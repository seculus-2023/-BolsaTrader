"""
Camada de serviços do BolsaTrader:

- integração com a API pública brapi.dev (cotações da B3);
- cálculo de posições e percentual de lucro/perda;
- análise simples de tendência (alta/baixa) a partir do histórico de cotações;
- geração de alertas/lembretes.

Manter a lógica de negócio aqui (fora das views) facilita testes e reuso
tanto pelas views quanto pelos comandos de management (cron).
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

from django.db.models import F, Sum

from .models import (
    ConsumoApiBrapi,
    Ativo, Cotacao, Operacao, Alerta, FonteNoticia, Noticia, AcaoB3, CotacaoIndice,
    RegistroAtualizacaoCarteira, ContaCorrente, LancamentoContaCorrente, PostIt,
)


# --------------------------------------------------------------------------
# Atividade recente de todos os usuários (mural da comunidade)
# --------------------------------------------------------------------------
def atividade_recente(limite: int = 15) -> list[dict]:
    """
    Retorna as operações mais recentes de TODOS os usuários (nome + tipo de
    operação + quando), para um mural de atividade da comunidade. Não expõe
    ticker, quantidade nem preço - esses detalhes continuam privados de
    cada usuário.
    """
    operacoes = (
        Operacao.objects.select_related("usuario").order_by("-criado_em")[:limite]
    )
    return [
        {
            "nome": op.usuario.first_name or op.usuario.username,
            "tipo": op.tipo,
            "tipo_label": op.get_tipo_display(),
            "quando": op.criado_em,
        }
        for op in operacoes
    ]


# --------------------------------------------------------------------------
# Horário de negociação da B3 (configurável em B3_HORARIO_ABERTURA/
# B3_HORARIO_FECHAMENTO no .env)
# --------------------------------------------------------------------------
def mercado_b3_aberto() -> bool:
    """
    True quando agora é dia útil (segunda a sexta) e está dentro do horário
    de negociação configurado - usado tanto para mostrar o badge de mercado
    aberto/fechado quanto para bloquear "Atualizar cotações agora" fora do
    horário (fora do pregão a cotação não muda, então não há nada de novo
    pra buscar).
    """
    from datetime import datetime

    abertura = datetime.strptime(settings.B3_HORARIO_ABERTURA, "%H:%M").time()
    fechamento = datetime.strptime(settings.B3_HORARIO_FECHAMENTO, "%H:%M").time()

    agora = timezone.localtime()
    dia_util = agora.weekday() < 5  # segunda(0) a sexta(4)
    return dia_util and abertura <= agora.time() <= fechamento


# --------------------------------------------------------------------------
# Integração com a API de cotações (brapi.dev)
# --------------------------------------------------------------------------
class BrapiError(Exception):
    """Erro ao consultar a API de cotações."""


def _detalhe_limite_brapi(resposta) -> str | None:
    """
    A brapi.dev responde 429 tanto pra um limite passageiro (por minuto -
    resolve tentando de novo em instantes) quanto pro limite MENSAL do
    plano gratuito esgotado (só volta a funcionar na próxima virada do
    ciclo, às vezes semanas depois - "tente novamente em instantes" seria
    enganoso nesse caso). O corpo da resposta traz um "code" que distingue
    os dois; quando é o limite mensal, monta uma mensagem com a data real
    de renovação em vez da genérica.
    """
    try:
        corpo = resposta.json()
    except (ValueError, AttributeError):
        return None

    if corpo.get("code") != "MONTHLY_LIMIT_EXCEEDED":
        return None

    resets_at = (corpo.get("details") or {}).get("usage", {}).get("resetsAt")
    instante = parse_datetime(resets_at) if resets_at else None
    quando = (
        f"em {timezone.localtime(instante).strftime('%d/%m/%Y')}" if instante else "no início do próximo ciclo"
    )
    return f"Limite mensal de consultas da API de cotações foi atingido - só volta a funcionar {quando}"


def _mensagem_falha_api(exc: requests.RequestException, contexto: str) -> str:
    """
    Mensagem amigável para falhas de rede numa API externa (brapi.dev ou
    Banco Central), sem embutir a exceção crua na mensagem - o texto de uma
    requests.RequestException inclui a URL completa da requisição, e para a
    brapi.dev isso significa expor o BRAPI_TOKEN em texto puro. Essas
    mensagens às vezes chegam até a tela do usuário (ver
    core.views.consultar_cotacao_avulsa, acoes_b3_atualizar,
    atualizar_benchmarks_agora), então vazar o token ali seria uma falha de
    segurança desnecessária.

    Trata especificamente o limite de requisições (HTTP 429) com uma
    mensagem própria, distinguindo o limite mensal esgotado (ver
    _detalhe_limite_brapi) do limite passageiro por minuto - são situações
    bem diferentes na prática.
    """
    resposta = getattr(exc, "response", None)
    status = getattr(resposta, "status_code", None)
    if status == 429:
        detalhe_mensal = _detalhe_limite_brapi(resposta) if resposta is not None else None
        if detalhe_mensal:
            return f"{detalhe_mensal} ({contexto})."
        return f"Limite de consultas da API atingido para {contexto}. Tente novamente em alguns minutos."
    return f"Falha ao consultar a API para {contexto}. Tente novamente em instantes."


# --------------------------------------------------------------------------
# Orçamento de requisições à brapi.dev
#
# O plano contratado tem um limite por ciclo de 30 dias (BRAPI_LIMITE_MENSAL,
# Startup = 150.000) e estourar deixa o sistema sem cotações até a renovação.
# Toda requisição à brapi.dev passa por _get_brapi, que conta num contador
# diário (ConsumoApiBrapi, compartilhado entre os processos que usam o mesmo
# banco) e trava antes de estourar.
# --------------------------------------------------------------------------
BRAPI_TETO_ABSOLUTO_PCT = 95  # acima disso NENHUMA requisição é feita (nem manual)
DIAS_UTEIS_POR_CICLO = 22  # dias úteis médios em 30 dias corridos


def consumo_api_ultimos_30_dias() -> int:
    """Requisições feitas à brapi.dev nos últimos 30 dias (janela móvel, conservadora frente ao ciclo do plano)."""
    inicio = timezone.localdate() - timedelta(days=29)
    return ConsumoApiBrapi.objects.filter(data__gte=inicio).aggregate(total=Sum("requisicoes"))["total"] or 0


def limite_automatico_api() -> int:
    """Consumo a partir do qual o ciclo AUTOMÁTICO pausa - deixa a margem de segurança para uso manual/imprevistos."""
    return int(settings.BRAPI_LIMITE_MENSAL * (100 - settings.BRAPI_MARGEM_SEGURANCA_PCT) / 100)


def limite_absoluto_api() -> int:
    """Consumo a partir do qual QUALQUER requisição é bloqueada."""
    return int(settings.BRAPI_LIMITE_MENSAL * BRAPI_TETO_ABSOLUTO_PCT / 100)


def orcamento_automatico_disponivel() -> bool:
    return consumo_api_ultimos_30_dias() < limite_automatico_api()


def projetar_consumo_mensal(qtd_ativos: int, intervalo_minutos: int | None = None) -> dict:
    """
    Estima quantas requisições/mês o ciclo automático consome: ciclos por dia
    (só dentro do pregão) x requisições por ciclo (ativos / tickers por lote)
    x dias úteis. Serve para conferir se COTACOES_INTERVALO_MINUTOS cabe no
    orçamento antes de o consumo real mostrar o problema.
    """
    from datetime import datetime as _dt

    intervalo = intervalo_minutos or settings.COTACOES_INTERVALO_MINUTOS
    abertura = _dt.strptime(settings.B3_HORARIO_ABERTURA, "%H:%M")
    fechamento = _dt.strptime(settings.B3_HORARIO_FECHAMENTO, "%H:%M")
    minutos_pregao = max(int((fechamento - abertura).total_seconds() // 60), 0)
    ciclos_dia = minutos_pregao // max(intervalo, 1) + 1
    lote = max(getattr(settings, "BRAPI_TICKERS_POR_LOTE", BRAPI_TICKERS_POR_LOTE_PADRAO), 1)
    requisicoes_por_ciclo = -(-qtd_ativos // lote) if qtd_ativos else 0  # divisão com teto
    mensal = ciclos_dia * requisicoes_por_ciclo * DIAS_UTEIS_POR_CICLO
    return {
        "ciclos_dia": ciclos_dia,
        "requisicoes_por_ciclo": requisicoes_por_ciclo,
        "requisicoes_mes": mensal,
        "limite_automatico": limite_automatico_api(),
        "cabe_no_orcamento": mensal <= limite_automatico_api(),
        "percentual_do_limite": round(mensal / settings.BRAPI_LIMITE_MENSAL * 100, 1),
    }


def _get_brapi(url: str, params: dict, timeout: int):
    """requests.get para a brapi.dev com controle de orçamento: bloqueia acima do teto absoluto e conta a requisição."""
    if consumo_api_ultimos_30_dias() >= limite_absoluto_api():
        raise BrapiError(
            "Orçamento mensal de requisições à API de cotações quase esgotado - consultas bloqueadas por "
            "segurança até o consumo dos últimos 30 dias baixar."
        )
    dia = ConsumoApiBrapi.objects.get_or_create(data=timezone.localdate())[0]
    ConsumoApiBrapi.objects.filter(pk=dia.pk).update(requisicoes=F("requisicoes") + 1)
    return requests.get(url, params=params, timeout=timeout)


def buscar_cotacao_atual(ticker: str) -> dict:
    """
    Consulta a cotação atual de um ticker na API brapi.dev.

    Retorna um dicionário com pelo menos: regularMarketPrice, shortName,
    regularMarketChangePercent.

    Documentação: https://brapi.dev/docs
    """
    url = f"{settings.BRAPI_BASE_URL}/quote/{ticker.upper()}"
    params = {}
    if settings.BRAPI_TOKEN:
        params["token"] = settings.BRAPI_TOKEN

    try:
        resposta = _get_brapi(url, params=params, timeout=10)
        resposta.raise_for_status()
        dados = resposta.json()
    except requests.RequestException as exc:
        raise BrapiError(_mensagem_falha_api(exc, f"o ticker {ticker}")) from exc

    resultados = dados.get("results") or []
    if not resultados:
        raise BrapiError(f"Ticker {ticker} não encontrado na API de cotações.")

    return resultados[0]


def buscar_cotacao_atual_com_historico(ticker: str, dias_historico: int = 30) -> tuple[dict, list[float]]:
    """
    Busca a cotação atual de um ticker JUNTO com um histórico recente de
    fechamentos, numa única chamada à API - a brapi.dev retorna os mesmos
    campos de "cotação agora" (regularMarketPrice etc.) e também
    "historicalDataPrice" na mesma resposta quando os parâmetros
    range/interval são informados, então dá pra ter os dois sem uma segunda
    requisição.

    Usado pela consulta avulsa (ver core.views.consultar_cotacao_avulsa)
    para calcular o IFR/RSI (ver calcular_rsi) de qualquer ticker pesquisado,
    mesmo um que o usuário não tenha em carteira, sem gastar chamada extra
    de API.

    Retorna (dados_atuais, precos_cronologicos) - o segundo item já vem do
    fechamento mais antigo para o mais recente, pronto para calcular_rsi.
    """
    url = f"{settings.BRAPI_BASE_URL}/quote/{ticker.upper()}"
    params = {"range": _range_brapi(dias_historico), "interval": "1d"}
    if settings.BRAPI_TOKEN:
        params["token"] = settings.BRAPI_TOKEN

    try:
        resposta = _get_brapi(url, params=params, timeout=15)
        resposta.raise_for_status()
        dados = resposta.json()
    except requests.RequestException as exc:
        raise BrapiError(_mensagem_falha_api(exc, f"o ticker {ticker}")) from exc

    resultados = dados.get("results") or []
    if not resultados:
        raise BrapiError(f"Ticker {ticker} não encontrado na API de cotações.")

    dados_atuais = resultados[0]

    pontos_ordenados = []
    vistos = set()
    for ponto in dados_atuais.get("historicalDataPrice") or []:
        fechamento = ponto.get("close")
        data_convertida = _converter_data_historico(ponto.get("date"))
        if fechamento is None or data_convertida is None or data_convertida in vistos:
            continue
        vistos.add(data_convertida)
        pontos_ordenados.append((data_convertida, float(fechamento)))
    pontos_ordenados.sort(key=lambda item: item[0])

    precos_cronologicos = [preco for _, preco in pontos_ordenados]
    return dados_atuais, precos_cronologicos


# brapi.dev aceita vários tickers separados por vírgula no mesmo endpoint de
# cotação avulsa (ex: /quote/PETR4,VALE3,ITUB4) - agrupar em lotes assim, em
# vez de uma requisição HTTP por ativo, reduz drasticamente o número de
# chamadas à API e ajuda a evitar o erro 429 ("Too Many Requests"). O número
# máximo de tickers por requisição depende do PLANO contratado na brapi.dev
# (ex: gratuito e Startup = 10, Pro = 20 - pedir mais que o limite do plano
# não dá 429, dá 400 "QUOTES_PER_REQUEST_EXCEEDED", e o lote inteiro falha em
# silêncio) - configurável via BRAPI_TICKERS_POR_LOTE no .env pra acompanhar
# o plano sem precisar mexer no código.
BRAPI_TICKERS_POR_LOTE_PADRAO = 10


def buscar_cotacoes_em_lote(tickers: list[str]) -> dict[str, dict]:
    """
    Busca a cotação atual de vários tickers de uma vez (ver
    BRAPI_TICKERS_POR_LOTE no .env), em lotes de tamanho fixo. Retorna um
    dict {TICKER: dados_da_api} só com os tickers efetivamente encontrados -
    tickers não encontrados ou cujo lote falhou (erro de rede, 429, ou 400
    quando o lote excede o limite de ativos por requisição do plano
    contratado) simplesmente não aparecem no retorno; cabe ao chamador (ver
    atualizar_cotacoes_ativos) tratar a ausência como falha por ativo, sem
    que a falha de um lote derrube os demais lotes que deram certo.
    """
    tamanho_lote = getattr(settings, "BRAPI_TICKERS_POR_LOTE", BRAPI_TICKERS_POR_LOTE_PADRAO) or 1
    resultado = {}
    for inicio in range(0, len(tickers), tamanho_lote):
        lote = tickers[inicio:inicio + tamanho_lote]
        url = f"{settings.BRAPI_BASE_URL}/quote/{','.join(t.upper() for t in lote)}"
        params = {}
        if settings.BRAPI_TOKEN:
            params["token"] = settings.BRAPI_TOKEN

        try:
            resposta = _get_brapi(url, params=params, timeout=15)
            resposta.raise_for_status()
            dados = resposta.json()
        except requests.RequestException:
            continue  # esse lote falhou - os tickers dele ficam de fora do resultado

        for item in dados.get("results") or []:
            simbolo = (item.get("symbol") or "").upper()
            if simbolo:
                resultado[simbolo] = item

    return resultado


def atualizar_cotacoes_ativos(ativos) -> tuple[int, int]:
    """
    Atualiza a cotação de uma lista/queryset de ativos com uma (ou poucas,
    ver BRAPI_TICKERS_POR_LOTE no .env) chamada(s) em lote à API, em vez de
    uma requisição HTTP por ativo - usado tanto pelo botão "Atualizar
    cotações agora" quanto pelo ciclo automático (ver
    executar_ciclo_atualizacao_cotacoes). Retorna (atualizados, falhas).
    """
    ativos = list(ativos)
    try:
        lote = buscar_cotacoes_em_lote([a.ticker for a in ativos])
    except BrapiError:
        lote = {}  # orçamento de requisições esgotado (ver _get_brapi) - todos contam como falha

    atualizados, falhas = 0, 0
    for ativo in ativos:
        dados_api = lote.get(ativo.ticker.upper())
        try:
            if dados_api is None:
                raise BrapiError(f"Ticker {ativo.ticker} não encontrado na consulta em lote.")
            atualizar_cotacao_diaria(ativo, dados_api=dados_api)
            atualizados += 1
        except BrapiError:
            falhas += 1

    return atualizados, falhas


def buscar_maiores_variacoes(limite: int = 5) -> tuple[list[dict], list[dict]]:
    """
    Consulta a lista geral de ações da B3 na API brapi.dev e retorna as
    maiores altas e as maiores baixas do dia (ordenadas pela variação
    percentual atual), cada uma limitada a `limite` itens.
    """
    url = f"{settings.BRAPI_BASE_URL}/quote/list"
    params_base = {"type": "stock", "limit": limite}
    if settings.BRAPI_TOKEN:
        params_base["token"] = settings.BRAPI_TOKEN

    def _consultar(sort_order: str) -> list[dict]:
        params = {**params_base, "sortBy": "change", "sortOrder": sort_order}
        try:
            resposta = _get_brapi(url, params=params, timeout=10)
            resposta.raise_for_status()
            dados = resposta.json()
        except requests.RequestException as exc:
            raise BrapiError(_mensagem_falha_api(exc, "as maiores variações do dia")) from exc
        return dados.get("stocks") or []

    maiores_altas = _consultar("desc")
    maiores_baixas = _consultar("asc")
    return maiores_altas, maiores_baixas


def _para_decimal(valor) -> Decimal | None:
    """Converte um valor numérico da API para Decimal, tolerando None/ausente."""
    if valor is None:
        return None
    try:
        return Decimal(str(valor)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (ArithmeticError, ValueError, TypeError):
        return None


def sincronizar_acoes_b3() -> int:
    """
    Sincroniza o catálogo de ações da B3 (AcaoB3) com a lista atual da API
    brapi.dev: apaga o catálogo inteiro e cadastra de novo a partir do que a
    API retornar agora - botão "Atualizar lista" na tela Ações da B3.

    Se a API não retornar nenhuma ação, trata como falha (BrapiError) em vez
    de apagar o catálogo existente - evita esvaziar a lista por causa de uma
    leitura ruim. Retorna quantas ações foram cadastradas.
    """
    url = f"{settings.BRAPI_BASE_URL}/quote/list"
    params = {"type": "stock", "limit": 1000}
    if settings.BRAPI_TOKEN:
        params["token"] = settings.BRAPI_TOKEN

    try:
        resposta = _get_brapi(url, params=params, timeout=30)
        resposta.raise_for_status()
        dados = resposta.json()
    except requests.RequestException as exc:
        raise BrapiError(_mensagem_falha_api(exc, "a lista de ações da B3")) from exc

    acoes = dados.get("stocks") or []
    if not acoes:
        raise BrapiError("A API não retornou nenhuma ação da B3 agora.")

    vistos = set()
    novas = []
    for item in acoes:
        ticker = (item.get("stock") or "").strip().upper()
        if not ticker or ticker in vistos:
            continue  # ignora entradas sem ticker ou duplicadas na resposta da API
        vistos.add(ticker)
        novas.append(AcaoB3(
            ticker=ticker,
            nome=item.get("name") or "",
            setor=item.get("sector") or "",
            logo_url=item.get("logo") or "",
            preco_atual=_para_decimal(item.get("close")),
            variacao_dia_pct=_para_decimal(item.get("change")),
        ))

    AcaoB3.objects.all().delete()
    AcaoB3.objects.bulk_create(novas)
    return len(novas)


def atualizar_cotacao_diaria(ativo: Ativo, dados_api: dict | None = None) -> Cotacao:
    """Busca (ou recebe) a cotação atual de um ativo e grava/atualiza o registro do dia."""
    if dados_api is None:
        dados_api = buscar_cotacao_atual(ativo.ticker)

    # a brapi.dev às vezes aninha os campos em "data" (ex: consultas em lote);
    # trata os dois formatos pra não quebrar se a API mudar o formato de novo.
    dados = dados_api.get("data") if isinstance(dados_api.get("data"), dict) else dados_api

    preco = dados.get("regularMarketPrice")
    variacao = dados.get("regularMarketChangePercent")

    if preco is None:
        raise BrapiError(f"API não retornou preço para {ativo.ticker}.")

    ativo.nome = dados.get("shortName") or ativo.nome
    ativo.nome_longo = dados.get("longName") or ativo.nome_longo
    ativo.moeda = dados.get("currency") or ativo.moeda
    ativo.logo_url = dados.get("logourl") or ativo.logo_url
    ativo.maxima_dia = _para_decimal(dados.get("regularMarketDayHigh"))
    ativo.minima_dia = _para_decimal(dados.get("regularMarketDayLow"))
    ativo.abertura = _para_decimal(dados.get("regularMarketOpen"))
    ativo.fechamento_anterior = _para_decimal(dados.get("regularMarketPreviousClose"))
    ativo.variacao_dia_valor = _para_decimal(dados.get("regularMarketChange"))
    ativo.volume = dados.get("regularMarketVolume")
    ativo.valor_mercado = _para_decimal(dados.get("marketCap"))
    ativo.minima_52_semanas = _para_decimal(dados.get("fiftyTwoWeekLow"))
    ativo.maxima_52_semanas = _para_decimal(dados.get("fiftyTwoWeekHigh"))

    hora_cotacao = dados.get("regularMarketTime")
    if hora_cotacao:
        ativo.hora_cotacao = parse_datetime(hora_cotacao)

    if not ativo.setor:
        # nem toda resposta da brapi.dev traz "sector" na cotação avulsa (é
        # mais comum vir na lista usada por sincronizar_acoes_b3) - quando
        # falta, tenta completar pelo catálogo Ações da B3 já sincronizado
        # (ver core.services.sincronizar_acoes_b3), sem custo de mais uma
        # chamada de API. Usado pela análise de concentração por setor da
        # carteira (ver calcular_concentracao_setor).
        setor = dados.get("sector") or dados.get("sectorName")
        if not setor:
            catalogo = AcaoB3.objects.filter(ticker=ativo.ticker).values_list("setor", flat=True).first()
            setor = catalogo or ""
        ativo.setor = setor

    ativo.save()  # também atualiza 'atualizado_em' (auto_now) com o horário desta atualização

    cotacao, _ = Cotacao.objects.update_or_create(
        ativo=ativo,
        data=timezone.localdate(),
        defaults={
            "preco_fechamento": Decimal(str(preco)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            "variacao_dia_pct": Decimal(str(variacao)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if variacao is not None
            else None,
            "volume": dados.get("regularMarketVolume"),
        },
    )
    return cotacao


# --------------------------------------------------------------------------
# Ciclo completo de atualização automática (cotações + alertas + robô +
# histórico da carteira) - lógica compartilhada entre o comando de
# management "atualizar_cotacoes" (--loop) e o agendador embutido no
# próprio processo do servidor (ver iniciar_agendador_cotacoes_embutido,
# ligado em bolsatrader/wsgi.py) - um único lugar pra não duplicar a regra
# entre os dois "chamadores".
# --------------------------------------------------------------------------
def executar_ciclo_atualizacao_cotacoes() -> dict:
    """
    Atualiza a cotação de todos os ativos com alguma operação registrada,
    depois gera os alertas de lucro/perda/tendência, roda o robô consultor e
    grava o retrato da carteira (ver registrar_atualizacao_carteira) de
    cada usuário com operações. Retorna um resumo numérico do que foi feito.
    """
    ativos = Ativo.objects.filter(operacoes__isnull=False).distinct()
    atualizados, falhas = atualizar_cotacoes_ativos(ativos)

    total_alertas, total_sinais_robo = 0, 0
    for usuario in get_user_model().objects.filter(operacoes__isnull=False).distinct():
        total_alertas += len(gerar_alertas_para_usuario(usuario))
        total_sinais_robo += len(gerar_sinais_robo_para_usuario(usuario))
        registrar_atualizacao_carteira(usuario)

    return {
        "ativos_total": ativos.count(),
        "ativos_atualizados": atualizados,
        "ativos_falha": falhas,
        "alertas_gerados": total_alertas,
        "sinais_robo_gerados": total_sinais_robo,
    }


_agendador_cotacoes_lock = threading.Lock()
_agendador_cotacoes_iniciado = False


def iniciar_agendador_cotacoes_embutido() -> bool:
    """
    Inicia, numa thread em segundo plano dentro do próprio processo do
    servidor web, o mesmo ciclo do comando "atualizar_cotacoes --loop" -
    repete a cada COTACOES_INTERVALO_MINUTOS minutos (.env), sem precisar
    manter um processo/terminal separado rodando esse comando à parte.

    Ligado em bolsatrader/wsgi.py (só quando um servidor WSGI de verdade
    carrega o app - Waitress/gunicorn -, nunca durante "manage.py migrate",
    "test" etc., já que esses comandos não importam o módulo wsgi).
    Idempotente (chamar mais de uma vez não inicia uma segunda thread) e
    desligado por padrão quando COTACOES_INTERVALO_MINUTOS é zero/negativo,
    ou explicitamente via AGENDADOR_COTACOES_EMBUTIDO=False no .env (usado
    em deploys com múltiplos processos worker - Docker/gunicorn com
    --workers > 1 -, onde cada worker chamaria isto e duplicaria o
    trabalho; nesse caso, use o comando "atualizar_cotacoes --loop" à parte,
    em um único processo, em vez do agendador embutido).

    Retorna True se a thread foi (ou já estava) iniciada, False se está
    desligado por configuração.
    """
    global _agendador_cotacoes_iniciado

    if not getattr(settings, "AGENDADOR_COTACOES_EMBUTIDO", True):
        return False

    intervalo = getattr(settings, "COTACOES_INTERVALO_MINUTOS", 0)
    if not intervalo or intervalo <= 0:
        return False

    with _agendador_cotacoes_lock:
        if _agendador_cotacoes_iniciado:
            return True
        _agendador_cotacoes_iniciado = True

    def _loop():
        # dorme antes do primeiro ciclo (em vez de rodar na hora) de propósito:
        # a thread é iniciada na importação de bolsatrader/wsgi.py, junto com
        # a subida do processo do servidor - rodar um ciclo completo (rede +
        # banco, para todos os usuários) nesse instante competiria com o
        # próprio servidor terminando de subir. Quem precisa de uma
        # atualização imediata usa o botão "Atualizar cotações agora".
        while True:
            time.sleep(intervalo * 60)
            _ciclo_agendador_embutido()

    threading.Thread(target=_loop, name="bolsatrader-cotacoes-agendador", daemon=True).start()
    # aviso no console do servidor: o intervalo é lido do .env só na subida do
    # processo - mudar COTACOES_INTERVALO_MINUTOS com o servidor rodando não
    # tem efeito até reiniciar, e sem esta linha não dá pra saber qual valor
    # está valendo de verdade.
    print(
        f"[BolsaTrader] Agendador de cotações ligado: a cada {intervalo} minuto(s), "
        "só durante o pregão da B3.", flush=True,
    )
    return True


def _ciclo_agendador_embutido() -> None:
    """
    Um "tick" do agendador embutido (ver iniciar_agendador_cotacoes_embutido):
    só executa o ciclo de atualização dentro do horário de negociação da B3
    (B3_HORARIO_ABERTURA/FECHAMENTO no .env) - fora do pregão o preço não
    muda, então não há nada de novo pra buscar. Extraído à parte pra poder
    ser testado sem precisar controlar a thread/sleep de verdade.
    """
    if not mercado_b3_aberto():
        return
    if not orcamento_automatico_disponivel():
        logger.warning(
            "Atualização automática de cotações pausada: %s requisições nos últimos 30 dias, acima do limite "
            "automático de %s (margem de segurança do plano).",
            consumo_api_ultimos_30_dias(), limite_automatico_api(),
        )
        return
    try:
        resultado = executar_ciclo_atualizacao_cotacoes()
        logger.info(
            "Atualização automática de cotações: %s/%s ativo(s), %s alerta(s), %s sinal(is) do robô.",
            resultado["ativos_atualizados"], resultado["ativos_total"],
            resultado["alertas_gerados"], resultado["sinais_robo_gerados"],
        )
    except Exception:
        logger.exception("Falha na atualização automática de cotações (agendador embutido).")


# --------------------------------------------------------------------------
# Histórico de preços (brapi.dev) e backfill de Cotacao
#
# Usado tanto para preencher de uma vez o histórico de um ativo recém-
# cadastrado (RSI/MACD exigem ~35 pregões - sem isso o robô consultor fica
# "Aguardando histórico" por semanas, um dia de cada vez) quanto para o
# histórico do Ibovespa usado no comparativo com benchmarks (ver
# atualizar_benchmarks, em core.services).
# --------------------------------------------------------------------------
_FAIXAS_RANGE_BRAPI = [
    (30, "1mo"), (90, "3mo"),
]


def _range_brapi(dias: int) -> str:
    """
    Converte uma quantidade de dias no parâmetro `range` aceito pela
    brapi.dev. Limitado a "3mo" - ranges maiores ("6mo", "1y", ...) exigem
    um plano pago da brapi.dev e retornam 400 Bad Request no plano
    gratuito, então qualquer pedido de mais de 90 dias é limitado a essa
    janela em vez de estourar a requisição.
    """
    for limite, range_str in _FAIXAS_RANGE_BRAPI:
        if dias <= limite:
            return range_str
    return "3mo"


def _converter_data_historico(valor) -> date | None:
    """
    Converte a data de um ponto do histórico da brapi.dev para date - a API
    retorna timestamp Unix (segundos, UTC) no campo "date" de
    historicalDataPrice, mas trata string ISO também, por segurança caso o
    formato mude (mesma cautela já aplicada em atualizar_cotacao_diaria).
    """
    if isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        try:
            return datetime.fromtimestamp(valor, tz=dt_timezone.utc).date()
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(valor, str):
        convertido = parse_datetime(valor)
        if convertido:
            return convertido.date()
        try:
            return date.fromisoformat(valor[:10])
        except ValueError:
            return None
    return None


def buscar_historico_precos(ticker: str, dias: int = 180) -> list[dict]:
    """
    Busca o histórico diário de fechamento de um ticker (ação da B3, ou um
    índice como "^BVSP" para o Ibovespa) na API brapi.dev.

    Retorna uma lista de {"data": date, "fechamento": Decimal}, em ordem
    cronológica (mais antigo primeiro) e sem datas duplicadas.
    """
    url = f"{settings.BRAPI_BASE_URL}/quote/{ticker.upper()}"
    params = {"range": _range_brapi(dias), "interval": "1d"}
    if settings.BRAPI_TOKEN:
        params["token"] = settings.BRAPI_TOKEN

    try:
        resposta = _get_brapi(url, params=params, timeout=30)
        resposta.raise_for_status()
        dados = resposta.json()
    except requests.RequestException as exc:
        raise BrapiError(_mensagem_falha_api(exc, f"o histórico de {ticker}")) from exc

    resultados = dados.get("results") or []
    if not resultados:
        raise BrapiError(f"Ticker {ticker} não encontrado na API de cotações (histórico).")

    pontos = resultados[0].get("historicalDataPrice") or []

    historico = []
    vistos = set()
    for ponto in pontos:
        fechamento = ponto.get("close")
        data_convertida = _converter_data_historico(ponto.get("date"))
        if fechamento is None or data_convertida is None or data_convertida in vistos:
            continue
        vistos.add(data_convertida)
        preco = _para_decimal(fechamento)
        if preco is not None:
            # volume já vem de graça no mesmo ponto do histórico (sem custo
            # extra de API) - usado pelo Scanner Técnico (ver analisar_volume_precos)
            historico.append({"data": data_convertida, "fechamento": preco, "volume": ponto.get("volume")})

    historico.sort(key=lambda item: item["data"])
    return historico


def backfill_historico_cotacoes(ativo: Ativo, dias: int = 180) -> int:
    """
    Preenche de uma vez o histórico de cotações diárias (Cotacao) de um
    ativo, a partir do endpoint histórico da brapi.dev - chamado
    automaticamente ao registrar a primeira operação de um ativo novo (ver
    core.views.operacao_nova) e também disponível para ativos já existentes
    via o comando "python manage.py backfill_cotacoes".

    Não sobrescreve preço/variação de cotações que já existem (ex: a de hoje,
    gravada por atualizar_cotacao_diaria) - só grava as datas que ainda
    faltam. Retorna quantas cotações novas foram gravadas.

    Efeito colateral "auto-cura": o campo volume (ver Scanner Técnico,
    core.services.analisar_volume_precos) foi adicionado depois de várias
    cotações já existirem sem ele - como o histórico da brapi.dev já traz o
    volume de graça (mesma chamada, sem custo extra), aproveita pra completar
    o volume que estiver faltando em cotações JÁ existentes dentro do período
    buscado, sem tocar no preço/variação delas.
    """
    historico = buscar_historico_precos(ativo.ticker, dias=dias)
    if not historico:
        return 0

    datas_buscadas = [item["data"] for item in historico]
    existentes = {
        c.data: c for c in ativo.cotacoes.filter(data__in=datas_buscadas)
    }

    novas = [
        Cotacao(ativo=ativo, data=item["data"], preco_fechamento=item["fechamento"], volume=item.get("volume"))
        for item in historico
        if item["data"] not in existentes
    ]
    if novas:
        Cotacao.objects.bulk_create(novas, ignore_conflicts=True)

    por_data = {item["data"]: item.get("volume") for item in historico}
    a_completar = [
        Cotacao(id=cotacao.id, volume=por_data.get(data_))
        for data_, cotacao in existentes.items()
        if cotacao.volume is None and por_data.get(data_) is not None
    ]
    if a_completar:
        Cotacao.objects.bulk_update(a_completar, ["volume"])

    return len(novas)


# --------------------------------------------------------------------------
# Benchmark de mercado (Ibovespa / CDI)
#
# Responde a pergunta mais importante para decidir se vale a pena continuar
# operando ativamente: "minha carteira está rendendo mais do que simplesmente
# deixar esse dinheiro no Ibovespa (renda variável passiva) ou no CDI (renda
# fixa)?" - sem isso, saber que a carteira está "com +8% de lucro" não diz
# muita coisa sozinho.
# --------------------------------------------------------------------------
IBOVESPA_TICKER = "^BVSP"
_BCB_SGS_CDI = 12  # código da série do CDI (taxa diária, % ao dia) no SGS do Banco Central


def buscar_historico_cdi(dias: int = 180) -> list[dict]:
    """
    Busca o histórico diário da taxa do CDI (série 12 do SGS/Banco Central) -
    cada registro é a taxa do próprio dia (% ao dia), não um índice
    acumulado, então o retorno de um período precisa compor (juros
    compostos) as taxas, não só comparar o primeiro e o último valor (ver
    _retorno_cdi_periodo). Retorna uma lista de {"data": date, "taxa_pct":
    Decimal} em ordem cronológica.

    Reaproveita BrapiError para o erro, ainda que a fonte aqui seja o Banco
    Central (não a brapi.dev) - mantém um único tipo de exceção pros
    chamadores (atualizar_benchmarks, comandos de management) tratarem.

    Usa o endpoint por intervalo de datas (`dados?dataInicial=...`), não o
    `dados/ultimos/N` - esse último retorna 400 Bad Request ("quantidade
    máxima de valores deve ser 20") pra série do CDI acima de 20 registros.
    """
    data_final = date.today()
    data_inicial = data_final - timedelta(days=max(dias, 1))
    url = f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{_BCB_SGS_CDI}/dados"
    params = {
        "formato": "json",
        "dataInicial": data_inicial.strftime("%d/%m/%Y"),
        "dataFinal": data_final.strftime("%d/%m/%Y"),
    }
    try:
        resposta = requests.get(url, params=params, timeout=30)
        resposta.raise_for_status()
        dados = resposta.json()
    except requests.RequestException as exc:
        raise BrapiError(_mensagem_falha_api(exc, "o histórico do CDI no Banco Central")) from exc

    historico = []
    for item in dados or []:
        data_str = item.get("data")
        valor_str = item.get("valor")
        if not data_str or valor_str is None:
            continue
        try:
            data_convertida = datetime.strptime(data_str, "%d/%m/%Y").date()
            taxa = Decimal(str(valor_str))
        except (ValueError, ArithmeticError):
            continue
        historico.append({"data": data_convertida, "taxa_pct": taxa})

    historico.sort(key=lambda item: item["data"])
    return historico


def _gravar_historico_indice(indice: str, pontos: list[tuple]) -> int:
    """Grava em CotacaoIndice os pontos (data, valor) cuja data ainda não existe para aquele índice."""
    if not pontos:
        return 0
    datas = [data for data, _ in pontos]
    existentes = set(
        CotacaoIndice.objects.filter(indice=indice, data__in=datas).values_list("data", flat=True)
    )
    novos = [
        CotacaoIndice(indice=indice, data=data, valor=valor)
        for data, valor in pontos
        if data not in existentes
    ]
    if novos:
        CotacaoIndice.objects.bulk_create(novos, ignore_conflicts=True)
    return len(novos)


def atualizar_benchmarks(dias: int = 180) -> dict:
    """
    Atualiza o histórico de benchmarks (Ibovespa e CDI) usado no comparativo
    de desempenho da carteira (ver calcular_comparativo_benchmark) - grava em
    CotacaoIndice as datas que ainda não existiam. Chamado pelo comando
    "atualizar_benchmarks" (agendável via cron/--loop, separado de
    "atualizar_cotacoes" de propósito - os benchmarks não dependem de nenhum
    usuário ter ativos cadastrados e não precisam da mesma frequência).

    Ibovespa e CDI são buscados de fontes independentes - uma fonte fora do
    ar não impede a atualização da outra. Retorna {"ibovespa": <quantas
    cotações novas>, "cdi": <quantas taxas novas>, "erros": [...]}.
    """
    resultado = {"ibovespa": 0, "cdi": 0, "erros": []}

    try:
        historico_ibov = buscar_historico_precos(IBOVESPA_TICKER, dias=dias)
        resultado["ibovespa"] = _gravar_historico_indice(
            CotacaoIndice.IBOVESPA, [(item["data"], item["fechamento"]) for item in historico_ibov]
        )
    except BrapiError as exc:
        resultado["erros"].append(str(exc))

    try:
        historico_cdi = buscar_historico_cdi(dias=dias)
        resultado["cdi"] = _gravar_historico_indice(
            CotacaoIndice.CDI, [(item["data"], item["taxa_pct"]) for item in historico_cdi]
        )
    except BrapiError as exc:
        resultado["erros"].append(str(exc))

    return resultado


def ultimo_valor_ibovespa() -> dict | None:
    """
    Valor do Ibovespa (pontos de fechamento) DE HOJE e a variação percentual
    em relação ao pregão anterior já salvo - vem do histórico gravado por
    atualizar_benchmarks (botão "Atualizar benchmarks (Ibovespa / CDI)" ou o
    comando "atualizar_benchmarks"), não do ciclo de cotações dos ativos, que
    não mexe em índices.

    None quando ainda não há nenhum valor salvo, OU quando o valor mais
    recente salvo é de um dia anterior a hoje - de propósito, pra não mostrar
    um número desatualizado como se fosse o de agora; nesse caso a tela deve
    sugerir rodar aquela atualização.
    """
    ultimos = list(
        CotacaoIndice.objects.filter(indice=CotacaoIndice.IBOVESPA).order_by("-data")[:2]
    )
    if not ultimos or ultimos[0].data != timezone.localdate():
        return None

    atual = ultimos[0]
    variacao_pct = None
    if len(ultimos) == 2 and ultimos[1].valor:
        variacao_pct = ((atual.valor - ultimos[1].valor) / ultimos[1].valor * 100).quantize(Decimal("0.01"))

    return {"data": atual.data, "valor": atual.valor, "variacao_pct": variacao_pct}


def _retorno_indice_periodo(indice: str, data_inicio: date, data_fim: date) -> Decimal | None:
    """Retorno % de um índice tipo Ibovespa (comparando o primeiro e o último valor do período)."""
    pontos = list(
        CotacaoIndice.objects.filter(indice=indice, data__gte=data_inicio, data__lte=data_fim)
        .order_by("data").values_list("valor", flat=True)
    )
    if len(pontos) < 2 or not pontos[0]:
        return None
    return ((pontos[-1] - pontos[0]) / pontos[0] * 100).quantize(Decimal("0.01"))


def _retorno_cdi_periodo(data_inicio: date, data_fim: date) -> Decimal | None:
    """
    Retorno acumulado (%) do CDI no período, compondo (juros compostos) as
    taxas diárias - cada registro de CotacaoIndice(indice=CDI) é a taxa do
    próprio dia, não um índice, então soma-los diretamente subestimaria o
    efeito de juros sobre juros.
    """
    taxas = list(
        CotacaoIndice.objects.filter(indice=CotacaoIndice.CDI, data__gte=data_inicio, data__lte=data_fim)
        .order_by("data").values_list("valor", flat=True)
    )
    if not taxas:
        return None
    fator = Decimal("1")
    for taxa in taxas:
        fator *= (1 + taxa / 100)
    return ((fator - 1) * 100).quantize(Decimal("0.01"))


def calcular_comparativo_benchmark(usuario, posicoes: list | None = None, dias: int = 90) -> dict | None:
    """
    Compara o retorno da carteira comprada (não considera reservas) com o
    retorno do Ibovespa e o retorno acumulado do CDI no mesmo período - ajuda
    a decidir se vale a pena continuar operando ativamente em vez de deixar o
    dinheiro num índice ou na renda fixa.

    O período vai da abertura da posição comprada mais antiga até hoje,
    limitado a `dias` corridos (carteiras antigas comparam só a janela mais
    recente, pra manter a comparação relevante ao momento atual). O retorno
    da carteira é uma aproximação: usa a cotação mais próxima do início do
    período e a mais recente de cada ativo, ponderada pela quantidade em
    carteira hoje - não reconstitui compras/vendas feitas dentro da janela.

    Retorna None quando não há posição comprada com histórico suficiente
    para montar a comparação, ou quando os benchmarks ainda não têm dados no
    período (ver atualizar_benchmarks).
    """
    if posicoes is None:
        posicoes = calcular_posicoes(usuario)

    compradas = [p for p in posicoes if not p.apenas_reservado and p.quantidade > 0]
    datas_abertura = [p.data_abertura for p in compradas if p.data_abertura is not None]
    if not datas_abertura:
        return None

    hoje = timezone.localdate()
    limite_periodo = hoje - timedelta(days=dias)
    data_inicio = max(min(datas_abertura), limite_periodo)
    if data_inicio >= hoje:
        return None

    valor_inicial_total = Decimal("0")
    valor_final_total = Decimal("0")
    for p in compradas:
        cotacao_inicial = p.ativo.cotacoes.filter(data__gte=data_inicio).order_by("data").first()
        cotacao_final = p.ativo.ultima_cotacao()
        if not cotacao_inicial or not cotacao_final:
            continue
        valor_inicial_total += cotacao_inicial.preco_fechamento * p.quantidade
        valor_final_total += cotacao_final.preco_fechamento * p.quantidade

    if not valor_inicial_total:
        return None

    retorno_carteira_pct = ((valor_final_total - valor_inicial_total) / valor_inicial_total * 100).quantize(
        Decimal("0.01")
    )
    retorno_ibovespa_pct = _retorno_indice_periodo(CotacaoIndice.IBOVESPA, data_inicio, hoje)
    retorno_cdi_pct = _retorno_cdi_periodo(data_inicio, hoje)

    return {
        "data_inicio": data_inicio,
        "data_fim": hoje,
        "dias_periodo": (hoje - data_inicio).days,
        "retorno_carteira_pct": retorno_carteira_pct,
        "retorno_ibovespa_pct": retorno_ibovespa_pct,
        "retorno_cdi_pct": retorno_cdi_pct,
        "carteira_bateu_ibovespa": (
            retorno_ibovespa_pct is not None and retorno_carteira_pct > retorno_ibovespa_pct
        ),
        "carteira_bateu_cdi": retorno_cdi_pct is not None and retorno_carteira_pct > retorno_cdi_pct,
    }


# --------------------------------------------------------------------------
# Cálculo de posições (compra/venda) e lucro/perda
# --------------------------------------------------------------------------
@dataclass
class Posicao:
    ativo: Ativo
    quantidade: int
    preco_medio: Decimal
    valor_investido: Decimal
    preco_atual: Decimal | None = None
    variacao_dia_pct: Decimal | None = None
    valor_atual: Decimal | None = None
    lucro_perda_valor: Decimal | None = None
    lucro_perda_pct: Decimal | None = None
    meta_lucro_pct: Decimal | None = None
    meta_perda_pct: Decimal | None = None
    tendencia: str | None = None
    data_abertura: date | None = None
    quantidade_reservada: int = 0
    reserva_do_robo: bool = False
    reserva_operacao_ids: list[int] = field(default_factory=list)
    lotes_compra: list = field(default_factory=list)  # lotes (Operacao COMPRA) com saldo > 0 que compõem a posição

    @property
    def situacao(self) -> str:
        if self.lucro_perda_pct is None:
            return "SEM_COTACAO"
        if self.lucro_perda_pct > 0:
            return "LUCRO"
        if self.lucro_perda_pct < 0:
            return "PERDA"
        return "NEUTRO"

    @property
    def apenas_reservado(self) -> bool:
        """True quando o ativo não está de fato em carteira - só há reserva(s) de intenção de compra."""
        return self.quantidade == 0 and self.quantidade_reservada > 0

    @property
    def dias_desde_compra(self) -> int | None:
        """Dias corridos desde que a posição foi aberta (primeira compra após ela estar zerada)."""
        if self.data_abertura is None:
            return None
        return (timezone.localdate() - self.data_abertura).days

    @property
    def lucro_perda_por_dia_valor(self) -> Decimal | None:
        """
        Lucro/perda atual (R$, ainda não realizado) dividido pelos dias que a
        posição está em carteira - mede a "velocidade" do resultado (R$/dia),
        igual à mesma ideia já usada em Operacao.lucro_perda_por_dia para
        vendas, mas aqui sobre a posição aberta. None quando comprado hoje (0
        dias, não dá pra ratear) ou sem lucro/perda apurado (sem cotação).
        """
        dias = self.dias_desde_compra
        if self.lucro_perda_valor is None or not dias:
            return None
        return (self.lucro_perda_valor / dias).quantize(Decimal("0.01"))

    @property
    def lucro_perda_por_dia_pct(self) -> Decimal | None:
        """Lucro/perda atual (%) dividido pelos dias em carteira - mesma ideia de lucro_perda_por_dia_valor, em percentual."""
        dias = self.dias_desde_compra
        if self.lucro_perda_pct is None or not dias:
            return None
        return (self.lucro_perda_pct / dias).quantize(Decimal("0.01"))

    @property
    def meta_lucro_atingida(self) -> bool:
        """
        True quando o lucro/perda atual da posição já atingiu a meta de lucro
        (a própria, ou a padrão do sistema quando não há uma definida) - usado
        para destacar na grid que é hora de considerar a venda.
        """
        if self.lucro_perda_pct is None or self.apenas_reservado:
            return False
        # a meta de lucro é sempre um ganho (valor positivo) - normaliza caso
        # tenha sido salva como negativa (dado antigo, de antes da validação)
        meta = abs(
            self.meta_lucro_pct
            if self.meta_lucro_pct is not None
            else Decimal(str(settings.META_LUCRO_PADRAO))
        )
        return self.lucro_perda_pct >= meta

    @property
    def variacao_pct_reserva(self) -> Decimal | None:
        """
        Para posições só de reserva (sem compra real): variação percentual do
        preço atual em relação ao preço pretendido na reserva - usada para
        saber se a baixa desejada para comprar já foi atingida.
        """
        if not self.apenas_reservado or self.preco_atual is None or not self.preco_medio:
            return None
        return ((self.preco_atual - self.preco_medio) / self.preco_medio * 100).quantize(Decimal("0.01"))

    @property
    def meta_compra_atingida(self) -> bool:
        """
        True quando uma posição só de reserva já caiu até (ou além) a meta de
        baixa definida (a própria, ou a padrão do sistema quando não há uma
        definida) - usado para destacar na grid que é hora de considerar a compra.
        """
        variacao = self.variacao_pct_reserva
        if variacao is None:
            return False
        meta_bruta = (
            self.meta_perda_pct
            if self.meta_perda_pct is not None
            else Decimal(str(settings.META_PERDA_PADRAO))
        )
        # a meta de perda é sempre uma queda (valor negativo) - normaliza caso
        # tenha sido salva como positiva (dado antigo, de antes da validação)
        meta = -abs(meta_bruta)
        return variacao <= meta


def calcular_posicoes(usuario) -> list[Posicao]:
    """
    Consolida os lotes de compra do usuário em posições por ativo (preço
    médio de compra do saldo não vendido, quantidade líquida em carteira) e
    calcula o lucro/perda percentual com base na última cotação disponível.

    Cada Operacao tipo=COMPRA é um lote com seu próprio saldo (quantidade -
    quantidade_vendida); só o saldo ainda não vendido entra na posição.
    """
    operacoes = (
        Operacao.objects.filter(usuario=usuario)
        .select_related("ativo")
        .order_by("data_operacao", "criado_em")
    )

    agregados = defaultdict(lambda: {
        "quantidade": 0,
        "custo_total": Decimal("0"),
        "meta_lucro_pct": None,
        "meta_perda_pct": None,
        "data_abertura": None,
        "quantidade_reservada": 0,
        "custo_reservado": Decimal("0"),
        "reserva_do_robo": False,
        "reserva_operacao_ids": [],
        "lotes_compra": [],
    })

    for op in operacoes:
        item = agregados[op.ativo_id]
        if op.tipo == Operacao.COMPRA:
            saldo_lote = op.saldo
            if saldo_lote > 0:
                # a posição é composta pelo(s) lote(s) mais antigo(s) que ainda
                # têm saldo - "há quanto tempo está na carteira" olha pra eles
                if item["data_abertura"] is None or op.data_operacao < item["data_abertura"]:
                    item["data_abertura"] = op.data_operacao
                item["quantidade"] += saldo_lote
                item["custo_total"] += saldo_lote * op.preco_unitario
                item["lotes_compra"].append(op)
        else:  # RESERVAR: intenção de compra futura (no máximo 1 unidade), não altera a posição real
            item["quantidade_reservada"] += op.quantidade
            item["custo_reservado"] += op.valor_total
            item["reserva_operacao_ids"].append(op.id)
            if "robô consultor" in (op.observacao or "").lower():
                item["reserva_do_robo"] = True

        # a meta mais recente informada pelo usuário prevalece
        if op.meta_lucro_pct is not None:
            item["meta_lucro_pct"] = op.meta_lucro_pct
        if op.meta_perda_pct is not None:
            item["meta_perda_pct"] = op.meta_perda_pct
        item["ativo"] = op.ativo

    posicoes = []
    for ativo_id, item in agregados.items():
        if item["quantidade"] <= 0 and item["quantidade_reservada"] <= 0:
            continue  # posição zerada (tudo vendido) e sem nenhuma reserva - nada a mostrar

        ativo = item["ativo"]

        if item["quantidade"] > 0:
            preco_medio = (item["custo_total"] / item["quantidade"]).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            valor_investido = (preco_medio * item["quantidade"]).quantize(Decimal("0.01"))
        else:
            # só há reserva(s): usa o preço pretendido na reserva como referência,
            # sem valor investido de verdade (nenhum capital foi comprometido ainda)
            preco_medio = (item["custo_reservado"] / item["quantidade_reservada"]).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            valor_investido = Decimal("0.00")

        posicao = Posicao(
            ativo=ativo,
            quantidade=item["quantidade"],
            preco_medio=preco_medio,
            valor_investido=valor_investido,
            meta_lucro_pct=item["meta_lucro_pct"],
            meta_perda_pct=item["meta_perda_pct"],
            data_abertura=item["data_abertura"],
            quantidade_reservada=item["quantidade_reservada"],
            reserva_do_robo=item["reserva_do_robo"],
            reserva_operacao_ids=item["reserva_operacao_ids"],
            lotes_compra=item["lotes_compra"],
        )

        ultima_cotacao = ativo.ultima_cotacao()
        if ultima_cotacao:
            posicao.preco_atual = ultima_cotacao.preco_fechamento
            posicao.variacao_dia_pct = ultima_cotacao.variacao_dia_pct
            posicao.tendencia = analisar_tendencia(ativo)

        if ultima_cotacao and not posicao.apenas_reservado:
            posicao.valor_atual = (posicao.preco_atual * posicao.quantidade).quantize(Decimal("0.01"))
            posicao.lucro_perda_valor = (posicao.valor_atual - posicao.valor_investido).quantize(Decimal("0.01"))
            if posicao.valor_investido:
                posicao.lucro_perda_pct = (
                    (posicao.lucro_perda_valor / posicao.valor_investido) * 100
                ).quantize(Decimal("0.01"))

        posicoes.append(posicao)

    # menos dias em carteira primeiro; posições só com reserva (sem compra
    # real, sem "dias em carteira" pra comparar) ficam por último
    posicoes.sort(key=lambda p: (
        p.dias_desde_compra is None,
        p.dias_desde_compra if p.dias_desde_compra is not None else 0,
        p.ativo.ticker,
    ))
    return posicoes


# --------------------------------------------------------------------------
# Histórico de atualizações da carteira (snapshot a cada atualização de
# cotações) - ver core.models.RegistroAtualizacaoCarteira e a tela Histórico
# de Atualizações.
# --------------------------------------------------------------------------
def registrar_atualizacao_carteira(usuario, posicoes: list[Posicao] | None = None) -> RegistroAtualizacaoCarteira:
    """
    Grava um retrato (snapshot) dos totais da carteira comprada do usuário
    (total de ativos, valor investido, valor atual, lucro/perda) - chamado
    toda vez que as cotações são atualizadas, seja pelo botão manual
    "Atualizar cotações agora" (core.views.atualizar_cotacoes_agora) seja
    pelo comando "atualizar_cotacoes" (inclusive em --loop). Mesma conta
    usada nos cartões de resumo do Painel e de Posições em Carteira.
    """
    if posicoes is None:
        posicoes = calcular_posicoes(usuario)

    compradas = [p for p in posicoes if not p.apenas_reservado]
    valor_investido = sum((p.valor_investido for p in compradas), Decimal("0"))
    valor_atual = sum((p.valor_atual for p in compradas if p.valor_atual is not None), Decimal("0"))
    lucro_perda = valor_atual - valor_investido
    lucro_perda_pct = (
        (lucro_perda / valor_investido * 100).quantize(Decimal("0.01")) if valor_investido else None
    )

    return RegistroAtualizacaoCarteira.objects.create(
        usuario=usuario,
        total_ativos=len(compradas),
        valor_investido=valor_investido,
        valor_atual=valor_atual,
        lucro_perda=lucro_perda,
        lucro_perda_pct=lucro_perda_pct,
    )


def excluir_registros_atualizacao_antigos(usuario, dias: int) -> int:
    """
    Exclui os registros de atualização da carteira do usuário com mais de
    `dias` dias - usado no formulário "Excluir por dias" da tela Histórico
    de Atualizações, pra não deixar a tabela crescer indefinidamente quando
    o comando "atualizar_cotacoes --loop" fica rodando o dia inteiro.
    """
    limite = timezone.now() - timedelta(days=dias)
    excluidos, _ = RegistroAtualizacaoCarteira.objects.filter(
        usuario=usuario, criado_em__lt=limite,
    ).delete()
    return excluidos


def excluir_registros_atualizacao_por_periodo(usuario, data_inicio: date, data_fim: date) -> int:
    """
    Exclui os registros de atualização da carteira do usuário com data/hora
    entre `data_inicio` e `data_fim` (ambas incluídas) - usado no formulário
    "Excluir por período" da tela Histórico de Atualizações, alternativa ao
    "Excluir por dias" quando o usuário quer limpar uma janela específica em
    vez de "tudo mais antigo que N dias".
    """
    inicio = timezone.make_aware(datetime.combine(data_inicio, datetime.min.time()))
    fim = timezone.make_aware(datetime.combine(data_fim, datetime.max.time()))
    excluidos, _ = RegistroAtualizacaoCarteira.objects.filter(
        usuario=usuario, criado_em__gte=inicio, criado_em__lte=fim,
    ).delete()
    return excluidos


def calcular_variacoes_historico(registros_desc: list[RegistroAtualizacaoCarteira]) -> list[dict]:
    """
    Para cada registro do histórico de atualizações (ordenados do mais
    recente para o mais antigo, mesma ordem da tela), calcula a variação do
    valor atual da carteira (R$ e %) em relação ao registro imediatamente
    anterior no tempo - "quanto mudou desde a atualização passada". O
    registro mais antigo da lista fica sem variação (não há um anterior pra
    comparar), assim como quando o valor atual anterior era zero (não dá
    pra calcular percentual sobre zero).

    Retorna uma lista de dicts na mesma ordem de entrada, cada um com
    "registro", "variacao_valor" e "variacao_pct".
    """
    resultado = []
    for i, registro in enumerate(registros_desc):
        anterior = registros_desc[i + 1] if i + 1 < len(registros_desc) else None
        variacao_valor = None
        variacao_pct = None
        if anterior is not None:
            variacao_valor = (registro.valor_atual - anterior.valor_atual).quantize(Decimal("0.01"))
            if anterior.valor_atual:
                variacao_pct = (variacao_valor / anterior.valor_atual * 100).quantize(Decimal("0.01"))
        resultado.append({
            "registro": registro,
            "variacao_valor": variacao_valor,
            "variacao_pct": variacao_pct,
        })
    return resultado


def construir_grafico_atualizacoes_dia(
    registros_dia: list[RegistroAtualizacaoCarteira], largura: int = 640, altura: int = 200, padding: int = 40,
) -> dict | None:
    """
    Monta os dados (SVG) do gráfico "Variações do dia" da tela Histórico de
    Atualizações: evolução do lucro/perda (%) da carteira ao longo dos
    registros de hoje, na ordem em que foram gravados. Recebe os registros
    já filtrados pelo dia atual, em ordem cronológica (mais antigo primeiro).

    Retorna None quando não há pelo menos 2 registros hoje (não dá pra
    traçar uma linha com um ponto só).
    """
    pontos_validos = [r for r in registros_dia if r.lucro_perda_pct is not None]
    if len(pontos_validos) < 2:
        return None

    valores = [float(r.lucro_perda_pct) for r in pontos_validos]
    valor_min, valor_max = min(valores), max(valores)
    faixa = (valor_max - valor_min) or 1.0

    plot_largura = largura - (padding * 2)
    plot_altura = altura - (padding * 2)
    passo_x = plot_largura / (len(pontos_validos) - 1)

    pontos = []
    comandos_path = []
    for i, (registro, valor) in enumerate(zip(pontos_validos, valores)):
        x = round(padding + i * passo_x, 2)
        y = round(padding + (1 - (valor - valor_min) / faixa) * plot_altura, 2)
        comandos_path.append(f"{'M' if i == 0 else 'L'}{x} {y}")

        pontos.append({
            "x": x,
            "y": y,
            "hora_label": timezone.localtime(registro.criado_em).strftime("%H:%M"),
            "valor_atual_label": f"R$ {registro.valor_atual:,.2f}".replace(",", "#").replace(".", ",").replace("#", "."),
            "lucro_perda_label": f"R$ {registro.lucro_perda:,.2f}".replace(",", "#").replace(".", ",").replace("#", "."),
            "lucro_perda_pct_label": f"{valor:.2f}%".replace(".", ","),
            "positivo": valor >= 0,
        })

    return {
        "id_svg": "pontos-atualizacoes-dia",
        "largura": largura,
        "altura": altura,
        "x_inicio": padding,
        "x_fim": largura - padding,
        "y_topo": padding,
        "y_base": altura - padding,
        "path_d": " ".join(comandos_path),
        "pontos": pontos,
        "faixa_min_label": f"{valor_min:.2f}%".replace(".", ","),
        "faixa_max_label": f"{valor_max:.2f}%".replace(".", ","),
        "tendencia_alta": valores[-1] >= valores[0],
    }


COLUNAS_HISTORICO_ATUALIZACOES = [
    "Data/hora", "Total de ativos", "Valor investido (R$)", "Valor atual (R$)",
    "Lucro/Perda (R$)", "Lucro/Perda (%)",
]


def _linha_historico_atualizacao(registro: RegistroAtualizacaoCarteira) -> list:
    return [
        timezone.localtime(registro.criado_em),
        registro.total_ativos,
        float(registro.valor_investido),
        float(registro.valor_atual),
        float(registro.lucro_perda),
        float(registro.lucro_perda_pct) if registro.lucro_perda_pct is not None else None,
    ]


def gerar_excel_historico_atualizacoes(registros: list[RegistroAtualizacaoCarteira]) -> bytes:
    """Gera uma planilha .xlsx com o histórico de atualizações da carteira do usuário, pronta para download."""
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Histórico de atualizações"

    ws.append(["Histórico de Atualizações"])
    ws.append([f"Gerado em {timezone.localtime().strftime('%d/%m/%Y %H:%M')}"])
    ws.append([])
    ws.append(COLUNAS_HISTORICO_ATUALIZACOES)
    for celula in ws[4]:
        celula.font = Font(bold=True, color="FFFFFF")
        celula.fill = PatternFill("solid", fgColor="0D1526")
        celula.alignment = Alignment(horizontal="center")

    for registro in registros:
        linha = _linha_historico_atualizacao(registro)
        linha[0] = linha[0].strftime("%d/%m/%Y %H:%M")
        ws.append(linha)

    for indice in range(1, len(COLUNAS_HISTORICO_ATUALIZACOES) + 1):
        ws.column_dimensions[get_column_letter(indice)].width = 19
    ws.freeze_panes = "A5"

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def gerar_pdf_historico_atualizacoes(registros: list[RegistroAtualizacaoCarteira], nome_usuario: str) -> bytes:
    """Gera um PDF com o histórico de atualizações da carteira do usuário, pronto para download."""
    import io
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    nome_usuario = escape(nome_usuario)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4),
        topMargin=1.5 * cm, bottomMargin=1.5 * cm, leftMargin=1.2 * cm, rightMargin=1.2 * cm,
    )
    estilos = getSampleStyleSheet()

    elementos = [
        Paragraph("BolsaTrader - Histórico de Atualizações", estilos["Title"]),
        Paragraph(
            f"{nome_usuario} - gerado em {timezone.localtime().strftime('%d/%m/%Y %H:%M')}",
            estilos["Normal"],
        ),
        Spacer(1, 0.5 * cm),
    ]

    def fmt(valor, sufixo="", quando_none="—"):
        return f"{valor:.2f}{sufixo}" if valor is not None else quando_none

    if not registros:
        elementos.append(Paragraph("Nenhum registro de atualização até o momento.", estilos["Normal"]))
    else:
        dados = [COLUNAS_HISTORICO_ATUALIZACOES]
        for registro in registros:
            linha = _linha_historico_atualizacao(registro)
            dados.append([
                linha[0].strftime("%d/%m/%Y %H:%M"), str(linha[1]), fmt(linha[2]), fmt(linha[3]),
                fmt(linha[4]), fmt(linha[5], "%"),
            ])

        tabela = Table(dados, repeatRows=1)
        tabela.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0D1526")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef2f7")]),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        elementos.append(tabela)

    doc.build(elementos)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Concentração por setor
# --------------------------------------------------------------------------
def calcular_concentracao_setor(posicoes: list) -> list[dict]:
    """
    Agrupa o valor atual das posições compradas (reservas não entram) por
    setor do ativo (ver Ativo.setor, preenchido em atualizar_cotacao_diaria)
    e calcula o percentual de cada setor sobre o valor total da carteira -
    ativos sem setor conhecido entram em "Sem setor". Ordenado do maior para
    o menor percentual.

    O limite de ativos diferentes em carteira (MAX_ATIVOS_EM_CARTEIRA) não
    enxerga isso: dá pra estar "diversificado" em 10 tickers e todos do mesmo
    setor - esta função é o que permite avisar sobre esse tipo de risco.
    """
    valores_por_setor = defaultdict(lambda: Decimal("0"))
    valor_total = Decimal("0")
    for p in posicoes:
        if p.apenas_reservado or p.valor_atual is None:
            continue
        setor = p.ativo.setor or "Sem setor"
        valores_por_setor[setor] += p.valor_atual
        valor_total += p.valor_atual

    if not valor_total:
        return []

    concentracao = [
        {"setor": setor, "valor": valor, "percentual": (valor / valor_total * 100).quantize(Decimal("0.01"))}
        for setor, valor in valores_por_setor.items()
    ]
    concentracao.sort(key=lambda item: item["percentual"], reverse=True)
    return concentracao


# --------------------------------------------------------------------------
# Métricas de risco: volatilidade, drawdown e concentração por ativo
#
# Complementam o lucro/perda: duas carteiras com o mesmo retorno podem ter
# risco bem diferente - uma diversificada em vários ativos estáveis, outra
# concentrada numa única posição muito volátil. O percentual de lucro/perda
# sozinho não distingue os dois casos.
# --------------------------------------------------------------------------
DIAS_PREGAO_POR_ANO = 252
LIMIAR_CONCENTRACAO_ALERTA_PCT = Decimal("40")


def _volatilidade_e_drawdown_precos(precos_cronologico: list[float]) -> dict:
    """
    Núcleo puro (sem acesso ao banco) do cálculo de volatilidade anualizada e
    drawdown máximo a partir de uma lista de preços em ordem cronológica
    (mais antigo primeiro). Extraído de calcular_metricas_risco para ser
    reaproveitado pelo Scanner Técnico (ver analisar_volatilidade_precos),
    sem duplicar a fórmula. Ambos ficam None sem histórico suficiente
    (< 3 preços).

    Volatilidade: desvio padrão dos retornos diários no período, anualizado
    (× √252, dias de pregão por ano) - maior valor = preço oscila mais no dia
    a dia. Drawdown máximo: maior queda percentual do topo ao fundo dentro da
    mesma janela.
    """
    if len(precos_cronologico) < 3:
        return {"volatilidade_pct": None, "drawdown_pct": None}

    retornos = [
        (atual - anterior) / anterior
        for anterior, atual in zip(precos_cronologico, precos_cronologico[1:])
        if anterior
    ]
    volatilidade_pct = (
        round(statistics.pstdev(retornos) * (DIAS_PREGAO_POR_ANO ** 0.5) * 100, 2)
        if len(retornos) >= 2
        else None
    )

    pico = precos_cronologico[0]
    maior_queda = 0.0
    for preco in precos_cronologico:
        pico = max(pico, preco)
        if pico:
            maior_queda = min(maior_queda, (preco - pico) / pico)
    drawdown_pct = round(maior_queda * 100, 2)

    return {"volatilidade_pct": volatilidade_pct, "drawdown_pct": drawdown_pct}


def calcular_metricas_risco(posicoes: list, dias_janela: int = 30) -> dict:
    """
    Para cada posição comprada, calcula a volatilidade anualizada e o
    drawdown máximo dos últimos `dias_janela` pregões, mais o quanto aquela
    posição representa do valor atual total da carteira - e, a partir disso,
    a maior concentração individual e um índice de Herfindahl simples (soma
    dos quadrados das participações: quanto mais perto de 1, mais concentrada
    a carteira; quanto mais perto de 1/nº de ativos, mais diversificada).

    Volatilidade: desvio padrão dos retornos diários no período, anualizado
    (× √252, dias de pregão por ano) - maior valor = preço oscila mais no dia
    a dia. Drawdown máximo: maior queda percentual do topo ao fundo dentro da
    mesma janela. Ambos ficam None sem histórico suficiente (< 3 pregões).
    """
    compradas = [p for p in posicoes if not p.apenas_reservado and p.quantidade > 0]
    valor_total = sum((p.valor_atual for p in compradas if p.valor_atual is not None), Decimal("0"))

    por_ativo = []
    for p in compradas:
        historico_desc = list(
            p.ativo.cotacoes.order_by("-data").values_list("preco_fechamento", flat=True)[:dias_janela]
        )
        precos = [float(v) for v in reversed(historico_desc)]  # cronológico: mais antigo -> mais recente

        metricas = _volatilidade_e_drawdown_precos(precos)
        volatilidade_pct = metricas["volatilidade_pct"]
        drawdown_pct = metricas["drawdown_pct"]

        percentual_carteira = (
            (p.valor_atual / valor_total * 100).quantize(Decimal("0.01"))
            if p.valor_atual is not None and valor_total
            else None
        )
        por_ativo.append({
            "ativo": p.ativo,
            "volatilidade_pct": volatilidade_pct,
            "drawdown_pct": drawdown_pct,
            "percentual_carteira": percentual_carteira,
        })

    por_ativo.sort(key=lambda item: item["percentual_carteira"] or Decimal("0"), reverse=True)
    maior_posicao = por_ativo[0] if por_ativo else None
    indice_herfindahl = sum((float(item["percentual_carteira"] or 0) / 100) ** 2 for item in por_ativo)

    return {
        "por_ativo": por_ativo,
        "maior_posicao_ativo": maior_posicao["ativo"] if maior_posicao else None,
        "maior_posicao_pct": maior_posicao["percentual_carteira"] if maior_posicao else None,
        "concentracao_alta": bool(
            maior_posicao and maior_posicao["percentual_carteira"] is not None
            and maior_posicao["percentual_carteira"] >= LIMIAR_CONCENTRACAO_ALERTA_PCT
        ),
        "indice_herfindahl": round(indice_herfindahl, 3),
    }


# --------------------------------------------------------------------------
# Gráfico comparativo: valor de compra x valor atual no histórico (por ativo)
# --------------------------------------------------------------------------
def _fmt_reais(valor: float) -> str:
    return f"R$ {valor:,.2f}".replace(",", "#").replace(".", ",").replace("#", ".")


def construir_comparativo_valores(
    posicoes: list[Posicao], dias: int = 30, largura: int = 480, altura: int = 180, padding: int = 40
) -> list[dict]:
    """
    Para cada posição realmente comprada (exclui reservas), monta um gráfico
    de linha com duas séries: o valor de compra (linha de referência, constante
    - é o quanto você pagou) e o valor atual ao longo do histórico de cotações
    (quantidade em carteira x preço de fechamento em cada pregão).

    Cada ativo tem seu próprio gráfico (duas linhas). Ativos sem histórico de
    cotações suficiente (menos de 2 pregões) ficam de fora.
    """
    graficos = []
    for p in posicoes:
        if p.apenas_reservado or p.quantidade <= 0:
            continue

        historico_desc = list(p.ativo.cotacoes.order_by("-data")[:dias])
        if len(historico_desc) < 2:
            continue
        historico = list(reversed(historico_desc))  # cronológico: mais antigo -> mais recente

        valor_compra = float(p.valor_investido)
        valores_atual = [float(p.quantidade) * float(c.preco_fechamento) for c in historico]

        maior_valor = max(max(valores_atual), valor_compra)
        menor_valor = min(min(valores_atual), valor_compra)
        faixa = (maior_valor - menor_valor) or (valor_compra or 1.0)
        maior_valor += faixa * 0.12
        menor_valor -= faixa * 0.12
        faixa = maior_valor - menor_valor

        y_topo = 16
        y_base = altura - padding
        plot_altura = y_base - y_topo
        plot_largura = largura - padding * 2
        passo_x = plot_largura / (len(historico) - 1)

        def escala_y(valor, _menor=menor_valor, _faixa=faixa, _base=y_base, _alt=plot_altura):
            return round(_base - ((valor - _menor) / _faixa) * _alt, 2)

        comandos_atual, pontos = [], []
        for i, (cotacao, valor) in enumerate(zip(historico, valores_atual)):
            x = round(padding + i * passo_x, 2)
            y = escala_y(valor)
            comandos_atual.append(f"{'M' if i == 0 else 'L'}{x} {y}")
            diferenca = valor - valor_compra
            pontos.append({
                "x": x,
                "y": y,
                "data_label": cotacao.data.strftime("%d/%m/%Y"),
                "valor_compra_label": _fmt_reais(valor_compra),
                "valor_atual_label": _fmt_reais(valor),
                "diferenca_label": f"{'+' if diferenca >= 0 else ''}{_fmt_reais(diferenca)}",
                "ganho": valor >= valor_compra,
            })

        y_compra = escala_y(valor_compra)

        eixo_y = []
        for fracao in (0, 1 / 3, 2 / 3, 1):
            valor = menor_valor + fracao * faixa
            y = escala_y(valor)
            eixo_y.append({"y": y, "label_y": round(y + 4, 2), "label": f"R$ {valor:,.0f}".replace(",", ".")})

        graficos.append({
            "ativo": p.ativo,
            "id_svg": f"pontos-comparativo-{p.ativo.id}",
            "largura": largura,
            "altura": altura,
            "padding": padding,
            "x_inicio": padding,
            "x_fim": largura - padding,
            "y_topo": y_topo,
            "y_base": y_base,
            "y_compra": y_compra,
            "path_compra": f"M{padding} {y_compra} L{largura - padding} {y_compra}",
            "path_atual": " ".join(comandos_atual),
            "pontos": pontos,
            "eixo_y": eixo_y,
            "ganho_atual": valores_atual[-1] >= valor_compra,
            "primeira_data": historico[0].data.strftime("%d/%m/%Y"),
            "ultima_data": historico[-1].data.strftime("%d/%m/%Y"),
        })

    return graficos


# --------------------------------------------------------------------------
# Exportação das posições em carteira (Excel / PDF)
# --------------------------------------------------------------------------
COLUNAS_EXPORTACAO_POSICOES = [
    "Ativo", "Quantidade", "Preço médio (R$)", "Valor investido (R$)",
    "Cotação atual (R$)", "Valor atual (R$)", "Lucro/Perda (R$)", "Lucro/Perda (%)",
    "Meta lucro (%)", "Meta perda (%)", "Tendência", "Dias em carteira",
]


def _linha_exportacao_posicao(p: Posicao) -> list:
    return [
        p.ativo.ticker,
        p.quantidade,
        float(p.preco_medio),
        float(p.valor_investido),
        float(p.preco_atual) if p.preco_atual is not None else None,
        float(p.valor_atual) if p.valor_atual is not None else None,
        float(p.lucro_perda_valor) if p.lucro_perda_valor is not None else None,
        float(p.lucro_perda_pct) if p.lucro_perda_pct is not None else None,
        float(p.meta_lucro_pct) if p.meta_lucro_pct is not None else None,
        float(p.meta_perda_pct) if p.meta_perda_pct is not None else None,
        p.tendencia or "",
        p.dias_desde_compra,
    ]


def gerar_excel_posicoes(
    posicoes: list[Posicao],
    comparativo_benchmark: dict | None = None,
    concentracao_setor: list[dict] | None = None,
    metricas_risco: dict | None = None,
) -> bytes:
    """
    Gera uma planilha .xlsx com as posições em carteira, pronta para
    download - a aba "Posições" com a grid principal, mais três abas
    espelhando as análises da tela (ver templates/core/posicoes.html):
    "Carteira x Mercado" (comparativo_benchmark), "Concentração por setor"
    e "Indicadores de risco". As três últimas são omitidas (recebem só uma
    linha de aviso) quando a análise correspondente ainda não tem dados.
    """
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Posições"

    ws.append(COLUNAS_EXPORTACAO_POSICOES)
    for celula in ws[1]:
        celula.font = Font(bold=True, color="FFFFFF")
        celula.fill = PatternFill("solid", fgColor="0D1526")
        celula.alignment = Alignment(horizontal="center")

    for p in posicoes:
        ws.append(_linha_exportacao_posicao(p))

    for indice in range(1, len(COLUNAS_EXPORTACAO_POSICOES) + 1):
        ws.column_dimensions[get_column_letter(indice)].width = 17
    ws.freeze_panes = "A2"

    def cabecalho(ws, colunas):
        ws.append(colunas)
        for celula in ws[ws.max_row]:
            celula.font = Font(bold=True, color="FFFFFF")
            celula.fill = PatternFill("solid", fgColor="0D1526")
            celula.alignment = Alignment(horizontal="center")
        for indice in range(1, len(colunas) + 1):
            ws.column_dimensions[get_column_letter(indice)].width = 22

    ws_benchmark = wb.create_sheet("Carteira x Mercado")
    if comparativo_benchmark:
        c = comparativo_benchmark
        ws_benchmark.append([
            f"Desde {c['data_inicio'].strftime('%d/%m/%Y')} até {c['data_fim'].strftime('%d/%m/%Y')} "
            f"({c['dias_periodo']} dias)"
        ])
        ws_benchmark.append([])
        cabecalho(ws_benchmark, ["Indicador", "Retorno no período (%)"])
        ws_benchmark.append(["Sua carteira", float(c["retorno_carteira_pct"])])
        ws_benchmark.append([
            "Ibovespa", float(c["retorno_ibovespa_pct"]) if c["retorno_ibovespa_pct"] is not None else "sem dados"
        ])
        ws_benchmark.append([
            "CDI", float(c["retorno_cdi_pct"]) if c["retorno_cdi_pct"] is not None else "sem dados"
        ])
        ws_benchmark.append([])
        ws_benchmark.append(["Bateu o Ibovespa no período?", "Sim" if c["carteira_bateu_ibovespa"] else "Não"])
        ws_benchmark.append(["Bateu o CDI no período?", "Sim" if c["carteira_bateu_cdi"] else "Não"])
    else:
        ws_benchmark.append(["Sem dados suficientes para comparar com o mercado no momento."])

    ws_setor = wb.create_sheet("Concentração por setor")
    if concentracao_setor:
        cabecalho(ws_setor, ["Setor", "Valor (R$)", "% da carteira"])
        for item in concentracao_setor:
            ws_setor.append([item["setor"], float(item["valor"]), float(item["percentual"])])
    else:
        ws_setor.append(["Sem dados de concentração por setor no momento."])

    ws_risco = wb.create_sheet("Indicadores de risco")
    if metricas_risco and metricas_risco.get("por_ativo"):
        ws_risco.append([f"Índice de concentração (Herfindahl): {metricas_risco['indice_herfindahl']}"])
        ws_risco.append([])
        cabecalho(ws_risco, ["Ativo", "% da carteira", "Volatilidade anualizada (%)", "Drawdown máximo (%)"])
        for item in metricas_risco["por_ativo"]:
            ws_risco.append([
                item["ativo"].ticker,
                float(item["percentual_carteira"]) if item["percentual_carteira"] is not None else None,
                item["volatilidade_pct"],
                item["drawdown_pct"],
            ])
    else:
        ws_risco.append(["Sem indicadores de risco no momento."])

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def gerar_pdf_posicoes(
    posicoes: list[Posicao],
    nome_usuario: str,
    comparativo_benchmark: dict | None = None,
    concentracao_setor: list[dict] | None = None,
    metricas_risco: dict | None = None,
) -> bytes:
    """
    Gera um PDF (paisagem) com as posições em carteira, pronto para
    download - a tabela principal, mais três seções espelhando as análises
    da tela (ver templates/core/posicoes.html): "Carteira x Mercado"
    (comparativo_benchmark), "Concentração por setor" e "Indicadores de
    risco". As três últimas mostram um aviso quando a análise
    correspondente ainda não tem dados, em vez de sumir da página.
    """
    import io
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    nome_usuario = escape(nome_usuario)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4),
        topMargin=1.5 * cm, bottomMargin=1.5 * cm, leftMargin=1.2 * cm, rightMargin=1.2 * cm,
    )
    estilos = getSampleStyleSheet()

    elementos = [
        Paragraph("BolsaTrader - Posições em Carteira", estilos["Title"]),
        Paragraph(
            f"{nome_usuario} - gerado em {timezone.localtime().strftime('%d/%m/%Y %H:%M')}",
            estilos["Normal"],
        ),
        Spacer(1, 0.5 * cm),
    ]

    def fmt(valor, sufixo="", quando_none="—"):
        return f"{valor:.2f}{sufixo}" if valor is not None else quando_none

    dados = [COLUNAS_EXPORTACAO_POSICOES]
    for p in posicoes:
        l = _linha_exportacao_posicao(p)
        dados.append([
            l[0], str(l[1]), fmt(l[2]), fmt(l[3]), fmt(l[4]), fmt(l[5]), fmt(l[6]), fmt(l[7], "%"),
            fmt(l[8], "%", "padrão"), fmt(l[9], "%", "padrão"), l[10] or "—",
            "hoje" if l[11] == 0 else (f"{l[11]} dias" if l[11] is not None else "—"),
        ])

    if len(dados) == 1:
        elementos.append(Paragraph("Nenhuma posição em carteira.", estilos["Normal"]))
    else:
        tabela = Table(dados, repeatRows=1)
        tabela.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0D1526")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef2f7")]),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        elementos.append(tabela)

    def estilo_tabela_secao():
        return TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0D1526")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef2f7")]),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ])

    elementos.append(Spacer(1, 0.8 * cm))
    elementos.append(Paragraph("Sua carteira x mercado", estilos["Heading2"]))
    if comparativo_benchmark:
        c = comparativo_benchmark
        elementos.append(Paragraph(
            escape(
                f"Desde {c['data_inicio'].strftime('%d/%m/%Y')} até {c['data_fim'].strftime('%d/%m/%Y')} "
                f"({c['dias_periodo']} dias)"
            ),
            estilos["Normal"],
        ))
        elementos.append(Spacer(1, 0.2 * cm))
        dados_benchmark = [
            ["Indicador", "Retorno no período (%)"],
            ["Sua carteira", fmt(float(c["retorno_carteira_pct"]), "%")],
            ["Ibovespa", fmt(float(c["retorno_ibovespa_pct"]), "%") if c["retorno_ibovespa_pct"] is not None else "sem dados"],
            ["CDI", fmt(float(c["retorno_cdi_pct"]), "%") if c["retorno_cdi_pct"] is not None else "sem dados"],
        ]
        tabela_benchmark = Table(dados_benchmark, repeatRows=1, colWidths=[6 * cm, 6 * cm])
        tabela_benchmark.setStyle(estilo_tabela_secao())
        elementos.append(tabela_benchmark)
        elementos.append(Spacer(1, 0.2 * cm))
        elementos.append(Paragraph(
            ("✅" if c["carteira_bateu_ibovespa"] else "⚠️") + " Sua carteira "
            + ("bateu" if c["carteira_bateu_ibovespa"] else "não bateu") + " o Ibovespa no período. "
            + ("✅" if c["carteira_bateu_cdi"] else "⚠️") + " Sua carteira "
            + ("bateu" if c["carteira_bateu_cdi"] else "não bateu") + " o CDI no período.",
            estilos["Normal"],
        ))
    else:
        elementos.append(Paragraph("Sem dados suficientes para comparar com o mercado no momento.", estilos["Normal"]))

    elementos.append(Spacer(1, 0.8 * cm))
    elementos.append(Paragraph("Concentração por setor", estilos["Heading2"]))
    if concentracao_setor:
        dados_setor = [["Setor", "Valor (R$)", "% da carteira"]]
        for item in concentracao_setor:
            dados_setor.append([item["setor"], fmt(float(item["valor"])), fmt(float(item["percentual"]), "%")])
        tabela_setor = Table(dados_setor, repeatRows=1, colWidths=[8 * cm, 5 * cm, 5 * cm])
        tabela_setor.setStyle(estilo_tabela_secao())
        elementos.append(tabela_setor)
    else:
        elementos.append(Paragraph("Sem dados de concentração por setor no momento.", estilos["Normal"]))

    elementos.append(Spacer(1, 0.8 * cm))
    elementos.append(Paragraph("Indicadores de risco", estilos["Heading2"]))
    if metricas_risco and metricas_risco.get("por_ativo"):
        elementos.append(Paragraph(
            escape(f"Índice de concentração (Herfindahl): {metricas_risco['indice_herfindahl']}"),
            estilos["Normal"],
        ))
        elementos.append(Spacer(1, 0.2 * cm))
        dados_risco = [["Ativo", "% da carteira", "Volatilidade anualizada (%)", "Drawdown máximo (%)"]]
        for item in metricas_risco["por_ativo"]:
            dados_risco.append([
                item["ativo"].ticker,
                fmt(float(item["percentual_carteira"])) if item["percentual_carteira"] is not None else "—",
                fmt(item["volatilidade_pct"], "%", "aguardando histórico"),
                fmt(item["drawdown_pct"], "%", "aguardando histórico"),
            ])
        tabela_risco = Table(dados_risco, repeatRows=1)
        tabela_risco.setStyle(estilo_tabela_secao())
        elementos.append(tabela_risco)
    else:
        elementos.append(Paragraph("Sem indicadores de risco no momento.", estilos["Normal"]))

    doc.build(elementos)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Exportação das operações (Excel / PDF)
#
# Reflete as três grids da página "Minhas Operações" (ver core.views.
# operacao_lista): em carteira, reservadas e vendidas - cada uma com suas
# próprias colunas, igual à tela - em vez de uma tabela genérica única.
# --------------------------------------------------------------------------
COLUNAS_EM_CARTEIRA = [
    "Data", "Dias", "Ativo", "Qtd. comprada", "Preço compra (R$)", "Total compra (R$)",
    "Qtd. vendida", "Preço venda (R$)", "Total venda (R$)", "Saldo",
    "Lucro/Perda realizado (R$)", "Lucro/Perda realizado (%)", "Meta lucro (%)", "Meta perda (%)",
]

COLUNAS_RESERVADAS = [
    "Data", "Dias", "Ativo", "Quantidade", "Preço pretendido (R$)", "Total pretendido (R$)",
    "Variação desde a reserva (%)", "Meta lucro (%)", "Meta perda (%)",
]

COLUNAS_VENDIDAS = [
    "Data compra", "Data venda", "Dias em carteira", "Ativo", "Qtd. comprada",
    "Preço compra (R$)", "Total compra (R$)", "Preço venda (R$)", "Total venda (R$)",
    "Corretora (%)", "Valor líquido a receber (R$)",
    "Lucro/Perda realizado (R$)", "Lucro/Perda realizado (%)",
    "Lucro/Perda por dia (R$)", "Lucro/Perda por dia (%)",
    "Meta lucro (%)", "Meta perda (%)",
]

# Índices (0-based) das colunas numéricas que entram na linha de "TOTAL" no
# fim de cada tabela do relatório - preço unitário, percentuais e metas não
# somam (não fazem sentido agregados), só quantidades e valores em R$.
INDICES_SOMA_EM_CARTEIRA = {3, 5, 6, 8, 9, 10}
INDICES_SOMA_RESERVADAS = {3, 5}
INDICES_SOMA_VENDIDAS = {4, 6, 8, 10, 11}


def _linha_totais(linhas: list[list], indices_soma: set[int], num_colunas: int) -> list:
    """Linha de totais (soma das colunas numéricas indicadas) para o fim de uma tabela do relatório."""
    totais = [None] * num_colunas
    for indice in indices_soma:
        valores = [linha[indice] for linha in linhas if linha[indice] is not None]
        if valores:
            totais[indice] = round(sum(valores), 2)
    return totais


def _linha_em_carteira(op: Operacao) -> list:
    return [
        op.data_operacao,
        op.dias_desde_operacao,
        op.ativo.ticker,
        op.quantidade,
        float(op.preco_unitario),
        float(op.valor_total),
        op.quantidade_vendida,
        float(op.preco_venda) if op.preco_venda is not None else None,
        float(op.valor_total_vendido) if op.valor_total_vendido is not None else None,
        op.saldo,
        float(op.lucro_perda_realizado) if op.lucro_perda_realizado is not None else None,
        float(op.lucro_perda_pct_realizado) if op.lucro_perda_pct_realizado is not None else None,
        float(op.meta_lucro_pct) if op.meta_lucro_pct is not None else None,
        float(op.meta_perda_pct) if op.meta_perda_pct is not None else None,
    ]


def _linha_reservada(op: Operacao) -> list:
    return [
        op.data_operacao,
        op.dias_desde_operacao,
        op.ativo.ticker,
        op.quantidade,
        float(op.preco_unitario),
        float(op.valor_total),
        float(op.variacao_pct_reserva) if op.variacao_pct_reserva is not None else None,
        float(op.meta_lucro_pct) if op.meta_lucro_pct is not None else None,
        float(op.meta_perda_pct) if op.meta_perda_pct is not None else None,
    ]


def _linha_vendida(op: Operacao) -> list:
    return [
        op.data_operacao,
        op.data_venda,
        op.dias_em_carteira_ate_venda,
        op.ativo.ticker,
        op.quantidade,
        float(op.preco_unitario),
        float(op.valor_total),
        float(op.preco_venda) if op.preco_venda is not None else None,
        float(op.valor_total_vendido) if op.valor_total_vendido is not None else None,
        float(op.percentual_corretora) if op.percentual_corretora is not None else None,
        float(op.valor_liquido_vendido) if op.valor_liquido_vendido is not None else None,
        float(op.lucro_perda_realizado) if op.lucro_perda_realizado is not None else None,
        float(op.lucro_perda_pct_realizado) if op.lucro_perda_pct_realizado is not None else None,
        float(op.lucro_perda_por_dia) if op.lucro_perda_por_dia is not None else None,
        float(op.lucro_perda_pct_por_dia) if op.lucro_perda_pct_por_dia is not None else None,
        float(op.meta_lucro_pct) if op.meta_lucro_pct is not None else None,
        float(op.meta_perda_pct) if op.meta_perda_pct is not None else None,
    ]


def gerar_excel_operacoes(
    operacoes_compradas: list[Operacao],
    operacoes_vendidas: list[Operacao],
    operacoes_reservadas: list[Operacao],
    periodo_label_carteira: str,
    periodo_label_vendidas: str,
    periodo_label_reservadas: str,
) -> bytes:
    """
    Gera uma planilha .xlsx com as operações do usuário, pronta para download -
    uma aba para cada grid da página (em carteira, vendidas, reservadas), cada
    uma com o período do seu próprio filtro indicado no topo (Vendidas filtra
    pela data da venda, as outras duas pela data da operação).
    """
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()

    def montar_aba(ws, titulo: str, periodo_label: str, colunas: list[str], linhas: list[list], indices_soma: set[int]):
        ws.append([titulo])
        ws.append([periodo_label])
        ws.append([])
        ws.append(colunas)
        for celula in ws[4]:
            celula.font = Font(bold=True, color="FFFFFF")
            celula.fill = PatternFill("solid", fgColor="0D1526")
            celula.alignment = Alignment(horizontal="center")

        for linha in linhas:
            ws.append(linha)

        if linhas:
            totais = _linha_totais(linhas, indices_soma, len(colunas))
            totais[0] = "TOTAL"
            ws.append(totais)
            for celula in ws[ws.max_row]:
                celula.font = Font(bold=True)
                celula.fill = PatternFill("solid", fgColor="E4E9F2")

        for indice in range(1, len(colunas) + 1):
            ws.column_dimensions[get_column_letter(indice)].width = 17
        ws.freeze_panes = "A5"

    ws_carteira = wb.active
    ws_carteira.title = "Em carteira"
    montar_aba(
        ws_carteira, "Em carteira", periodo_label_carteira, COLUNAS_EM_CARTEIRA,
        [_linha_em_carteira(op) for op in operacoes_compradas], INDICES_SOMA_EM_CARTEIRA,
    )

    ws_vendidas = wb.create_sheet("Vendidas")
    montar_aba(
        ws_vendidas, "Vendidas", periodo_label_vendidas, COLUNAS_VENDIDAS,
        [_linha_vendida(op) for op in operacoes_vendidas], INDICES_SOMA_VENDIDAS,
    )

    ws_reservadas = wb.create_sheet("Reservadas")
    montar_aba(
        ws_reservadas, "Reservadas", periodo_label_reservadas, COLUNAS_RESERVADAS,
        [_linha_reservada(op) for op in operacoes_reservadas], INDICES_SOMA_RESERVADAS,
    )

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def gerar_pdf_operacoes(
    operacoes_compradas: list[Operacao],
    operacoes_vendidas: list[Operacao],
    operacoes_reservadas: list[Operacao],
    periodo_label_carteira: str,
    periodo_label_vendidas: str,
    periodo_label_reservadas: str,
    nome_usuario: str,
) -> bytes:
    """
    Gera um PDF (paisagem) com as operações do usuário, pronto para download -
    uma tabela para cada grid da página (em carteira, vendidas, reservadas),
    cada uma com o período do seu próprio filtro (Vendidas filtra pela data
    da venda, as outras duas pela data da operação) indicado logo abaixo do
    título da seção.
    """
    import io
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    nome_usuario = escape(nome_usuario)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4),
        topMargin=1.5 * cm, bottomMargin=1.5 * cm, leftMargin=1.2 * cm, rightMargin=1.2 * cm,
    )
    estilos = getSampleStyleSheet()

    elementos = [
        Paragraph("BolsaTrader - Minhas Operações", estilos["Title"]),
        Paragraph(
            f"{nome_usuario} - gerado em {timezone.localtime().strftime('%d/%m/%Y %H:%M')}",
            estilos["Normal"],
        ),
        Spacer(1, 0.5 * cm),
    ]

    def fmt(valor, sufixo="", quando_none="—"):
        return f"{valor:.2f}{sufixo}" if valor is not None else quando_none

    def fmt_data(valor):
        return valor.strftime("%d/%m/%Y") if valor is not None else "—"

    def fmt_dias(valor):
        if valor is None:
            return "—"
        return "hoje" if valor == 0 else f"{valor} dias"

    from reportlab.lib.styles import ParagraphStyle

    estilo_cabecalho = ParagraphStyle(
        "CabecalhoTabela", fontSize=6.5, leading=8, textColor=colors.white, alignment=1,
    )
    estilo_celula = ParagraphStyle("CelulaTabela", fontSize=6.5, leading=8, alignment=1)
    estilo_celula_total = ParagraphStyle(
        "CelulaTotal", fontSize=6.5, leading=8, alignment=1, fontName="Helvetica-Bold",
    )

    def adicionar_secao(
        titulo: str, periodo_label: str, colunas: list[str], linhas: list[list], formatadores, indices_soma: set[int]
    ):
        elementos.append(Paragraph(titulo, estilos["Heading2"]))
        elementos.append(Paragraph(escape(periodo_label), estilos["Normal"]))
        elementos.append(Spacer(1, 0.2 * cm))
        if not linhas:
            elementos.append(Paragraph("Nenhuma operação nesta situação no período.", estilos["Normal"]))
            elementos.append(Spacer(1, 0.4 * cm))
            return

        # células viram Paragraph (em vez de texto puro) para quebrar linha em
        # vez de alargar a coluna - com muitas colunas, texto puro faz a
        # tabela ficar mais larga que a página (estoura a margem direita).
        dados = [[Paragraph(str(c), estilo_cabecalho) for c in colunas]]
        for linha in linhas:
            dados.append([
                Paragraph(str(f(v)), estilo_celula) for f, v in zip(formatadores, linha)
            ])

        totais = _linha_totais(linhas, indices_soma, len(colunas))
        linha_totais = [Paragraph("TOTAL", estilo_celula_total)]
        for f, v in list(zip(formatadores, totais))[1:]:
            texto = f(v) if v is not None else "—"
            linha_totais.append(Paragraph(str(texto), estilo_celula_total))
        dados.append(linha_totais)

        largura_coluna = doc.width / len(colunas)
        tabela = Table(dados, colWidths=[largura_coluna] * len(colunas), repeatRows=1)
        tabela.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0D1526")),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#d7deec")),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#eef2f7")]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ]))
        elementos.append(tabela)
        elementos.append(Spacer(1, 0.6 * cm))

    identidade = lambda v: v if v is not None else "—"

    adicionar_secao(
        "Em carteira", periodo_label_carteira, COLUNAS_EM_CARTEIRA,
        [_linha_em_carteira(op) for op in operacoes_compradas],
        [fmt_data, fmt_dias, identidade, identidade, fmt, fmt, identidade, fmt, fmt, identidade,
         fmt, lambda v: fmt(v, "%"), lambda v: fmt(v, "%", "padrão"), lambda v: fmt(v, "%", "padrão")],
        INDICES_SOMA_EM_CARTEIRA,
    )
    adicionar_secao(
        "Vendidas", periodo_label_vendidas, COLUNAS_VENDIDAS,
        [_linha_vendida(op) for op in operacoes_vendidas],
        [fmt_data, fmt_data, fmt_dias, identidade, identidade, fmt, fmt, fmt, fmt,
         lambda v: fmt(v, "%"), fmt, fmt, lambda v: fmt(v, "%"), fmt, lambda v: fmt(v, "%"),
         lambda v: fmt(v, "%", "padrão"), lambda v: fmt(v, "%", "padrão")],
        INDICES_SOMA_VENDIDAS,
    )
    adicionar_secao(
        "Reservadas", periodo_label_reservadas, COLUNAS_RESERVADAS,
        [_linha_reservada(op) for op in operacoes_reservadas],
        [fmt_data, fmt_dias, identidade, identidade, fmt, fmt,
         lambda v: fmt(v, "%"), lambda v: fmt(v, "%", "padrão"), lambda v: fmt(v, "%", "padrão")],
        INDICES_SOMA_RESERVADAS,
    )

    doc.build(elementos)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Gráfico de acompanhamento (linha simples com o histórico de fechamento)
# --------------------------------------------------------------------------
def construir_grafico_cotacoes(
    historico_desc: list[Cotacao], largura: int = 560, altura: int = 160, padding: int = 26,
    id_svg_sufixo: str = "",
) -> dict | None:
    """
    Monta os dados de um gráfico de linha (SVG) com o histórico de preços de
    fechamento de um ativo, para o acompanhamento visual na página de Análise
    de Mercado. Recebe o histórico já carregado (mais recente primeiro, mesma
    lista usada na tabela) para não repetir consulta ao banco.

    `id_svg_sufixo` evita ids de SVG duplicados na mesma página quando o
    mesmo ativo aparece em mais de um gráfico (ex: o gráfico "desde a
    compra" e o gráfico "desde a reserva" do mesmo ativo, em Histórico e
    Gráficos) - ids duplicados quebrariam o crosshair/tooltip (ver grafico.js,
    que busca os pontos por getElementById).

    Retorna None quando não há pontos suficientes para traçar uma linha.
    """
    cotacoes = list(reversed(historico_desc))  # cronológico: mais antigo -> mais recente
    if len(cotacoes) < 2:
        return None

    precos = [float(c.preco_fechamento) for c in cotacoes]
    preco_min, preco_max = min(precos), max(precos)
    faixa = (preco_max - preco_min) or 1.0

    plot_largura = largura - (padding * 2)
    plot_altura = altura - (padding * 2)
    passo_x = plot_largura / (len(cotacoes) - 1)

    pontos = []
    comandos_path = []
    for i, (cotacao, preco) in enumerate(zip(cotacoes, precos)):
        x = round(padding + i * passo_x, 2)
        y = round(padding + (1 - (preco - preco_min) / faixa) * plot_altura, 2)
        comandos_path.append(f"{'M' if i == 0 else 'L'}{x} {y}")

        variacao = cotacao.variacao_dia_pct
        pontos.append({
            "x": x,
            "y": y,
            "data_label": cotacao.data.strftime("%d/%m/%Y"),
            "preco_label": f"R$ {preco:.2f}".replace(".", ","),
            "variacao_label": f"{'+' if variacao and variacao > 0 else ''}{variacao}%".replace(".", ",")
            if variacao is not None
            else None,
            "variacao_positiva": bool(variacao and variacao >= 0),
        })

    return {
        "id_svg": f"pontos-cotacoes-{cotacoes[0].ativo_id}{id_svg_sufixo}",
        "largura": largura,
        "altura": altura,
        "padding": padding,
        "x_inicio": padding,
        "x_fim": largura - padding,
        "y_topo": padding,
        "y_base": altura - padding,
        "path_d": " ".join(comandos_path),
        "pontos": pontos,
        "faixa_min_label": f"R$ {preco_min:.2f}".replace(".", ","),
        "faixa_max_label": f"R$ {preco_max:.2f}".replace(".", ","),
        "tendencia_alta": precos[-1] >= precos[0],
    }


# --------------------------------------------------------------------------
# Análise simples de tendência de mercado (alta / baixa / neutro)
# --------------------------------------------------------------------------
def _tendencia_a_partir_de_precos_desc(precos_desc: list, janela_curta: int = 3, janela_longa: int = 10) -> str:
    """
    Núcleo puro (sem acesso ao banco) da análise de tendência: recebe preços
    já em ordem do mais recente para o mais antigo (no máximo `janela_longa`
    itens) e classifica comparando a média móvel curta com a longa. Extraído
    de analisar_tendencia para ser reaproveitado pelo backtesting (ver
    backtest_sinais_robo), que precisa simular a tendência "como ela era" em
    cada dia do histórico, não só a de hoje.
    """
    if len(precos_desc) < 2:
        return "DADOS_INSUFICIENTES"

    precos = [Decimal(p) for p in precos_desc]
    curta = precos[: min(janela_curta, len(precos))]
    media_curta = sum(curta) / len(curta)
    media_longa = sum(precos) / len(precos)

    if media_longa == 0:
        return "NEUTRO"

    diferenca_pct = ((media_curta - media_longa) / media_longa) * 100

    if diferenca_pct >= Decimal("0.5"):
        return "ALTA"
    if diferenca_pct <= Decimal("-0.5"):
        return "BAIXA"
    return "NEUTRO"


def analisar_tendencia(ativo: Ativo, janela_curta: int = 3, janela_longa: int = 10) -> str:
    """
    Analisa o histórico de cotações diárias de um ativo e classifica a
    tendência recente comparando a média móvel curta com a média móvel longa
    (uma aproximação simples de "possível alta/baixa" para dar suporte à
    decisão do investidor - não é recomendação de investimento).
    """
    historico = list(
        ativo.cotacoes.order_by("-data").values_list("preco_fechamento", flat=True)[:janela_longa]
    )
    return _tendencia_a_partir_de_precos_desc(historico, janela_curta, janela_longa)


TENDENCIA_LABELS = {
    "ALTA": "Possível alta",
    "BAIXA": "Possível baixa",
    "NEUTRO": "Estável",
    "DADOS_INSUFICIENTES": "Aguardando histórico",
}


# --------------------------------------------------------------------------
# Indicadores técnicos (RSI e MACD) e sinal consolidado de compra/venda
# --------------------------------------------------------------------------
def calcular_rsi(precos: list[float], periodo: int = 14) -> float | None:
    """
    RSI (Índice de Força Relativa) pelo método de suavização de Wilder, a
    partir de uma lista de preços de fechamento em ordem cronológica (mais
    antigo primeiro). Retorna None sem histórico suficiente (precisa de pelo
    menos periodo + 1 preços). Acima de 70 = sobrecomprado (sinal de venda);
    abaixo de 30 = sobrevendido (sinal de compra).
    """
    if len(precos) < periodo + 1:
        return None

    ganhos, perdas = [], []
    for anterior, atual in zip(precos, precos[1:]):
        variacao = atual - anterior
        ganhos.append(max(variacao, 0.0))
        perdas.append(max(-variacao, 0.0))

    media_ganho = sum(ganhos[:periodo]) / periodo
    media_perda = sum(perdas[:periodo]) / periodo

    for i in range(periodo, len(ganhos)):
        media_ganho = (media_ganho * (periodo - 1) + ganhos[i]) / periodo
        media_perda = (media_perda * (periodo - 1) + perdas[i]) / periodo

    if media_perda == 0:
        return 100.0
    rs = media_ganho / media_perda
    return round(100 - (100 / (1 + rs)), 2)


def _classificar_rsi(rsi: float | None) -> str:
    if rsi is None:
        return "DADOS_INSUFICIENTES"
    if rsi >= 70:
        return "SOBRECOMPRADO"
    if rsi <= 30:
        return "SOBREVENDIDO"
    return "NEUTRO"


RSI_LABELS = {
    "SOBRECOMPRADO": "Sobrecomprado (RSI alto)",
    "SOBREVENDIDO": "Sobrevendido (RSI baixo)",
    "NEUTRO": "Neutro",
    "DADOS_INSUFICIENTES": "Aguardando histórico",
}
# classe CSS (badge-alta/badge-baixa/badge-neutro) de cada rótulo - RSI baixo é
# sinal de compra (verde/"alta"), RSI alto é sinal de venda (vermelho/"baixa")
RSI_CLASSES = {
    "SOBRECOMPRADO": "baixa",
    "SOBREVENDIDO": "alta",
    "NEUTRO": "neutro",
    "DADOS_INSUFICIENTES": "neutro",
}
# traduz o vocabulário do RSI (SOBRECOMPRADO/SOBREVENDIDO) para o vocabulário
# de voto ALTA/BAIXA/NEUTRO/DADOS_INSUFICIENTES usado pelo Scanner Técnico
# (ver core.services.escanear_ativo_precos) - o rótulo exibido continua sendo
# o mais descritivo (RSI_LABELS/RSI_CLASSES), só a contagem de votos usa isto.
RSI_CHAVE_PARA_VOTO_SCANNER = {
    "SOBRECOMPRADO": "BAIXA",
    "SOBREVENDIDO": "ALTA",
    "NEUTRO": "NEUTRO",
    "DADOS_INSUFICIENTES": "DADOS_INSUFICIENTES",
}


def _media_movel_exponencial(valores: list[float], periodo: int) -> list[float]:
    """EMA de `valores`, iniciada com a média simples dos primeiros `periodo` itens."""
    if len(valores) < periodo:
        return []
    fator = 2 / (periodo + 1)
    ema = [sum(valores[:periodo]) / periodo]
    for valor in valores[periodo:]:
        ema.append(valor * fator + ema[-1] * (1 - fator))
    return ema


def calcular_macd(
    precos: list[float], periodo_curto: int = 12, periodo_longo: int = 26, periodo_sinal: int = 9
) -> dict | None:
    """
    MACD (Moving Average Convergence Divergence): linha MACD = EMA curta -
    EMA longa; linha de sinal = EMA do MACD. Histograma positivo (MACD acima
    da linha de sinal) = cruzamento de alta; negativo = cruzamento de baixa.
    Retorna None sem histórico suficiente.
    """
    if len(precos) < periodo_longo + periodo_sinal:
        return None

    ema_curta = _media_movel_exponencial(precos, periodo_curto)
    ema_longa = _media_movel_exponencial(precos, periodo_longo)
    # ema_curta[i] corresponde ao preço de índice (periodo_curto - 1 + i) na
    # lista original; ema_longa[j] ao índice (periodo_longo - 1 + j). Alinha
    # as duas séries pelo índice original antes de subtrair.
    deslocamento = periodo_longo - periodo_curto
    linha_macd = [c - l for c, l in zip(ema_curta[deslocamento:], ema_longa)]

    linha_sinal = _media_movel_exponencial(linha_macd, periodo_sinal)
    if not linha_sinal:
        return None

    macd_atual = linha_macd[-1]
    sinal_atual = linha_sinal[-1]
    histograma = macd_atual - sinal_atual

    return {
        "macd": round(macd_atual, 4),
        "sinal": round(sinal_atual, 4),
        "histograma": round(histograma, 4),
        "cruzamento": "ALTA" if histograma > 0 else ("BAIXA" if histograma < 0 else "NEUTRO"),
    }


MACD_LABELS = {
    "ALTA": "Cruzamento de alta",
    "BAIXA": "Cruzamento de baixa",
    "NEUTRO": "Neutro",
    "DADOS_INSUFICIENTES": "Aguardando histórico",
}
MACD_CLASSES = {
    "ALTA": "alta",
    "BAIXA": "baixa",
    "NEUTRO": "neutro",
    "DADOS_INSUFICIENTES": "neutro",
}

SINAL_GERAL_LABELS = {"COMPRA": "Sinal de compra", "VENDA": "Sinal de venda", "NEUTRO": "Neutro"}
SINAL_GERAL_CLASSES = {"COMPRA": "alta", "VENDA": "baixa", "NEUTRO": "neutro"}


def analisar_indicadores_tecnicos_precos(
    precos_cronologico: list[float], janela_curta: int = 3, janela_longa: int = 10
) -> dict:
    """
    Núcleo puro (sem acesso ao banco) da análise técnica consolidada: recebe
    os preços de fechamento em ordem cronológica (mais antigo primeiro) "até
    aquele momento" e devolve tendência, RSI, MACD e o sinal geral. Extraído
    de analisar_indicadores_tecnicos para ser reaproveitado pelo backtesting
    (ver backtest_sinais_robo), que recalcula o mesmo sinal em cada dia do
    passado usando só os preços disponíveis até aquele dia - exatamente a
    mesma lógica usada ao vivo, sem duplicar as regras de voto.
    """
    tendencia = _tendencia_a_partir_de_precos_desc(
        list(reversed(precos_cronologico))[:janela_longa], janela_curta, janela_longa
    )

    rsi = calcular_rsi(precos_cronologico)
    rsi_label_chave = _classificar_rsi(rsi)

    macd = calcular_macd(precos_cronologico)
    macd_label_chave = macd["cruzamento"] if macd else "DADOS_INSUFICIENTES"

    votos_compra = 0
    votos_venda = 0
    if tendencia == "ALTA":
        votos_compra += 1
    elif tendencia == "BAIXA":
        votos_venda += 1
    if rsi_label_chave == "SOBREVENDIDO":
        votos_compra += 1
    elif rsi_label_chave == "SOBRECOMPRADO":
        votos_venda += 1
    if macd_label_chave == "ALTA":
        votos_compra += 1
    elif macd_label_chave == "BAIXA":
        votos_venda += 1

    if votos_compra > votos_venda:
        sinal_geral = "COMPRA"
    elif votos_venda > votos_compra:
        sinal_geral = "VENDA"
    else:
        sinal_geral = "NEUTRO"

    return {
        "tendencia": tendencia,
        "tendencia_label": TENDENCIA_LABELS.get(tendencia, tendencia),
        "tendencia_classe": tendencia.lower() if tendencia != "DADOS_INSUFICIENTES" else "neutro",
        "rsi": rsi,
        "rsi_label": RSI_LABELS[rsi_label_chave],
        "rsi_classe": RSI_CLASSES[rsi_label_chave],
        "macd": macd,
        "macd_label": MACD_LABELS[macd_label_chave],
        "macd_classe": MACD_CLASSES[macd_label_chave],
        "sinal_geral": sinal_geral,
        "sinal_geral_label": SINAL_GERAL_LABELS[sinal_geral],
        "sinal_geral_classe": SINAL_GERAL_CLASSES[sinal_geral],
        "votos_compra": votos_compra,
        "votos_venda": votos_venda,
    }


def analisar_indicadores_tecnicos(ativo: Ativo, janela_curta: int = 3, janela_longa: int = 10) -> dict:
    """
    Consolida a análise técnica de um ativo: tendência (cruzamento de médias
    móveis já existente), RSI e MACD - cada um com seu próprio sinal - mais um
    sinal geral simples (quantos indicadores apontam compra x quantos apontam
    venda; empate ou indicadores insuficientes = neutro). É só uma referência
    de apoio à decisão, não uma recomendação de investimento.
    """
    historico = list(
        ativo.cotacoes.order_by("data").values_list("preco_fechamento", flat=True)
    )
    precos = [float(p) for p in historico]
    return analisar_indicadores_tecnicos_precos(precos, janela_curta, janela_longa)


# --------------------------------------------------------------------------
# Scanner Técnico da carteira (ações compradas)
#
# Combina IFR + médias móveis + MACD + volume + volatilidade + tendência num
# só painel por ativo comprado. Diferença deliberada do sinal_geral do robô
# consultor (analisar_indicadores_tecnicos_precos, que sempre elege um
# "vencedor" por votação, inclusive em caso de empate): aqui, sempre que pelo
# menos um indicador direcional aponta pra cada lado ao mesmo tempo, o
# veredito é explicitamente CONFLITANTE, em vez de fingir que existe uma
# previsão certa. Só fecha PREDOMINIO_ALTA/PREDOMINIO_BAIXA quando todos os
# indicadores direcionais que não ficaram neutros/sem dados concordam entre
# si. Volatilidade não entra nessa votação - é risco (magnitude), não direção.
# --------------------------------------------------------------------------
MEDIA_MOVEL_CURTA_PADRAO = 9
MEDIA_MOVEL_LONGA_PADRAO = 21


def calcular_medias_moveis(
    precos_cronologico: list[float], curta: int = MEDIA_MOVEL_CURTA_PADRAO, longa: int = MEDIA_MOVEL_LONGA_PADRAO
) -> dict | None:
    """
    Médias móveis simples curta (padrão: 9 pregões) e longa (padrão: 21
    pregões) a partir de preços em ordem cronológica (mais antigo primeiro).
    None sem histórico suficiente para a média longa.
    """
    if len(precos_cronologico) < longa:
        return None

    media_curta = sum(precos_cronologico[-curta:]) / curta
    media_longa = sum(precos_cronologico[-longa:]) / longa
    preco_atual = precos_cronologico[-1]

    return {
        "media_curta": round(media_curta, 2),
        "media_longa": round(media_longa, 2),
        "preco_atual": round(preco_atual, 2),
        "preco_acima_da_longa": preco_atual > media_longa,
        "preco_abaixo_da_longa": preco_atual < media_longa,
        "curta_acima_da_longa": media_curta > media_longa,
        "curta_abaixo_da_longa": media_curta < media_longa,
    }


def _classificar_medias_moveis(info: dict | None) -> str:
    """
    ALTA só quando o preço atual E a média curta concordam (as duas acima da
    longa); BAIXA quando as duas concordam abaixo. Qualquer combinação mista
    (ex: preço já voltou pra cima da longa, mas a curta ainda não cruzou) é um
    sinal internamente conflitante desse próprio indicador - fica NEUTRO em
    vez de forçar um lado.
    """
    if info is None:
        return "DADOS_INSUFICIENTES"
    if info["preco_acima_da_longa"] and info["curta_acima_da_longa"]:
        return "ALTA"
    if info["preco_abaixo_da_longa"] and info["curta_abaixo_da_longa"]:
        return "BAIXA"
    return "NEUTRO"


MEDIAS_MOVEIS_LABELS = {
    "ALTA": "Preço e médias em alinhamento de alta",
    "BAIXA": "Preço e médias em alinhamento de baixa",
    "NEUTRO": "Sem alinhamento claro",
    "DADOS_INSUFICIENTES": "Aguardando histórico",
}
MEDIAS_MOVEIS_CLASSES = {
    "ALTA": "alta", "BAIXA": "baixa", "NEUTRO": "neutro", "DADOS_INSUFICIENTES": "neutro",
}


VOLUME_JANELA_PADRAO = 20


def analisar_volume_precos(
    volumes_cronologico: list, precos_cronologico: list[float], janela: int = VOLUME_JANELA_PADRAO
) -> dict:
    """
    Compara o volume do último pregão com a média dos anteriores (janela
    padrão: 20 pregões) e cruza com a direção do preço no mesmo dia: volume
    acima da média (margem de 10%, pra não reagir a ruído) confirmando alta é
    força compradora; volume acima da média numa queda ("volume aumentando na
    queda") é força vendedora.

    Sem pelo menos 6 pregões com volume registrado, fica "DADOS_INSUFICIENTES"
    em vez de fingir um sinal - o campo Cotacao.volume foi adicionado depois
    de várias cotações já existirem sem ele (ver backfill_historico_cotacoes),
    então esse indicador só passa a valer conforme o histórico com volume vai
    se acumulando.
    """
    pares = [(v, p) for v, p in zip(volumes_cronologico, precos_cronologico) if v is not None]
    if len(pares) < 6:
        return {"classificacao": "DADOS_INSUFICIENTES", "volume_atual": None, "media_volume": None, "variacao_pct": None}

    volumes_validos = [v for v, _ in pares]
    volume_atual = volumes_validos[-1]
    anteriores = volumes_validos[-(janela + 1):-1]
    media_volume = sum(anteriores) / len(anteriores)

    variacao_pct = round((volume_atual - media_volume) / media_volume * 100, 2) if media_volume else None
    acima_da_media = media_volume > 0 and volume_atual > media_volume * 1.1

    preco_atual = precos_cronologico[-1]
    preco_anterior = precos_cronologico[-2] if len(precos_cronologico) >= 2 else None
    preco_caiu = preco_anterior is not None and preco_atual < preco_anterior
    preco_subiu = preco_anterior is not None and preco_atual > preco_anterior

    if acima_da_media and preco_caiu:
        classificacao = "BAIXA"
    elif acima_da_media and preco_subiu:
        classificacao = "ALTA"
    else:
        classificacao = "NEUTRO"

    return {
        "classificacao": classificacao,
        "volume_atual": volume_atual,
        "media_volume": round(media_volume, 0),
        "variacao_pct": variacao_pct,
    }


VOLUME_LABELS = {
    "ALTA": "Volume acima da média confirmando a alta",
    "BAIXA": "Volume aumentando na queda",
    "NEUTRO": "Volume dentro do normal",
    "DADOS_INSUFICIENTES": "Sem histórico de volume suficiente ainda",
}
VOLUME_CLASSES = {
    "ALTA": "alta", "BAIXA": "baixa", "NEUTRO": "neutro", "DADOS_INSUFICIENTES": "neutro",
}


VOLATILIDADE_ALTA_LIMIAR_PCT = 40.0
VOLATILIDADE_BAIXA_LIMIAR_PCT = 15.0


def _classificar_volatilidade(volatilidade_pct: float | None) -> str:
    """Não é direcional (não vota alta nem baixa) - é só um alerta de risco/contexto, independente do veredito."""
    if volatilidade_pct is None:
        return "DADOS_INSUFICIENTES"
    if volatilidade_pct >= VOLATILIDADE_ALTA_LIMIAR_PCT:
        return "ALTA"
    if volatilidade_pct <= VOLATILIDADE_BAIXA_LIMIAR_PCT:
        return "BAIXA"
    return "MODERADA"


VOLATILIDADE_LABELS = {
    "ALTA": "Alta volatilidade - oscila bastante",
    "MODERADA": "Volatilidade moderada",
    "BAIXA": "Baixa volatilidade - mais estável",
    "DADOS_INSUFICIENTES": "Aguardando histórico",
}
# não é direção de preço - "ALTA volatilidade" usa a cor de alerta (mesma do
# "BAIXA" nos outros indicadores) e "BAIXA volatilidade" usa a cor "positiva"
# (menos risco), pra não ser lido como um voto de alta/baixa do preço.
VOLATILIDADE_CLASSES = {
    "ALTA": "baixa", "MODERADA": "neutro", "BAIXA": "alta", "DADOS_INSUFICIENTES": "neutro",
}

VEREDITO_SCANNER_LABELS = {
    "PREDOMINIO_ALTA": "Predomínio de sinais de alta",
    "PREDOMINIO_BAIXA": "Predomínio de sinais de baixa",
    "CONFLITANTE": "Indicadores conflitantes - sem sinal claro",
    "NEUTRO": "Neutro - nenhum indicador aponta direção",
    "DADOS_INSUFICIENTES": "Aguardando histórico suficiente",
}
VEREDITO_SCANNER_CLASSES = {
    # CONFLITANTE fica sozinho com o âmbar (badge-neutro) - é o estado que
    # mais precisa chamar atenção nessa tela; NEUTRO/DADOS_INSUFICIENTES usam
    # o cinza (badge-dados-insuficientes) para não competir visualmente com
    # ele, já que os dois não têm nada de "conflito" pra sinalizar.
    "PREDOMINIO_ALTA": "alta", "PREDOMINIO_BAIXA": "baixa", "CONFLITANTE": "neutro",
    "NEUTRO": "dados-insuficientes", "DADOS_INSUFICIENTES": "dados-insuficientes",
}


def _veredito_scanner(chaves: list[str]) -> str:
    """
    A regra do veredito do Scanner Técnico, isolada dos indicadores em si pra
    poder ser testada direto: CONFLITANTE sempre que há pelo menos um "ALTA" E
    um "BAIXA" ao mesmo tempo entre as chaves (não importa quantos "NEUTRO"/
    "DADOS_INSUFICIENTES" também existam) - só fecha PREDOMINIO_ALTA/
    PREDOMINIO_BAIXA quando todas as chaves que apontaram uma direção
    concordam entre si. DADOS_INSUFICIENTES só quando TODAS as chaves são
    "DADOS_INSUFICIENTES" (nenhum indicador tem histórico suficiente ainda).
    """
    votos_alta = chaves.count("ALTA")
    votos_baixa = chaves.count("BAIXA")
    votos_sem_dados = chaves.count("DADOS_INSUFICIENTES")

    if votos_sem_dados == len(chaves):
        return "DADOS_INSUFICIENTES"
    if votos_alta > 0 and votos_baixa > 0:
        return "CONFLITANTE"
    if votos_alta > 0:
        return "PREDOMINIO_ALTA"
    if votos_baixa > 0:
        return "PREDOMINIO_BAIXA"
    return "NEUTRO"


def escanear_ativo_precos(
    precos_cronologico: list[float], volumes_cronologico: list | None = None,
    janela_curta: int = 3, janela_longa: int = 10,
) -> dict:
    """
    Núcleo puro (sem acesso ao banco) do Scanner Técnico: combina IFR (RSI),
    médias móveis, MACD, volume, volatilidade e tendência de curto prazo num
    só veredito. Ver o comentário da seção acima para a regra do veredito
    (CONFLITANTE sempre que houver indicadores discordando entre si).
    """
    if volumes_cronologico is None:
        volumes_cronologico = [None] * len(precos_cronologico)

    rsi = calcular_rsi(precos_cronologico)
    rsi_chave = _classificar_rsi(rsi)

    medias = calcular_medias_moveis(precos_cronologico)
    medias_chave = _classificar_medias_moveis(medias)

    macd = calcular_macd(precos_cronologico)
    macd_chave = macd["cruzamento"] if macd else "DADOS_INSUFICIENTES"

    volume_info = analisar_volume_precos(volumes_cronologico, precos_cronologico)
    volume_chave = volume_info["classificacao"]

    tendencia_chave = _tendencia_a_partir_de_precos_desc(
        list(reversed(precos_cronologico))[:janela_longa], janela_curta, janela_longa
    )

    volatilidade_metricas = _volatilidade_e_drawdown_precos(precos_cronologico)
    volatilidade_chave = _classificar_volatilidade(volatilidade_metricas["volatilidade_pct"])

    indicadores = [
        {
            "nome": "IFR (RSI)", "chave": RSI_CHAVE_PARA_VOTO_SCANNER[rsi_chave],
            "label": RSI_LABELS[rsi_chave], "classe": RSI_CLASSES[rsi_chave],
        },
        {
            "nome": "Médias móveis", "chave": medias_chave,
            "label": MEDIAS_MOVEIS_LABELS[medias_chave], "classe": MEDIAS_MOVEIS_CLASSES[medias_chave],
        },
        {"nome": "MACD", "chave": macd_chave, "label": MACD_LABELS[macd_chave], "classe": MACD_CLASSES[macd_chave]},
        {"nome": "Volume", "chave": volume_chave, "label": VOLUME_LABELS[volume_chave], "classe": VOLUME_CLASSES[volume_chave]},
        {
            "nome": "Tendência de curto prazo", "chave": tendencia_chave,
            "label": TENDENCIA_LABELS.get(tendencia_chave, tendencia_chave),
            "classe": tendencia_chave.lower() if tendencia_chave != "DADOS_INSUFICIENTES" else "neutro",
        },
    ]

    chaves = [item["chave"] for item in indicadores]
    votos_alta = chaves.count("ALTA")
    votos_baixa = chaves.count("BAIXA")
    votos_sem_dados = chaves.count("DADOS_INSUFICIENTES")
    veredito = _veredito_scanner(chaves)

    return {
        "indicadores": indicadores,
        "rsi": rsi,
        "medias_moveis": medias,
        "macd": macd,
        "volume": volume_info,
        "volatilidade_pct": volatilidade_metricas["volatilidade_pct"],
        "drawdown_pct": volatilidade_metricas["drawdown_pct"],
        "volatilidade_chave": volatilidade_chave,
        "volatilidade_label": VOLATILIDADE_LABELS[volatilidade_chave],
        "volatilidade_classe": VOLATILIDADE_CLASSES[volatilidade_chave],
        "tendencia_label": TENDENCIA_LABELS.get(tendencia_chave, tendencia_chave),
        "votos_alta": votos_alta,
        "votos_baixa": votos_baixa,
        "votos_sem_dados": votos_sem_dados,
        "veredito": veredito,
        "veredito_label": VEREDITO_SCANNER_LABELS[veredito],
        "veredito_classe": VEREDITO_SCANNER_CLASSES[veredito],
    }


def escanear_ativo(ativo: Ativo, janela_curta: int = 3, janela_longa: int = 10) -> dict:
    """Versão do Scanner Técnico que busca preços e volumes já salvos (Cotacao) de um ativo."""
    cotacoes = list(ativo.cotacoes.order_by("data").values_list("preco_fechamento", "volume"))
    precos = [float(preco) for preco, _ in cotacoes]
    volumes = [volume for _, volume in cotacoes]
    resultado = escanear_ativo_precos(precos, volumes, janela_curta, janela_longa)
    resultado["ativo"] = ativo
    return resultado


def escanear_carteira(usuario) -> list[dict]:
    """
    Scanner Técnico de todos os ativos realmente comprados (saldo > 0) do
    usuário, ordenados por ticker - ver core.views.scanner_tecnico.
    """
    posicoes = calcular_posicoes(usuario)
    compradas = sorted(
        (p for p in posicoes if not p.apenas_reservado and p.quantidade > 0),
        key=lambda p: p.ativo.ticker,
    )
    return [escanear_ativo(p.ativo) for p in compradas]


# --------------------------------------------------------------------------
# Backtesting do robô consultor
#
# Responde à pergunta que o robô sozinho não responde: "se eu tivesse
# seguido todo sinal de compra/venda que o robô já deu para este ativo,
# quantas vezes eu teria acertado?" - percorre o histórico de Cotacao já
# salvo recalculando, em cada pregão, o mesmo sinal que analisar_indicadores_
# tecnicos_precos calcularia "ao vivo" naquele dia (usando só os preços
# disponíveis até ali, nunca os futuros), e confere contra o retorno real
# `dias_retorno` pregões à frente. É só uma estatística sobre o passado, não
# garante desempenho futuro - a mesma ressalva do robô ao vivo.
# --------------------------------------------------------------------------
def backtest_sinais_robo(
    ativo: Ativo, dias_retorno: int = 5, janela_curta: int = 3, janela_longa: int = 10,
    minimo_precos: int = 15,
) -> dict:
    """
    Simula, dia a dia, os sinais que o robô consultor teria dado para
    `ativo` no passado (com base só no histórico de Cotacao já salvo) e mede
    a taxa de acerto: um sinal de COMPRA "acerta" quando o preço `dias_retorno`
    pregões depois está mais alto; um sinal de VENDA "acerta" quando está mais
    baixo. Sinais NEUTRO não entram na contagem (não haveria ação a avaliar).

    `minimo_precos` é o menor histórico a partir do qual já vale a pena
    simular um sinal (por padrão, o mínimo do RSI - com menos que isso o
    sinal geral tende a ficar sempre neutro por falta de dados); o MACD
    completo só passa a contar a partir de 35 pregões acumulados, mas isso é
    tratado automaticamente por analisar_indicadores_tecnicos_precos (fica
    "aguardando histórico" e simplesmente não vota até lá).
    """
    cotacoes = list(ativo.cotacoes.order_by("data"))
    precos = [float(c.preco_fechamento) for c in cotacoes]
    datas = [c.data for c in cotacoes]
    total_pregoes = len(precos)

    sinais = []
    for i in range(minimo_precos - 1, total_pregoes):
        if i + dias_retorno >= total_pregoes:
            break  # não há pregões suficientes à frente pra conferir o resultado deste sinal ainda

        indicadores = analisar_indicadores_tecnicos_precos(precos[: i + 1], janela_curta, janela_longa)
        sinal = indicadores["sinal_geral"]
        if sinal not in ("COMPRA", "VENDA"):
            continue

        preco_no_sinal = precos[i]
        if not preco_no_sinal:
            continue
        retorno_pct = (precos[i + dias_retorno] - preco_no_sinal) / preco_no_sinal * 100
        acerto = (sinal == "COMPRA" and retorno_pct > 0) or (sinal == "VENDA" and retorno_pct < 0)
        sinais.append({
            "data": datas[i], "sinal": sinal, "preco": round(preco_no_sinal, 2),
            "retorno_pct": round(retorno_pct, 2), "acerto": acerto,
        })

    if not sinais:
        return {
            "ativo": ativo, "dias_retorno": dias_retorno, "total_sinais": 0,
            "sinais_compra": 0, "sinais_venda": 0, "taxa_acerto_pct": None,
            "taxa_acerto_compra_pct": None, "taxa_acerto_venda_pct": None,
            "retorno_medio_compra_pct": None, "retorno_medio_venda_pct": None,
            "sinais": [], "dados_insuficientes": total_pregoes < minimo_precos + dias_retorno,
        }

    def _media_pct(itens):
        valores = [s["retorno_pct"] for s in itens]
        return round(sum(valores) / len(valores), 2) if valores else None

    def _taxa_acerto(itens):
        return round(sum(1 for s in itens if s["acerto"]) / len(itens) * 100, 1) if itens else None

    sinais_compra = [s for s in sinais if s["sinal"] == "COMPRA"]
    sinais_venda = [s for s in sinais if s["sinal"] == "VENDA"]

    return {
        "ativo": ativo,
        "dias_retorno": dias_retorno,
        "total_sinais": len(sinais),
        "sinais_compra": len(sinais_compra),
        "sinais_venda": len(sinais_venda),
        "taxa_acerto_pct": _taxa_acerto(sinais),
        "taxa_acerto_compra_pct": _taxa_acerto(sinais_compra),
        "taxa_acerto_venda_pct": _taxa_acerto(sinais_venda),
        "retorno_medio_compra_pct": _media_pct(sinais_compra),
        "retorno_medio_venda_pct": _media_pct(sinais_venda),
        # mais recentes primeiro, limitado pra não pesar a página com anos de sinais
        "sinais": list(reversed(sinais))[:30],
        "dados_insuficientes": False,
    }


# --------------------------------------------------------------------------
# Envio de avisos proativos via WhatsApp (Meta Cloud API)
#
# O sistema já recebe mensagens via webhook (ver core.views.whatsapp_webhook)
# - isso fecha o ciclo no outro sentido: enviar um aviso automático quando uma
# meta de lucro/perda é atingida ou o robô consultor dá um sinal de compra/
# venda, em vez de depender do usuário lembrar de abrir a tela Alertas. Cada
# usuário configura seu próprio número em "Minha Conta" (ver
# accounts.models.PerfilUsuario); sem WHATSAPP_ACCESS_TOKEN e
# WHATSAPP_PHONE_NUMBER_ID configurados no .env, o envio fica desligado e os
# alertas continuam funcionando normalmente, só na tela.
# --------------------------------------------------------------------------
def whatsapp_envio_configurado() -> bool:
    """True quando as credenciais do WhatsApp Business (Meta Cloud API) para ENVIAR mensagens estão configuradas."""
    return bool(settings.WHATSAPP_ACCESS_TOKEN and settings.WHATSAPP_PHONE_NUMBER_ID)


def enviar_whatsapp(numero_destino: str, mensagem: str) -> bool:
    """
    Envia uma mensagem de texto avulsa via WhatsApp Business (Meta Cloud API)
    para `numero_destino` (formato internacional, só dígitos - ex:
    5565999998888). Retorna True se a API aceitou o envio, False em qualquer
    falha (número inválido, token expirado, API fora do ar etc.) - nunca
    levanta exceção, porque um aviso por WhatsApp que falha não pode impedir
    o alerta de ser gravado e exibido normalmente na tela (ver
    _notificar_whatsapp_usuario).
    """
    if not whatsapp_envio_configurado() or not numero_destino:
        return False

    url = f"https://graph.facebook.com/v20.0/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": numero_destino,
        "type": "text",
        "text": {"body": mensagem},
    }
    try:
        resposta = requests.post(url, json=payload, headers=headers, timeout=10)
        resposta.raise_for_status()
        return True
    except requests.RequestException:
        return False


def _notificar_whatsapp_usuario(usuario, mensagem: str) -> None:
    """
    Envia `mensagem` para o WhatsApp cadastrado do usuário (ver
    accounts.models.PerfilUsuario), quando o envio está configurado e o
    usuário tem um número salvo - silencioso em qualquer outro caso (usuário
    sem perfil, sem número, ou envio desligado).
    """
    if not whatsapp_envio_configurado():
        return
    try:
        numero = usuario.perfil.numero_whatsapp
    except ObjectDoesNotExist:
        return
    if numero:
        enviar_whatsapp(numero, mensagem)


# --------------------------------------------------------------------------
# Geração de alertas/lembretes
# --------------------------------------------------------------------------
def gerar_alertas_para_usuario(usuario) -> list[Alerta]:
    """
    Verifica as posições do usuário contra as metas de lucro/perda definidas
    e cria alertas quando alguma meta é atingida (evita duplicar alertas do
    mesmo dia para o mesmo ativo/tipo).
    """
    hoje = timezone.localdate()
    novos_alertas = []

    for posicao in calcular_posicoes(usuario):
        if posicao.lucro_perda_pct is None:
            continue

        meta_lucro = abs(
            posicao.meta_lucro_pct if posicao.meta_lucro_pct is not None else Decimal(
                str(settings.META_LUCRO_PADRAO)
            )
        )
        # a meta de perda é sempre uma queda (valor negativo) - normaliza caso
        # tenha sido salva como positiva (dado antigo, de antes da validação)
        meta_perda = -abs(
            posicao.meta_perda_pct if posicao.meta_perda_pct is not None else Decimal(
                str(settings.META_PERDA_PADRAO)
            )
        )

        tipo_disparado = None
        if posicao.lucro_perda_pct >= meta_lucro:
            tipo_disparado = Alerta.LUCRO
            mensagem = (
                f"{posicao.ativo.ticker}: meta de lucro atingida "
                f"({posicao.lucro_perda_pct}% ≥ {meta_lucro}%)."
            )
        elif posicao.lucro_perda_pct <= meta_perda:
            tipo_disparado = Alerta.PERDA
            mensagem = (
                f"{posicao.ativo.ticker}: meta de perda atingida "
                f"({posicao.lucro_perda_pct}% ≤ {meta_perda}%)."
            )

        if tipo_disparado:
            ja_existe = Alerta.objects.filter(
                usuario=usuario,
                ativo=posicao.ativo,
                tipo=tipo_disparado,
                criado_em__date=hoje,
            ).exists()
            if not ja_existe:
                alerta = Alerta.objects.create(
                    usuario=usuario,
                    ativo=posicao.ativo,
                    tipo=tipo_disparado,
                    mensagem=mensagem,
                    percentual=posicao.lucro_perda_pct,
                )
                novos_alertas.append(alerta)
                _notificar_whatsapp_usuario(usuario, alerta.mensagem)

        if posicao.tendencia in ("ALTA", "BAIXA"):
            ja_existe_tendencia = Alerta.objects.filter(
                usuario=usuario,
                ativo=posicao.ativo,
                tipo=Alerta.TENDENCIA,
                criado_em__date=hoje,
            ).exists()
            if not ja_existe_tendencia:
                rotulo = TENDENCIA_LABELS.get(posicao.tendencia, posicao.tendencia)
                alerta = Alerta.objects.create(
                    usuario=usuario,
                    ativo=posicao.ativo,
                    tipo=Alerta.TENDENCIA,
                    mensagem=f"{posicao.ativo.ticker}: sinal de {rotulo.lower()} identificado no mercado.",
                )
                novos_alertas.append(alerta)

    return novos_alertas


def ativo_ja_comprado(usuario, ativo: Ativo) -> bool:
    """True quando o usuário já tem saldo comprado (em carteira) deste ativo."""
    return any(
        op.saldo > 0
        for op in Operacao.objects.filter(usuario=usuario, ativo=ativo, tipo=Operacao.COMPRA)
    )


def ativos_distintos_comprados(usuario) -> set[int]:
    """
    IDs dos ativos que o usuário tem efetivamente em carteira (saldo comprado
    > 0 em pelo menos um lote) - usado para aplicar o limite de ativos
    diferentes em carteira (MAX_ATIVOS_EM_CARTEIRA no .env, ver
    core.forms.OperacaoForm e core.forms.ConfirmarCompraForm).
    """
    ids = set()
    for op in Operacao.objects.filter(usuario=usuario, tipo=Operacao.COMPRA):
        if op.saldo > 0:
            ids.add(op.ativo_id)
    return ids


def gerar_sinais_robo_para_usuario(usuario) -> list[Alerta]:
    """
    "Robô consultor": aplica os indicadores técnicos já calculados pelo
    sistema (tendência por média móvel, RSI, MACD - ver
    analisar_indicadores_tecnicos) a cada ativo que o usuário acompanha
    (comprado ou reservado) e cria um alerta quando o sinal geral consolidado
    aponta claramente compra ou venda - evita duplicar o mesmo sinal no
    mesmo dia para o mesmo ativo.

    Sinal de COMPRA é ignorado pra ativos que o usuário já tem em carteira
    (saldo comprado > 0) - a sugestão de compra é pra ajudar a decidir sobre
    ativos que ele ainda não tem, não pra quem já está comprado (esse caso já
    é coberto pelo alerta de meta de lucro/perda). Quando o sinal de compra
    se aplica, além do alerta, cadastra automaticamente uma reserva (Operacao
    tipo RESERVAR, ver _reservar_automaticamente) pro ativo - a sugestão vira
    um item de verdade na grid "Reservadas" de Minhas Operações, onde o
    usuário decide se efetiva a compra (o robô não executa nenhuma ordem
    sozinho). Sinal de VENDA continua sendo avaliado normalmente mesmo pra
    quem já está comprado - é justamente o caso que interessa avisar.

    É só um apoio técnico à decisão - não constitui recomendação de
    investimento.
    """
    hoje = timezone.localdate()
    novos_alertas = []

    ativos = Ativo.objects.filter(operacoes__usuario=usuario).distinct()
    for ativo in ativos:
        indicadores = analisar_indicadores_tecnicos(ativo)
        sinal = indicadores["sinal_geral"]
        if sinal not in ("COMPRA", "VENDA"):
            continue

        if sinal == "COMPRA" and ativo_ja_comprado(usuario, ativo):
            continue

        tipo = Alerta.SINAL_COMPRA if sinal == "COMPRA" else Alerta.SINAL_VENDA
        ja_existe = Alerta.objects.filter(
            usuario=usuario, ativo=ativo, tipo=tipo, criado_em__date=hoje,
        ).exists()
        if not ja_existe:
            mensagem = (
                f"{ativo.ticker}: robô identificou sinal de {sinal.lower()} "
                f"({indicadores['tendencia_label'].lower()}, RSI: {indicadores['rsi_label'].lower()}, "
                f"MACD: {indicadores['macd_label'].lower()}) - sinal técnico, não é recomendação de investimento."
            )
            alerta = Alerta.objects.create(usuario=usuario, ativo=ativo, tipo=tipo, mensagem=mensagem)
            novos_alertas.append(alerta)
            _notificar_whatsapp_usuario(usuario, alerta.mensagem)

        if sinal == "COMPRA":
            _reservar_automaticamente(usuario, ativo)

    return novos_alertas


def _reservar_automaticamente(usuario, ativo: Ativo) -> None:
    """
    Cadastra uma reserva (intenção de compra) pro ativo quando o robô
    consultor identifica um sinal de compra, usando a cotação mais recente
    como preço pretendido - assim aparece como item acionável na grid
    "Reservadas" de Minhas Operações (o usuário decide se compra, editando o
    preço/quantidade se quiser, ou excluindo a reserva).

    Não duplica: se o usuário já tem uma reserva em aberto pra esse ativo,
    não mexe nela.
    """
    ja_reservado = Operacao.objects.filter(usuario=usuario, ativo=ativo, tipo=Operacao.RESERVAR).exists()
    if ja_reservado:
        return

    cotacao = ativo.ultima_cotacao()
    if cotacao is None:
        return  # sem cotação ainda não há preço pretendido pra registrar

    Operacao.objects.create(
        usuario=usuario,
        ativo=ativo,
        tipo=Operacao.RESERVAR,
        quantidade=1,
        preco_unitario=cotacao.preco_fechamento,
        data_operacao=timezone.localdate(),
        observacao="Reserva automática do robô consultor - sinal técnico de compra (RSI/MACD/tendência).",
    )


def limpar_alertas_antigos() -> int:
    """
    Remove os avisos/lembretes (Alerta) de TODOS os usuários que não são de
    hoje - mantém só os do dia atual. Retorna quantos foram apagados.
    """
    hoje = timezone.localdate()
    apagados, _ = Alerta.objects.exclude(criado_em__date=hoje).delete()
    return apagados


# --------------------------------------------------------------------------
# Relatório de indicação do robô consultor (Excel / PDF)
#
# Reflete a tabela "Indicadores técnicos" da página Análise de Mercado (ver
# core.views._sinais_robo): o sinal geral, a tendência, o RSI e o MACD de
# cada ativo acompanhado, mais se o ativo já está em carteira (contexto de
# por que um sinal de compra pode não ter virado uma reserva - ver
# core.services.gerar_sinais_robo_para_usuario).
# --------------------------------------------------------------------------
COLUNAS_ROBO = [
    "Ativo", "Em carteira", "Tendência", "RSI", "MACD", "Sinal geral", "Votos compra", "Votos venda",
]


def _linha_robo(item: dict) -> list:
    return [
        item["ativo"].ticker,
        "Sim" if item["em_carteira"] else "Não",
        item["tendencia_label"],
        item["rsi"],
        item["macd_label"],
        item["sinal_geral_label"],
        item["votos_compra"],
        item["votos_venda"],
    ]


def gerar_excel_robo(sinais: list[dict]) -> bytes:
    """
    Gera uma planilha .xlsx com a indicação atual do robô consultor (sinal
    geral, tendência, RSI e MACD) para cada ativo acompanhado, pronta para
    download.
    """
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Indicação do robô"

    ws.append(["Indicação do robô consultor"])
    ws.append([f"Gerado em {timezone.localtime().strftime('%d/%m/%Y %H:%M')}"])
    ws.append(["Sinal técnico (RSI, MACD, tendência) - não é recomendação de investimento."])
    ws.append([])
    ws.append(COLUNAS_ROBO)
    for celula in ws[5]:
        celula.font = Font(bold=True, color="FFFFFF")
        celula.fill = PatternFill("solid", fgColor="0D1526")
        celula.alignment = Alignment(horizontal="center")

    for item in sinais:
        ws.append(_linha_robo(item))

    for indice in range(1, len(COLUNAS_ROBO) + 1):
        ws.column_dimensions[get_column_letter(indice)].width = 18
    ws.freeze_panes = "A6"

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def gerar_pdf_robo(sinais: list[dict], nome_usuario: str) -> bytes:
    """
    Gera um PDF com a indicação atual do robô consultor (sinal geral,
    tendência, RSI e MACD) para cada ativo acompanhado, pronto para download.
    """
    import io
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    nome_usuario = escape(nome_usuario)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4),
        topMargin=1.5 * cm, bottomMargin=1.5 * cm, leftMargin=1.2 * cm, rightMargin=1.2 * cm,
    )
    estilos = getSampleStyleSheet()

    elementos = [
        Paragraph("BolsaTrader - Indicação do Robô Consultor", estilos["Title"]),
        Paragraph(
            f"{nome_usuario} - gerado em {timezone.localtime().strftime('%d/%m/%Y %H:%M')} - "
            "sinal técnico (RSI, MACD, tendência), não é recomendação de investimento.",
            estilos["Normal"],
        ),
        Spacer(1, 0.5 * cm),
    ]

    if not sinais:
        elementos.append(Paragraph("Nenhum ativo para analisar.", estilos["Normal"]))
    else:
        dados = [COLUNAS_ROBO]
        for item in sinais:
            linha = _linha_robo(item)
            dados.append([
                linha[0], linha[1], linha[2],
                f"{linha[3]:.2f}" if linha[3] is not None else "—",
                linha[4], linha[5], linha[6], linha[7],
            ])

        tabela = Table(dados, repeatRows=1)
        tabela.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0D1526")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef2f7")]),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        elementos.append(tabela)

    doc.build(elementos)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Notícias de mercado (fontes configuráveis, raspagem simples de manchetes)
# --------------------------------------------------------------------------
class NoticiaScrapingError(Exception):
    """Erro ao buscar ou ler as manchetes de uma fonte de notícias."""


_NOTICIAS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def buscar_manchetes(url: str, limite: int = 15) -> list[dict]:
    """
    Busca a página em `url` e extrai as manchetes (título + link) mais
    prováveis: primeiro títulos (h1/h2/h3) que contêm um link, depois
    qualquer link com texto longo o bastante pra parecer uma manchete de
    verdade - descarta menus, rodapés e links de uma palavra só.

    Cada site de notícias tem uma estrutura de HTML diferente, então isso é
    uma heurística genérica (não um parser dedicado a cada site) - captura a
    maioria dos títulos, mas pode incluir algum link que não é bem uma
    notícia, ou deixar escapar alguma manchete com marcação incomum.
    """
    try:
        resposta = requests.get(url, headers={"User-Agent": _NOTICIAS_USER_AGENT}, timeout=15)
        resposta.raise_for_status()
    except requests.RequestException as exc:
        raise NoticiaScrapingError(f"Falha ao acessar {url}: {exc}") from exc

    soup = BeautifulSoup(resposta.text, "html.parser")

    encontradas = []
    vistos = set()

    candidatos = soup.select("h1 a[href], h2 a[href], h3 a[href]") + soup.select("a[href]")
    for link in candidatos:
        titulo = link.get_text(strip=True)
        href = link.get("href")
        if not titulo or not href or len(titulo) < 25:
            continue  # texto curto demais pra ser um título de matéria (menu, "Entrar", etc.)

        link_absoluto = urljoin(url, href)
        if link_absoluto in vistos:
            continue
        vistos.add(link_absoluto)

        encontradas.append({"titulo": titulo, "url": link_absoluto})
        if len(encontradas) >= limite:
            break

    return encontradas


def atualizar_noticias_fonte(fonte: FonteNoticia, limite: int = 15) -> list[int]:
    """
    Busca as manchetes que o site está exibindo agora (as "notícias do dia")
    e sincroniza a fonte com elas: grava as que ainda não existem
    (identificadas pelo link - já vistas não duplicam) e exclui as manchetes
    antigas que não estão mais na página (não são mais exibidas pelo site).

    Se o site não retornar nenhuma manchete reconhecível, trata como falha de
    leitura (NoticiaScrapingError) em vez de apagar o histórico existente -
    evita esvaziar a fonte por causa de uma leitura ruim.

    Retorna os ids das notícias novas gravadas (não só a contagem) - usado
    pela tela de Notícias do Mercado para destacar as recém-encontradas.
    """
    manchetes = buscar_manchetes(fonte.url, limite=limite)
    if not manchetes:
        raise NoticiaScrapingError(f"Nenhuma notícia encontrada em {fonte.url}.")

    urls_atuais = {item["url"] for item in manchetes}

    novas_ids = []
    for item in manchetes:
        noticia, criada = Noticia.objects.get_or_create(
            url=item["url"],
            defaults={"fonte": fonte, "titulo": item["titulo"]},
        )
        if criada:
            novas_ids.append(noticia.id)

    fonte.noticias.exclude(url__in=urls_atuais).delete()

    return novas_ids


# --------------------------------------------------------------------------
# Conta corrente (caixa das operações de bolsa)
# --------------------------------------------------------------------------
def obter_ou_criar_conta_corrente(usuario) -> ContaCorrente:
    """Cada usuário tem no máximo uma conta corrente - cria na primeira vez que for preciso (saldo inicial R$ 0)."""
    conta, _ = ContaCorrente.objects.get_or_create(usuario=usuario)
    return conta


def saldo_conta_corrente(conta: ContaCorrente) -> Decimal:
    """Saldo atual = saldo inicial + soma dos créditos - soma dos débitos de todos os lançamentos."""
    total_creditos = conta.lancamentos.filter(
        tipo=LancamentoContaCorrente.CREDITO
    ).aggregate(total=Sum("valor"))["total"] or Decimal("0")
    total_debitos = conta.lancamentos.filter(
        tipo=LancamentoContaCorrente.DEBITO
    ).aggregate(total=Sum("valor"))["total"] or Decimal("0")
    return conta.saldo_inicial + total_creditos - total_debitos


def sincronizar_lancamento_compra(operacao: Operacao) -> None:
    """
    Cria/atualiza o lançamento de DÉBITO (compra) da conta corrente do usuário
    com o valor/quantidade/data atuais da Operacao - chamar sempre depois de
    criar, efetivar (reserva -> compra) ou editar uma compra. Reservas
    (Operacao.RESERVAR) não geram lançamento, já que ainda não é dinheiro
    comprometido de fato.
    """
    if operacao.tipo != Operacao.COMPRA:
        return

    conta = obter_ou_criar_conta_corrente(operacao.usuario)
    LancamentoContaCorrente.objects.update_or_create(
        operacao=operacao,
        origem=LancamentoContaCorrente.ORIGEM_COMPRA,
        defaults={
            "conta": conta,
            "tipo": LancamentoContaCorrente.DEBITO,
            "valor": operacao.valor_total,
            "descricao": f"Compra de {operacao.quantidade}x {operacao.ativo.ticker}",
            "data": operacao.data_operacao,
        },
    )


def sincronizar_lancamento_venda(operacao: Operacao) -> None:
    """
    Cria/atualiza o lançamento de CRÉDITO (venda) da conta corrente do usuário
    com a venda (total ou parcial) já registrada neste lote de compra - chamar
    sempre depois de registrar ou corrigir uma venda (core.views.
    operacao_vender). Usa o valor líquido (já descontada a corretora), que é o
    que realmente entra na conta. Sem venda registrada (quantidade_vendida
    zerada, ex: venda desfeita ao editar o lote), remove o lançamento se
    existir.
    """
    if not operacao.quantidade_vendida or operacao.preco_venda is None:
        LancamentoContaCorrente.objects.filter(
            operacao=operacao, origem=LancamentoContaCorrente.ORIGEM_VENDA
        ).delete()
        return

    conta = obter_ou_criar_conta_corrente(operacao.usuario)
    valor = operacao.valor_liquido_vendido
    LancamentoContaCorrente.objects.update_or_create(
        operacao=operacao,
        origem=LancamentoContaCorrente.ORIGEM_VENDA,
        defaults={
            "conta": conta,
            "tipo": LancamentoContaCorrente.CREDITO,
            "valor": valor,
            "descricao": f"Venda de {operacao.quantidade_vendida}x {operacao.ativo.ticker}",
            "data": operacao.data_venda or timezone.localdate(),
        },
    )


def registrar_transferencia_conta_corrente(
    usuario, tipo: str, valor: Decimal, descricao: str, data: date | None = None
) -> LancamentoContaCorrente:
    """Registra uma transferência manual (crédito ou débito) de/para outra conta (ex: Nubank, corretora)."""
    conta = obter_ou_criar_conta_corrente(usuario)
    return LancamentoContaCorrente.objects.create(
        conta=conta,
        tipo=tipo,
        origem=LancamentoContaCorrente.ORIGEM_TRANSFERENCIA,
        valor=valor,
        descricao=descricao,
        data=data or timezone.localdate(),
    )


def extrato_conta_corrente(
    conta: ContaCorrente, data_inicio: date | None = None, data_fim: date | None = None
) -> list[dict]:
    """
    Lançamentos da conta corrente no período informado (todos, se nenhuma
    data for passada), com o saldo acumulado (saldo_apos) já calculado a
    partir do saldo inicial mais tudo que aconteceu antes do período - assim
    o extrato bate com o saldo real da conta mesmo filtrando só uma fatia do
    tempo. Devolvido do lançamento mais recente para o mais antigo, como as
    outras grids do sistema.
    """
    todos = conta.lancamentos.select_related("operacao__ativo").order_by("data", "criado_em")
    saldo = conta.saldo_inicial
    linhas = []
    for lancamento in todos:
        sinal = 1 if lancamento.tipo == LancamentoContaCorrente.CREDITO else -1

        if data_inicio and lancamento.data < data_inicio:
            saldo += sinal * lancamento.valor
            continue
        if data_fim and lancamento.data > data_fim:
            continue

        saldo += sinal * lancamento.valor
        linhas.append({"lancamento": lancamento, "saldo_apos": saldo})

    linhas.reverse()
    return linhas


COLUNAS_EXTRATO_CONTA_CORRENTE = ["Data", "Tipo", "Origem", "Descrição", "Valor (R$)", "Saldo após (R$)"]


def _linha_extrato_conta_corrente(item: dict) -> list:
    lancamento = item["lancamento"]
    sinal = 1 if lancamento.tipo == LancamentoContaCorrente.CREDITO else -1
    return [
        lancamento.data,
        lancamento.get_tipo_display(),
        lancamento.get_origem_display(),
        lancamento.descricao,
        float(sinal * lancamento.valor),
        float(item["saldo_apos"]),
    ]


def gerar_excel_extrato_conta_corrente(linhas: list[dict], saldo_inicial: Decimal, saldo_atual: Decimal) -> bytes:
    """Gera uma planilha .xlsx com o extrato da conta corrente do usuário, pronta para download."""
    import io

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Extrato conta corrente"

    ws.append(["Extrato da Conta Corrente"])
    ws.append([f"Gerado em {timezone.localtime().strftime('%d/%m/%Y %H:%M')}"])
    ws.append([f"Saldo inicial: R$ {saldo_inicial}", f"Saldo atual: R$ {saldo_atual}"])
    ws.append([])
    ws.append(COLUNAS_EXTRATO_CONTA_CORRENTE)
    for celula in ws[5]:
        celula.font = Font(bold=True, color="FFFFFF")
        celula.fill = PatternFill("solid", fgColor="0D1526")
        celula.alignment = Alignment(horizontal="center")

    for item in linhas:
        linha = _linha_extrato_conta_corrente(item)
        linha[0] = linha[0].strftime("%d/%m/%Y")
        ws.append(linha)

    for indice in range(1, len(COLUNAS_EXTRATO_CONTA_CORRENTE) + 1):
        ws.column_dimensions[get_column_letter(indice)].width = 22
    ws.freeze_panes = "A6"

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def gerar_pdf_extrato_conta_corrente(
    linhas: list[dict], saldo_inicial: Decimal, saldo_atual: Decimal, periodo_label: str, nome_usuario: str
) -> bytes:
    """Gera um PDF com o extrato da conta corrente do usuário, pronto para download."""
    import io
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    nome_usuario = escape(nome_usuario)
    periodo_label = escape(periodo_label)
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=landscape(A4),
        topMargin=1.5 * cm, bottomMargin=1.5 * cm, leftMargin=1.2 * cm, rightMargin=1.2 * cm,
    )
    estilos = getSampleStyleSheet()

    elementos = [
        Paragraph("BolsaTrader - Extrato da Conta Corrente", estilos["Title"]),
        Paragraph(
            f"{nome_usuario} - gerado em {timezone.localtime().strftime('%d/%m/%Y %H:%M')}",
            estilos["Normal"],
        ),
        Paragraph(periodo_label, estilos["Normal"]),
        Paragraph(
            f"Saldo inicial: R$ {saldo_inicial:.2f} — Saldo atual: R$ {saldo_atual:.2f}",
            estilos["Normal"],
        ),
        Spacer(1, 0.5 * cm),
    ]

    if not linhas:
        elementos.append(Paragraph("Nenhum lançamento no período selecionado.", estilos["Normal"]))
    else:
        dados = [COLUNAS_EXTRATO_CONTA_CORRENTE]
        for item in linhas:
            linha = _linha_extrato_conta_corrente(item)
            dados.append([
                linha[0].strftime("%d/%m/%Y"), linha[1], linha[2], linha[3],
                f"{linha[4]:.2f}", f"{linha[5]:.2f}",
            ])

        tabela = Table(dados, repeatRows=1)
        tabela.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0D1526")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef2f7")]),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        elementos.append(tabela)

    doc.build(elementos)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Post-it (bloco de notas pessoal e fixo, nas telas Menu/Operações/Posições)
# --------------------------------------------------------------------------
def obter_post_it(usuario) -> PostIt | None:
    """
    O post-it do usuário, se ele já tiver escrito algo alguma vez - None
    quando ainda não existe. Só busca (nunca cria): é chamado pelo context
    processor em TODA requisição autenticada (ver core.context_processors.
    post_it), então criar um registro vazio aqui gravaria no banco à toa para
    quem nunca usou o post-it. A criação de verdade só acontece ao salvar
    (ver salvar_post_it).
    """
    return PostIt.objects.filter(usuario=usuario).first()


def salvar_post_it(usuario, texto: str | None = None, minimizado: bool | None = None) -> PostIt:
    """
    Cria (na primeira vez) ou atualiza o post-it do usuário - só grava os
    campos realmente informados, então salvar só o estado de minimizado não
    mexe no texto e vice-versa.
    """
    post_it, _ = PostIt.objects.get_or_create(usuario=usuario)
    campos = []
    if texto is not None:
        post_it.texto = texto
        campos.append("texto")
    if minimizado is not None:
        post_it.minimizado = minimizado
        campos.append("minimizado")
    if campos:
        post_it.save(update_fields=campos)
    return post_it

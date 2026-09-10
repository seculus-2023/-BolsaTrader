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

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import Ativo, Cotacao, Operacao, Alerta, FonteNoticia, Noticia, AcaoB3


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
        resposta = requests.get(url, params=params, timeout=10)
        resposta.raise_for_status()
        dados = resposta.json()
    except requests.RequestException as exc:
        raise BrapiError(f"Falha ao consultar a API de cotações para {ticker}: {exc}") from exc

    resultados = dados.get("results") or []
    if not resultados:
        raise BrapiError(f"Ticker {ticker} não encontrado na API de cotações.")

    return resultados[0]


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
            resposta = requests.get(url, params=params, timeout=10)
            resposta.raise_for_status()
            dados = resposta.json()
        except requests.RequestException as exc:
            raise BrapiError(f"Falha ao consultar as maiores variações do dia: {exc}") from exc
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
        resposta = requests.get(url, params=params, timeout=30)
        resposta.raise_for_status()
        dados = resposta.json()
    except requests.RequestException as exc:
        raise BrapiError(f"Falha ao consultar a lista de ações da B3: {exc}") from exc

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

    ativo.save()  # também atualiza 'atualizado_em' (auto_now) com o horário desta atualização

    cotacao, _ = Cotacao.objects.update_or_create(
        ativo=ativo,
        data=timezone.localdate(),
        defaults={
            "preco_fechamento": Decimal(str(preco)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            "variacao_dia_pct": Decimal(str(variacao)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if variacao is not None
            else None,
        },
    )
    return cotacao


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


def gerar_excel_posicoes(posicoes: list[Posicao]) -> bytes:
    """Gera uma planilha .xlsx com as posições em carteira, pronta para download."""
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

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def gerar_pdf_posicoes(posicoes: list[Posicao], nome_usuario: str) -> bytes:
    """Gera um PDF (paisagem, uma tabela) com as posições em carteira, pronto para download."""
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
    "Lucro/Perda realizado (R$)", "Lucro/Perda realizado (%)", "Meta lucro (%)", "Meta perda (%)",
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
         lambda v: fmt(v, "%"), fmt, fmt, lambda v: fmt(v, "%"),
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
    if len(historico) < 2:
        return "DADOS_INSUFICIENTES"

    precos = [Decimal(p) for p in historico]  # já em ordem do mais recente para o mais antigo
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


def analisar_indicadores_tecnicos(ativo: Ativo, janela_curta: int = 3, janela_longa: int = 10) -> dict:
    """
    Consolida a análise técnica de um ativo: tendência (cruzamento de médias
    móveis já existente), RSI e MACD - cada um com seu próprio sinal - mais um
    sinal geral simples (quantos indicadores apontam compra x quantos apontam
    venda; empate ou indicadores insuficientes = neutro). É só uma referência
    de apoio à decisão, não uma recomendação de investimento.
    """
    tendencia = analisar_tendencia(ativo, janela_curta, janela_longa)

    historico = list(
        ativo.cotacoes.order_by("data").values_list("preco_fechamento", flat=True)
    )
    precos = [float(p) for p in historico]

    rsi = calcular_rsi(precos)
    rsi_label_chave = _classificar_rsi(rsi)

    macd = calcular_macd(precos)
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

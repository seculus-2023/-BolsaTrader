"""
Controle das DARFs (código 6015 - ganhos líquidos em renda variável):
criação/atualização das guias a partir da apuração mensal, multa e juros de
pagamento em atraso, registro do pagamento (com débito na conta corrente) e
lembrete de vencimento pelo Telegram.

Fica separado de tributario.services de propósito: aqui se usa
core.services (conta corrente, Telegram), e core.services chama o lembrete
daqui no ciclo automático - importar isso em tributario.services criaria uma
importação circular.

Acréscimos por atraso (Lei 9.430/1996, art. 61):
- multa de mora de 0,33% por dia de atraso, limitada a 20%;
- juros = soma da Selic mensal do mês seguinte ao vencimento até o mês
  anterior ao pagamento, mais 1% no mês do pagamento.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

import requests
from django.db import transaction
from django.utils import timezone

from core.models import LancamentoContaCorrente
from core.services import credenciais_telegram, enviar_notificacao_telegram, obter_ou_criar_conta_corrente

from .models import Darf, TaxaSelicMensal
from .services import ApuracaoMes, apurar_meses

logger = logging.getLogger(__name__)

MULTA_POR_DIA_PCT = Decimal("0.33")
MULTA_LIMITE_PCT = Decimal("20")
JUROS_MES_PAGAMENTO_PCT = Decimal("1")
DIAS_ANTECEDENCIA_LEMBRETE = 5
BCB_SGS_SELIC_MENSAL = 4390
URL_SICALC = "https://sicalc.receita.economia.gov.br/sicalc/principal"


def _centavos(valor: Decimal) -> Decimal:
    return valor.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _primeiro_dia(dia: date) -> date:
    return dia.replace(day=1)


def _mes_seguinte(mes: date) -> date:
    return date(mes.year + 1, 1, 1) if mes.month == 12 else date(mes.year, mes.month + 1, 1)


def meses_de_selic(vencimento: date, data_pagamento: date) -> list[date]:
    """Meses cuja Selic entra nos juros: do mês seguinte ao vencimento até o mês anterior ao pagamento."""
    meses = []
    mes = _mes_seguinte(_primeiro_dia(vencimento))
    mes_pagamento = _primeiro_dia(data_pagamento)
    while mes < mes_pagamento:
        meses.append(mes)
        mes = _mes_seguinte(mes)
    return meses


@dataclass
class Acrescimos:
    dias_atraso: int = 0
    multa_pct: Decimal = Decimal("0")
    multa: Decimal = Decimal("0")
    juros_pct: Decimal = Decimal("0")
    juros: Decimal = Decimal("0")
    selic_completa: bool = True  # False = faltou a Selic de algum mês (juros subestimados)


def calcular_acrescimos(
    valor: Decimal, vencimento: date, data_pagamento: date, taxas_selic: dict[date, Decimal],
) -> Acrescimos:
    if data_pagamento <= vencimento:
        return Acrescimos()
    dias = (data_pagamento - vencimento).days
    multa_pct = min(MULTA_POR_DIA_PCT * dias, MULTA_LIMITE_PCT)
    meses = meses_de_selic(vencimento, data_pagamento)
    juros_pct = sum((taxas_selic[m] for m in meses if m in taxas_selic), Decimal("0")) + JUROS_MES_PAGAMENTO_PCT
    return Acrescimos(
        dias_atraso=dias,
        multa_pct=multa_pct,
        multa=_centavos(valor * multa_pct / 100),
        juros_pct=juros_pct,
        juros=_centavos(valor * juros_pct / 100),
        selic_completa=all(m in taxas_selic for m in meses),
    )


def buscar_selic_mensal(inicio: date, fim: date) -> dict[date, Decimal]:
    """Selic acumulada de cada mês do período, direto do Banco Central (série 4390 do SGS)."""
    url = f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{BCB_SGS_SELIC_MENSAL}/dados"
    params = {"formato": "json", "dataInicial": inicio.strftime("%d/%m/%Y"), "dataFinal": fim.strftime("%d/%m/%Y")}
    resposta = requests.get(url, params=params, timeout=8)
    resposta.raise_for_status()
    taxas = {}
    for item in resposta.json() or []:
        try:
            mes = _primeiro_dia(datetime.strptime(item["data"], "%d/%m/%Y").date())
            taxas[mes] = Decimal(str(item["valor"]))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            continue
    return taxas


def obter_taxas_selic(meses: list[date]) -> dict[date, Decimal]:
    """
    Selic dos meses pedidos - primeiro do cache (TaxaSelicMensal); os que
    faltarem são buscados no Banco Central e gravados. Sem internet, devolve
    só o que já havia (o chamador marca os juros como incompletos).
    """
    if not meses:
        return {}
    taxas = dict(TaxaSelicMensal.objects.filter(mes__in=meses).values_list("mes", "taxa_pct"))
    faltando = [m for m in meses if m not in taxas]
    if faltando:
        try:
            novas = buscar_selic_mensal(min(faltando), max(faltando))
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Não foi possível buscar a Selic mensal no Banco Central (%s).", type(exc).__name__)
            novas = {}
        for mes, taxa in novas.items():
            TaxaSelicMensal.objects.update_or_create(mes=mes, defaults={"taxa_pct": taxa})
        taxas.update({m: t for m, t in novas.items() if m in meses})
    return taxas


def acrescimos_ate(darf: Darf, data_pagamento: date) -> Acrescimos:
    meses = meses_de_selic(darf.vencimento, data_pagamento) if data_pagamento > darf.vencimento else []
    return calcular_acrescimos(darf.valor_principal, darf.vencimento, data_pagamento, obter_taxas_selic(meses))


@transaction.atomic
def sincronizar_darfs(usuario, meses: list[ApuracaoMes] | None = None) -> list[Darf]:
    """
    Deixa as guias do usuário iguais à apuração: cria a de cada mês com DARF a
    pagar, atualiza valor e vencimento das que estão em aberto e apaga as em
    aberto de meses que deixaram de ter imposto (ex: venda corrigida). Guias
    pagas não mudam. Devolve todas as guias, da mais recente para a mais antiga.
    """
    if meses is None:
        meses = apurar_meses(usuario)
    apuradas = {date(m.ano, m.mes, 1): m for m in meses if m.valor_darf > 0}

    for darf in Darf.objects.filter(usuario=usuario, data_pagamento__isnull=True):
        mes = apuradas.get(darf.competencia)
        if mes is None:
            darf.delete()
        elif (darf.valor_principal, darf.vencimento) != (mes.valor_darf, mes.vencimento):
            darf.valor_principal, darf.vencimento = mes.valor_darf, mes.vencimento
            darf.save(update_fields=["valor_principal", "vencimento", "atualizado_em"])

    existentes = set(Darf.objects.filter(usuario=usuario).values_list("competencia", flat=True))
    for competencia, mes in apuradas.items():
        if competencia not in existentes:
            Darf.objects.create(
                usuario=usuario, competencia=competencia, valor_principal=mes.valor_darf, vencimento=mes.vencimento,
            )
    return list(Darf.objects.filter(usuario=usuario))


@dataclass
class SituacaoDarf:
    """Uma guia com a situação calculada para a tela: em aberto, vencida (com acréscimos até hoje) ou paga."""

    darf: Darf
    status: str  # ABERTA, VENCIDA ou PAGA
    acrescimos: Acrescimos
    total_hoje: Decimal
    valor_apurado: Decimal  # o que a apuração diz hoje (difere do principal de guia paga se algo mudou)

    @property
    def diferenca(self) -> Decimal:
        return self.valor_apurado - self.darf.valor_principal if self.darf.paga else Decimal("0")

    @property
    def pago_a_mais(self) -> Decimal:
        return max(Decimal("0"), -self.diferenca)

    @property
    def periodo_apuracao(self) -> date:
        """Último dia do mês de apuração - o "período de apuração" pedido no Sicalc."""
        return _mes_seguinte(self.darf.competencia) - timedelta(days=1)


def situacao_das_darfs(usuario, meses: list[ApuracaoMes] | None = None) -> list[SituacaoDarf]:
    if meses is None:
        meses = apurar_meses(usuario)
    apurado = {date(m.ano, m.mes, 1): m.valor_darf for m in meses}
    hoje = timezone.localdate()
    situacoes = []
    for darf in sincronizar_darfs(usuario, meses):
        if darf.paga:
            status, acrescimos = "PAGA", Acrescimos()
            total = darf.valor_pago or darf.valor_principal
        else:
            acrescimos = acrescimos_ate(darf, hoje)
            status = "VENCIDA" if acrescimos.dias_atraso else "ABERTA"
            total = darf.valor_principal + acrescimos.multa + acrescimos.juros
        situacoes.append(SituacaoDarf(
            darf=darf, status=status, acrescimos=acrescimos, total_hoje=total,
            valor_apurado=apurado.get(darf.competencia, Decimal("0")),
        ))
    return situacoes


@transaction.atomic
def registrar_pagamento(darf: Darf, data_pagamento: date, valor_pago: Decimal) -> Darf:
    """Marca a guia como paga e lança o débito na conta corrente, na data do pagamento."""
    acrescimos = acrescimos_ate(darf, data_pagamento)
    conta = obter_ou_criar_conta_corrente(darf.usuario)
    descricao = f"DARF {darf.codigo_receita} - IR renda variável {darf.competencia:%m/%Y}"
    if darf.lancamento_id:
        lancamento = darf.lancamento
        lancamento.valor, lancamento.data, lancamento.descricao = valor_pago, data_pagamento, descricao
        lancamento.save(update_fields=["valor", "data", "descricao"])
    else:
        lancamento = LancamentoContaCorrente.objects.create(
            conta=conta, tipo=LancamentoContaCorrente.DEBITO, origem=LancamentoContaCorrente.ORIGEM_IMPOSTO,
            valor=valor_pago, descricao=descricao, data=data_pagamento,
        )
    darf.data_pagamento = data_pagamento
    darf.valor_pago = valor_pago
    darf.multa_paga = acrescimos.multa
    darf.juros_pagos = acrescimos.juros
    darf.lancamento = lancamento
    darf.save()
    return darf


@transaction.atomic
def desfazer_pagamento(darf: Darf) -> Darf:
    """Volta a guia para "em aberto" (pagamento marcado por engano) e remove o débito da conta corrente."""
    if darf.lancamento_id:
        darf.lancamento.delete()
    darf.data_pagamento = None
    darf.valor_pago = None
    darf.multa_paga = Decimal("0")
    darf.juros_pagos = Decimal("0")
    darf.lancamento = None
    darf.save()
    return darf


def notificar_darfs_telegram(usuario) -> bool:
    """
    Lembra pelo Telegram, uma vez por dia, das guias em aberto que vencem nos
    próximos dias ou que já venceram (com o valor atualizado com multa e
    juros). Não faz nada para quem não cadastrou o bot.
    """
    if credenciais_telegram(usuario) is None:
        return False
    hoje = timezone.localdate()
    linhas, lembradas = [], []
    for situacao in situacao_das_darfs(usuario):
        darf = situacao.darf
        if situacao.status == "PAGA" or darf.ultimo_lembrete == hoje:
            continue
        dias = (darf.vencimento - hoje).days
        if situacao.status == "VENCIDA":
            linhas.append(
                f"• {darf.competencia:%m/%Y}: VENCIDA há {-dias} dia(s) - R$ {situacao.total_hoje} "
                f"com multa e juros (principal R$ {darf.valor_principal})"
            )
        elif dias <= DIAS_ANTECEDENCIA_LEMBRETE:
            quando = "hoje" if dias == 0 else f"em {dias} dia(s)"
            linhas.append(f"• {darf.competencia:%m/%Y}: R$ {darf.valor_principal} vence {quando} ({darf.vencimento:%d/%m/%Y})")
        else:
            continue
        lembradas.append(darf)

    if not linhas:
        return False
    texto = "\n".join([
        f"🧾 DARF {lembradas[0].codigo_receita} (IR sobre a bolsa) - {usuario.get_username()}",
        *linhas,
        f"Emita a guia no Sicalc: {URL_SICALC}",
    ])
    enviado = enviar_notificacao_telegram(usuario, texto)
    if enviado:
        Darf.objects.filter(id__in=[d.id for d in lembradas]).update(ultimo_lembrete=hoje)
    return enviado

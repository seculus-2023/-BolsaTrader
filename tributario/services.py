"""
Cálculos fiscais das operações em bolsa (ações, FIIs, ETFs, BDRs e units).

Enquanto a tela de operações trabalha por LOTE (cada compra com o próprio
preço, para metas e preço alvo), a Receita Federal exige o PREÇO MÉDIO
PONDERADO por ativo: toda compra se mistura ao custo da posição, e cada
venda dá baixa pelo preço médio vigente no dia. Este módulo refaz esse
cálculo, em ordem cronológica, a partir dos mesmos registros (Operacao e
VendaLote) - nada é gravado, é sempre recalculado, então editar ou excluir
uma operação antiga corrige tudo que vem depois automaticamente.

Regras aplicadas:
- custo de aquisição = preço x quantidade + taxas da compra;
- valor de venda líquido = valor bruto - corretora - taxas da venda;
- day trade: compra e venda do MESMO ativo no MESMO dia, na quantidade que
  coincide (a menor das duas). Essa parte usa o preço médio das compras do
  próprio dia e não entra no preço médio da posição - independente de qual
  lote foi vendido na tela de operações;
- a sobra do dia é operação comum: compra sobrando entra no preço médio,
  venda sobrando dá baixa pelo preço médio da posição.

Os valores são estimativas para conferência - não substituem um contador.
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from core.classificacao import ACAO, FII, UNIT
from core.models import Operacao

from .models import ParametroTributario, SaldoInicialFiscal

COMUM = "COMUM"
DAY_TRADE = "DAY_TRADE"
MODALIDADE_LABELS = {COMUM: "Operação comum", DAY_TRADE: "Day trade"}

CENTAVO = Decimal("0.01")


def _centavos(valor: Decimal) -> Decimal:
    return valor.quantize(CENTAVO, rounding=ROUND_HALF_UP)


@dataclass
class MovimentoDia:
    """Compras e vendas de um ativo somadas num mesmo dia."""

    quantidade_compra: int = 0
    custo_compra: Decimal = Decimal("0")  # preço x quantidade + taxas
    quantidade_venda: int = 0
    valor_venda: Decimal = Decimal("0")  # bruto
    custos_venda: Decimal = Decimal("0")  # corretora + taxas


@dataclass
class PosicaoFiscal:
    """Quantidade e custo de aquisição (preço médio ponderado) de um ativo para o imposto de renda."""

    ticker: str
    classe: str = ""
    quantidade: int = 0
    custo_total: Decimal = Decimal("0")

    @property
    def preco_medio(self) -> Decimal | None:
        if not self.quantidade:
            return None
        return (self.custo_total / self.quantidade).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    @property
    def custo_total_centavos(self) -> Decimal:
        return _centavos(self.custo_total)


@dataclass
class ResultadoVenda:
    """Ganho ou perda apurado nas vendas de um ativo num dia, separado por modalidade (comum ou day trade)."""

    data: date
    ticker: str
    classe: str
    modalidade: str
    quantidade: int
    valor_venda: Decimal
    custos_venda: Decimal
    custo_aquisicao: Decimal

    @property
    def modalidade_label(self) -> str:
        return MODALIDADE_LABELS[self.modalidade]

    @property
    def resultado(self) -> Decimal:
        return _centavos(self.valor_venda - self.custos_venda - self.custo_aquisicao)


@dataclass
class ApuracaoFiscal:
    posicoes: dict[str, PosicaoFiscal] = field(default_factory=dict)
    resultados: list[ResultadoVenda] = field(default_factory=list)
    inconsistencias: list[str] = field(default_factory=list)

    def posicoes_em_aberto(self) -> list[PosicaoFiscal]:
        return sorted((p for p in self.posicoes.values() if p.quantidade > 0), key=lambda p: p.ticker)

    def resultados_do_ano(self, ano: int) -> list[ResultadoVenda]:
        return [r for r in self.resultados if r.data.year == ano]

    def anos_com_vendas(self) -> list[int]:
        return sorted({r.data.year for r in self.resultados}, reverse=True)


def calcular_apuracao(movimentos: dict[tuple[str, date], MovimentoDia], classes: dict[str, str]) -> ApuracaoFiscal:
    """
    Processa os movimentos (por ativo e dia) em ordem cronológica e devolve
    as posições com preço médio, o resultado de cada venda e os avisos de
    dados inconsistentes (ex: venda maior que a posição na data).
    """
    apuracao = ApuracaoFiscal()

    for ticker, dia in sorted(movimentos, key=lambda chave: (chave[1], chave[0])):
        mov = movimentos[(ticker, dia)]
        classe = classes.get(ticker, "")
        posicao = apuracao.posicoes.setdefault(ticker, PosicaoFiscal(ticker=ticker, classe=classe))

        quantidade_day_trade = min(mov.quantidade_compra, mov.quantidade_venda)
        if quantidade_day_trade:
            fracao_venda = Decimal(quantidade_day_trade) / mov.quantidade_venda
            fracao_compra = Decimal(quantidade_day_trade) / mov.quantidade_compra
            apuracao.resultados.append(ResultadoVenda(
                data=dia, ticker=ticker, classe=classe, modalidade=DAY_TRADE, quantidade=quantidade_day_trade,
                valor_venda=mov.valor_venda * fracao_venda,
                custos_venda=mov.custos_venda * fracao_venda,
                custo_aquisicao=mov.custo_compra * fracao_compra,
            ))

        sobra_compra = mov.quantidade_compra - quantidade_day_trade
        if sobra_compra:
            posicao.quantidade += sobra_compra
            posicao.custo_total += mov.custo_compra * Decimal(sobra_compra) / mov.quantidade_compra

        sobra_venda = mov.quantidade_venda - quantidade_day_trade
        if sobra_venda:
            if sobra_venda > posicao.quantidade:
                apuracao.inconsistencias.append(
                    f"{dia:%d/%m/%Y} - {ticker}: venda de {sobra_venda} unidade(s) com só {posicao.quantidade} "
                    "em carteira nessa data. Confira as datas de compra e venda dos lotes."
                )
            baixa = min(sobra_venda, posicao.quantidade)
            custo_baixa = posicao.custo_total * baixa / posicao.quantidade if posicao.quantidade else Decimal("0")
            posicao.quantidade -= baixa
            posicao.custo_total = posicao.custo_total - custo_baixa if posicao.quantidade else Decimal("0")

            fracao_venda = Decimal(sobra_venda) / mov.quantidade_venda
            apuracao.resultados.append(ResultadoVenda(
                data=dia, ticker=ticker, classe=classe, modalidade=COMUM, quantidade=sobra_venda,
                valor_venda=mov.valor_venda * fracao_venda,
                custos_venda=mov.custos_venda * fracao_venda,
                custo_aquisicao=custo_baixa,
            ))

    return apuracao


def apurar_operacoes(usuario, ate: date | None = None) -> ApuracaoFiscal:
    """Apuração fiscal de todas as compras e vendas de ações do usuário (até a data informada, se houver)."""
    lotes = (
        Operacao.objects.filter(usuario=usuario, tipo=Operacao.COMPRA)
        .select_related("ativo").prefetch_related("vendas")
    )
    movimentos: dict[tuple[str, date], MovimentoDia] = defaultdict(MovimentoDia)
    classes: dict[str, str] = {}

    for lote in lotes:
        ticker = lote.ativo.ticker
        classes[ticker] = lote.ativo.classe
        if ate is None or lote.data_operacao <= ate:
            mov = movimentos[(ticker, lote.data_operacao)]
            mov.quantidade_compra += lote.quantidade
            mov.custo_compra += lote.valor_total + (lote.taxas_compra or Decimal("0"))
        for venda in lote.vendas_registradas():
            if ate is not None and venda.data > ate:
                continue
            mov = movimentos[(ticker, venda.data)]
            mov.quantidade_venda += venda.quantidade
            mov.valor_venda += venda.valor_bruto
            mov.custos_venda += venda.custos

    return calcular_apuracao(movimentos, classes)


# --------------------------------------------------------------------------
# Apuração mensal do imposto de renda
#
# Os resultados das vendas (acima) são somados por mês em três grupos, cada um
# com a própria alíquota e o próprio estoque de prejuízo - prejuízo de um
# grupo nunca compensa ganho de outro:
# - operação comum: ações, ETFs, BDRs e units fora do day trade;
# - day trade: os mesmos ativos, comprados e vendidos no mesmo dia;
# - fundos imobiliários: FIIs, comum ou day trade.
# Ganho em operação comum com ações é isento no mês em que a soma das vendas
# de ações (inclusive day trade) não passa do limite (R$ 20 mil). O IR retido
# na fonte ("dedo-duro") abate o imposto do mês, e o que sobra abate os meses
# seguintes do mesmo ano. Imposto abaixo do mínimo de DARF (R$ 10) acumula.
# --------------------------------------------------------------------------
GRUPO_COMUM = "COMUM"
GRUPO_DAY_TRADE = "DAY_TRADE"
GRUPO_FII = "FII"
GRUPOS = (GRUPO_COMUM, GRUPO_DAY_TRADE, GRUPO_FII)
GRUPO_LABELS = {
    GRUPO_COMUM: "Operação comum",
    GRUPO_DAY_TRADE: "Day trade",
    GRUPO_FII: "Fundos imobiliários",
}

ZERO = Decimal("0")

# usado quando a tabela de parâmetros estiver vazia - mesmos valores padrão do model
PARAMETRO_PADRAO = ParametroTributario(vigencia_inicio=date(2000, 1, 1))


def parametro_vigente(data: date, parametros: list[ParametroTributario] | None = None) -> ParametroTributario:
    if parametros is None:
        parametros = list(ParametroTributario.objects.all())
    for parametro in sorted(parametros, key=lambda p: p.vigencia_inicio, reverse=True):
        if parametro.vigencia_inicio <= data and (parametro.vigencia_fim is None or parametro.vigencia_fim >= data):
            return parametro
    return PARAMETRO_PADRAO


def grupo_do_resultado(resultado: ResultadoVenda) -> str:
    if resultado.classe == FII:
        return GRUPO_FII
    return GRUPO_DAY_TRADE if resultado.modalidade == DAY_TRADE else GRUPO_COMUM


def classes_com_isencao(parametro: ParametroTributario) -> set[str]:
    return {ACAO, UNIT} if parametro.units_isentas else {ACAO}


def ultimo_dia_util(ano: int, mes: int) -> date:
    """Último dia útil (segunda a sexta) do mês - feriados não são considerados."""
    dia = date(ano, mes, calendar.monthrange(ano, mes)[1])
    while dia.weekday() >= 5:
        dia -= timedelta(days=1)
    return dia


@dataclass
class ApuracaoGrupo:
    grupo: str
    aliquota_pct: Decimal
    resultado: Decimal = ZERO  # soma dos ganhos e perdas do mês
    isento: Decimal = ZERO  # parte do ganho isenta (só ações em operação comum)
    prejuizo_anterior: Decimal = ZERO
    prejuizo_compensado: Decimal = ZERO
    base_calculo: Decimal = ZERO
    imposto: Decimal = ZERO
    prejuizo_a_compensar: Decimal = ZERO

    @property
    def label(self) -> str:
        return GRUPO_LABELS[self.grupo]

    @property
    def resultado_tributavel(self) -> Decimal:
        return self.resultado - self.isento


@dataclass
class ApuracaoMes:
    ano: int
    mes: int
    parametro: ParametroTributario
    resultados: list[ResultadoVenda]
    vendas_acoes: Decimal
    grupos: dict[str, ApuracaoGrupo]
    irrf_mes: Decimal = ZERO
    irrf_saldo_anterior: Decimal = ZERO
    irrf_utilizado: Decimal = ZERO
    irrf_saldo_final: Decimal = ZERO
    imposto_acumulado_anterior: Decimal = ZERO  # de meses abaixo do mínimo de DARF
    total_a_pagar: Decimal = ZERO
    valor_darf: Decimal = ZERO
    imposto_acumulado_proximo: Decimal = ZERO

    @property
    def competencia(self) -> str:
        return f"{self.mes:02d}/{self.ano}"

    @property
    def limite_isencao(self) -> Decimal:
        return self.parametro.limite_isencao_acoes

    @property
    def isento(self) -> bool:
        return self.vendas_acoes <= self.limite_isencao

    @property
    def disponivel_isencao(self) -> Decimal:
        return max(ZERO, self.limite_isencao - self.vendas_acoes)

    @property
    def percentual_limite(self) -> int:
        if not self.limite_isencao:
            return 100
        return min(100, int(self.vendas_acoes / self.limite_isencao * 100))

    @property
    def imposto_bruto(self) -> Decimal:
        return sum((grupo.imposto for grupo in self.grupos.values()), ZERO)

    @property
    def vencimento(self) -> date:
        ano, mes = (self.ano + 1, 1) if self.mes == 12 else (self.ano, self.mes + 1)
        return ultimo_dia_util(ano, mes)


def _irrf_do_mes(resultados: list[ResultadoVenda], parametro: ParametroTributario) -> Decimal:
    """
    Estimativa do IR retido pela corretora no mês: percentual sobre o valor das
    vendas comuns e percentual sobre o ganho de cada dia em day trade. Retenção
    abaixo de R$ 1,00 é dispensada.
    """
    vendas_comuns = sum((r.valor_venda for r in resultados if r.modalidade == COMUM), ZERO)
    irrf_comum = _centavos(vendas_comuns * parametro.irrf_comum_pct / 100)

    ganho_day_trade_por_dia: dict[date, Decimal] = defaultdict(lambda: ZERO)
    for r in resultados:
        if r.modalidade == DAY_TRADE:
            ganho_day_trade_por_dia[r.data] += r.resultado
    ganhos = sum((ganho for ganho in ganho_day_trade_por_dia.values() if ganho > 0), ZERO)
    irrf_day_trade = _centavos(ganhos * parametro.irrf_day_trade_pct / 100)

    return sum((valor for valor in (irrf_comum, irrf_day_trade) if valor >= Decimal("1")), ZERO)


def calcular_meses(
    resultados: list[ResultadoVenda],
    prejuizos_iniciais: dict[str, Decimal] | None = None,
    parametros: list[ParametroTributario] | None = None,
) -> list[ApuracaoMes]:
    """Apuração de cada mês com venda, em ordem cronológica (os saldos passam de um mês para o outro)."""
    if parametros is None:
        parametros = list(ParametroTributario.objects.all())
    prejuizos = {grupo: (prejuizos_iniciais or {}).get(grupo, ZERO) for grupo in GRUPOS}

    por_mes: dict[tuple[int, int], list[ResultadoVenda]] = defaultdict(list)
    for resultado in resultados:
        por_mes[(resultado.data.year, resultado.data.month)].append(resultado)

    meses: list[ApuracaoMes] = []
    irrf_saldo = ZERO
    imposto_acumulado = ZERO
    for ano, mes in sorted(por_mes):
        do_mes = por_mes[(ano, mes)]
        parametro = parametro_vigente(date(ano, mes, 1), parametros)
        isentas = classes_com_isencao(parametro)
        aliquotas = {
            GRUPO_COMUM: parametro.aliquota_comum_pct,
            GRUPO_DAY_TRADE: parametro.aliquota_day_trade_pct,
            GRUPO_FII: parametro.aliquota_fii_pct,
        }
        apuracao = ApuracaoMes(
            ano=ano, mes=mes, parametro=parametro, resultados=do_mes,
            vendas_acoes=_centavos(sum((r.valor_venda for r in do_mes if r.classe in isentas), ZERO)),
            grupos={grupo: ApuracaoGrupo(grupo=grupo, aliquota_pct=aliquotas[grupo]) for grupo in GRUPOS},
        )

        for resultado in do_mes:
            apuracao.grupos[grupo_do_resultado(resultado)].resultado += resultado.resultado
        if apuracao.isento:
            ganho_acoes = sum(
                (r.resultado for r in do_mes if grupo_do_resultado(r) == GRUPO_COMUM and r.classe in isentas), ZERO
            )
            # só o GANHO líquido com ações é isento - prejuízo continua indo para compensação
            if ganho_acoes > 0:
                apuracao.grupos[GRUPO_COMUM].isento = ganho_acoes

        for grupo in apuracao.grupos.values():
            grupo.prejuizo_anterior = prejuizos[grupo.grupo]
            tributavel = grupo.resultado_tributavel
            if tributavel > 0:
                grupo.prejuizo_compensado = min(grupo.prejuizo_anterior, tributavel)
                grupo.base_calculo = tributavel - grupo.prejuizo_compensado
                grupo.prejuizo_a_compensar = grupo.prejuizo_anterior - grupo.prejuizo_compensado
            else:
                grupo.prejuizo_a_compensar = grupo.prejuizo_anterior - tributavel
            grupo.imposto = _centavos(grupo.base_calculo * grupo.aliquota_pct / 100)
            prejuizos[grupo.grupo] = grupo.prejuizo_a_compensar

        # IRRF que sobra só abate meses do mesmo ano - o resto vai para a declaração anual
        if meses and meses[-1].ano != ano:
            irrf_saldo = ZERO
        apuracao.irrf_mes = _irrf_do_mes(do_mes, parametro)
        apuracao.irrf_saldo_anterior = irrf_saldo
        disponivel = irrf_saldo + apuracao.irrf_mes
        apuracao.irrf_utilizado = min(disponivel, apuracao.imposto_bruto)
        apuracao.irrf_saldo_final = irrf_saldo = disponivel - apuracao.irrf_utilizado

        apuracao.imposto_acumulado_anterior = imposto_acumulado
        apuracao.total_a_pagar = apuracao.imposto_bruto - apuracao.irrf_utilizado + imposto_acumulado
        if apuracao.total_a_pagar >= parametro.darf_minimo:
            apuracao.valor_darf = apuracao.total_a_pagar
            imposto_acumulado = ZERO
        else:
            imposto_acumulado = apuracao.total_a_pagar
        apuracao.imposto_acumulado_proximo = imposto_acumulado

        meses.append(apuracao)
    return meses


def prejuizos_iniciais_do_usuario(usuario) -> dict[str, Decimal]:
    saldo = SaldoInicialFiscal.objects.filter(usuario=usuario).first()
    if saldo is None:
        return {grupo: ZERO for grupo in GRUPOS}
    return {
        GRUPO_COMUM: saldo.prejuizo_comum,
        GRUPO_DAY_TRADE: saldo.prejuizo_day_trade,
        GRUPO_FII: saldo.prejuizo_fii,
    }


def apurar_meses(usuario, apuracao: ApuracaoFiscal | None = None) -> list[ApuracaoMes]:
    """Apuração mensal de todas as vendas do usuário (aceita uma apuração de operações já calculada)."""
    if apuracao is None:
        apuracao = apurar_operacoes(usuario)
    return calcular_meses(apuracao.resultados, prejuizos_iniciais_do_usuario(usuario))


def ir_estimado_por_lote(usuario, lotes) -> dict[int, Decimal]:
    """
    Quanto do imposto de cada mês cabe às vendas de cada lote - para mostrar
    "lucro após IR" na tela de operações, que é por lote. O IR é apurado por
    mês e por grupo, sobre o preço médio, então não existe "o IR do lote":
    cada venda com lucro recebe a alíquota EFETIVA do grupo no mês (imposto do
    grupo dividido pela soma dos ganhos tributáveis do grupo), que já reflete
    isenção e compensação de prejuízos. Venda com perda não gera IR.
    """
    apuracao = apurar_operacoes(usuario)
    meses = apurar_meses(usuario, apuracao)

    taxa_efetiva: dict[tuple[int, int, str], Decimal] = {}
    isencao_por_mes: dict[tuple[int, int], set[str]] = {}
    for mes in meses:
        isentas = classes_com_isencao(mes.parametro) if mes.isento else set()
        isencao_por_mes[(mes.ano, mes.mes)] = isentas
        for grupo in mes.grupos.values():
            ganhos = sum(
                (
                    r.resultado for r in mes.resultados
                    if grupo_do_resultado(r) == grupo.grupo and r.resultado > 0
                    and not (grupo.grupo == GRUPO_COMUM and r.classe in isentas)
                ),
                ZERO,
            )
            taxa_efetiva[(mes.ano, mes.mes, grupo.grupo)] = grupo.imposto / ganhos if ganhos else ZERO

    # quanto das vendas de cada ativo em cada dia foi day trade
    quantidades: dict[tuple[str, date], list[int]] = defaultdict(lambda: [0, 0])
    for r in apuracao.resultados:
        quantidade = quantidades[(r.ticker, r.data)]
        quantidade[1] += r.quantidade
        if r.modalidade == DAY_TRADE:
            quantidade[0] += r.quantidade

    estimativas: dict[int, Decimal] = {}
    for lote in lotes:
        classe = lote.ativo.classe
        total = ZERO
        for venda in lote.vendas_registradas():
            lucro = (venda.preco - lote.preco_unitario) * venda.quantidade
            if lucro <= 0:
                continue
            chave_mes = (venda.data.year, venda.data.month)
            day_trade, vendido = quantidades.get((lote.ativo.ticker, venda.data), (0, 0))
            fracao_day_trade = Decimal(day_trade) / vendido if vendido else ZERO
            grupo_comum = GRUPO_FII if classe == FII else GRUPO_COMUM
            grupo_day_trade = GRUPO_FII if classe == FII else GRUPO_DAY_TRADE
            taxa_comum = (
                ZERO if grupo_comum == GRUPO_COMUM and classe in isencao_por_mes.get(chave_mes, set())
                else taxa_efetiva.get((*chave_mes, grupo_comum), ZERO)
            )
            taxa = (
                fracao_day_trade * taxa_efetiva.get((*chave_mes, grupo_day_trade), ZERO)
                + (1 - fracao_day_trade) * taxa_comum
            )
            total += lucro * taxa
        estimativas[lote.id] = _centavos(total)
    return estimativas

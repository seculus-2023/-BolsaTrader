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

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from core.models import Operacao

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

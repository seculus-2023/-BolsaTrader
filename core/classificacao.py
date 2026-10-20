"""
Classificação de um ticker da B3 na classe de ativo usada pela tributação
(ação, FII, ETF, BDR ou unit) - a alíquota e a isenção de IR dependem dela.

Função pura (sem acesso ao banco) de propósito: é chamada tanto pelo
Ativo.save() quanto pela migração que preenche a classe dos ativos já
cadastrados, e uma migração não deve depender do estado atual dos models.

A inferência é só um ponto de partida: o sufixo 11 é usado por FIIs, ETFs e
units ao mesmo tempo, então ali a regra olha o nome e uma lista de units e
ETFs conhecidos, e na dúvida assume FII (o caso mais comum entre pessoas
físicas). O administrador corrige pelo Admin quando a inferência errar.
"""
import re

ACAO = "ACAO"
FII = "FII"
ETF = "ETF"
BDR = "BDR"
UNIT = "UNIT"

CLASSE_CHOICES = [
    (ACAO, "Ação"),
    (FII, "Fundo imobiliário (FII)"),
    (ETF, "ETF"),
    (BDR, "BDR"),
    (UNIT, "Unit"),
]

# units mais negociadas (sufixo 11, mas compostas por ações - não são FII)
UNITS_CONHECIDAS = {
    "ALUP11", "BPAC11", "BRBI11", "ENGI11", "IGTI11", "KLBN11", "SANB11",
    "SAPR11", "TAEE11", "TIET11", "AESB11", "CPLE11", "RNEW11", "SULA11",
    "BIDI11", "PPLA11", "GGPS11",
}

# ETFs mais negociados na B3
ETFS_CONHECIDOS = {
    "BOVA11", "BOVV11", "BOVB11", "BOVX11", "SMAL11", "SMAC11", "IVVB11",
    "SPXI11", "HASH11", "QBTC11", "BITH11", "ETHE11", "GOLD11", "DIVO11",
    "FIND11", "MATB11", "PIBB11", "ECOO11", "XFIX11", "IMAB11", "B5P211",
    "IRFM11", "FIXA11", "NASD11", "ACWI11", "EURP11", "XINA11", "TECK11",
    "WRLD11", "GOVE11", "BRAX11", "ISUS11", "UTIL11", "NFTS11", "DEFI11",
}


def inferir_classe_ativo(ticker: str, nome: str = "") -> str:
    """Classe provável do ativo a partir do ticker (e do nome, quando ajuda a desempatar o sufixo 11)."""
    ticker = (ticker or "").strip().upper()
    nome = (nome or "").upper()

    # tickers fracionários (PETR4F) seguem a classe do papel inteiro
    if ticker.endswith("F") and len(ticker) > 5:
        ticker = ticker[:-1]

    sufixo = re.search(r"(\d+)$", ticker)
    numero = sufixo.group(1) if sufixo else ""

    if numero in {"32", "33", "34", "35", "39"}:
        return BDR
    if numero == "11":
        if ticker in UNITS_CONHECIDAS:
            return UNIT
        if ticker in ETFS_CONHECIDOS or "ETF" in nome or "ISHARES" in nome or "ÍNDICE" in nome:
            return ETF
        if "UNIT" in nome:
            return UNIT
        return FII
    return ACAO

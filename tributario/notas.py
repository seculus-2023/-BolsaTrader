"""
Importação das notas de negociação (notas de corretagem) em PDF.

Lê o layout padrão SINACOR usado pela Clear, XP, Rico e outras corretoras:
cada página tem o número da nota, a data do pregão, a tabela "Negócios
realizados" e o "Resumo Financeiro" com as taxas. Só o mercado à vista e o
fracionário viram compras e vendas - notas de mercado futuro (BM&F), opções
e termo são listadas como aviso, porque o sistema não controla esses ativos.

A importação cria as compras (Operacao) e as vendas (VendaLote) como se o
usuário as tivesse digitado, com as taxas da nota rateadas pelo valor de
cada negócio. Cada venda dá baixa primeiro nas compras do mesmo dia (day
trade) e depois nas mais antigas - o IR não depende disso, porque usa o
preço médio por ativo (ver tributario.services), mas a tela de operações
mostra o resultado por lote.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction

from core.classificacao import BDR, FII, UNIT
from core.models import AcaoB3, Ativo, Operacao, VendaLote
from core.services import recalcular_resumo_vendas, sincronizar_lancamento_compra

from .models import MapeamentoTitulo, NotaCorretagem

CENTAVO = Decimal("0.01")
ZERO = Decimal("0")

MERCADOS_IMPORTADOS = {"VISTA", "FRACIONARIO"}

# classe do papel na especificação -> final do ticker e classe fiscal
SUFIXO_CLASSE = {
    "ON": ("3", ""), "PN": ("4", ""), "PNA": ("5", ""), "PNB": ("6", ""), "PNC": ("7", ""), "PND": ("8", ""),
    "UNT": ("11", UNIT), "CI": ("11", FII), "DRN": ("34", BDR), "DR1": ("34", BDR), "DR2": ("35", BDR),
    "DR3": ("35", BDR),
}

# nome de pregão (como sai na nota) -> raiz do ticker, para os casos em que o
# nome não aparece na razão social (ex: TAESA) ou a empresa não está na lista
# de ações da B3 que o sistema sincroniza (ex: BDRs)
RAIZ_POR_NOME = {
    "ALPARGATAS": "ALPA", "AMBEV S/A": "ABEV", "B3": "B3SA", "BBSEGURIDADE": "BBSE", "BRADESCO": "BBDC",
    "BRASIL": "BBAS", "CEMIG": "CMIG", "COPASA": "CSMG", "COPEL": "CPLE", "COSAN": "CSAN", "ENEVA": "ENEV",
    "EQUATORIAL": "EQTL", "GERDAU": "GGBR", "HYPERA": "HYPE", "ITAUSA": "ITSA", "ITAUUNIBANCO": "ITUB",
    "KLABIN S/A": "KLBN", "LOCALIZA": "RENT", "MAGAZ LUIZA": "MGLU", "MARCOPOLO": "POMO", "NU HOLDINGS": "ROXO",
    "PETROBRAS": "PETR", "PETRORIO": "PRIO", "RAIZEN": "RAIZ", "SABESP": "SBSP", "SANEPAR": "SAPR",
    "SID NACIONAL": "CSNA", "SUZANO S.A.": "SUZB", "TAESA": "TAEE", "TENDA": "TEND", "USIMINAS": "USIM",
    "VALE": "VALE", "VIVARA S.A.": "VIVA", "WEG": "WEGE",
}

# marcas que seguem a classe na especificação (governança, "ex-direitos" etc.) - não fazem parte do papel
_PADRAO_NUMERO = re.compile(r"^\d{1,3}(\.\d{3})*,\d{2,}$")
_PADRAO_QUANTIDADE = re.compile(r"^\d{1,3}(\.\d{3})*$")


class NotaInvalida(Exception):
    """PDF que não é uma nota de negociação no layout esperado."""


class ImportacaoInvalida(Exception):
    """As notas não podem ser importadas como estão (ex: venda sem compra em carteira)."""


class ExclusaoBloqueada(Exception):
    """Excluir as notas apagaria vendas que não vieram delas."""


def _decimal(texto: str) -> Decimal:
    return Decimal(texto.replace(".", "").replace(",", "."))


def _centavos(valor: Decimal) -> Decimal:
    return valor.quantize(CENTAVO, rounding=ROUND_HALF_UP)


def _sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))


# --------------------------------------------------------------------------
# Leitura do PDF


@dataclass
class Negocio:
    cv: str  # "C" ou "V"
    mercado: str
    especificacao: str
    obs: str
    quantidade: int
    preco: Decimal
    valor: Decimal

    @property
    def day_trade(self) -> bool:
        return "D" in self.obs

    @property
    def chave(self) -> str:
        return chave_titulo(self.especificacao)


@dataclass
class NotaLida:
    corretora: str
    numero: str
    data: date
    arquivo: str = ""
    negocios: list[Negocio] = field(default_factory=list)
    custos: Decimal = ZERO
    irrf: Decimal = ZERO
    liquido: Decimal = ZERO  # positivo = crédito
    avisos: list[str] = field(default_factory=list)

    @property
    def importaveis(self) -> list[Negocio]:
        return [n for n in self.negocios if n.mercado in MERCADOS_IMPORTADOS]

    @property
    def valor_compras(self) -> Decimal:
        return sum((n.valor for n in self.importaveis if n.cv == "C"), ZERO)

    @property
    def valor_vendas(self) -> Decimal:
        return sum((n.valor for n in self.importaveis if n.cv == "V"), ZERO)

    def como_dict(self) -> dict:
        return {
            "corretora": self.corretora, "numero": self.numero, "data": self.data.isoformat(),
            "arquivo": self.arquivo, "custos": str(self.custos), "irrf": str(self.irrf),
            "liquido": str(self.liquido), "avisos": self.avisos,
            "negocios": [
                {"cv": n.cv, "mercado": n.mercado, "especificacao": n.especificacao, "obs": n.obs,
                 "quantidade": n.quantidade, "preco": str(n.preco), "valor": str(n.valor)}
                for n in self.negocios
            ],
        }

    @classmethod
    def de_dict(cls, dados: dict) -> "NotaLida":
        return cls(
            corretora=dados["corretora"], numero=dados["numero"], data=date.fromisoformat(dados["data"]),
            arquivo=dados.get("arquivo", ""), custos=Decimal(dados["custos"]), irrf=Decimal(dados["irrf"]),
            liquido=Decimal(dados["liquido"]), avisos=list(dados.get("avisos", [])),
            negocios=[
                Negocio(cv=n["cv"], mercado=n["mercado"], especificacao=n["especificacao"], obs=n["obs"],
                        quantidade=n["quantidade"], preco=Decimal(n["preco"]), valor=Decimal(n["valor"]))
                for n in dados["negocios"]
            ],
        )


@dataclass
class ResultadoLeitura:
    notas: list[NotaLida] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)


def _linhas(pagina) -> list[list[tuple]]:
    """Palavras da página agrupadas em linhas (mesma altura), cada linha da esquerda para a direita."""
    palavras = sorted(pagina.get_text("words"), key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    linhas: list[list[tuple]] = []
    y_linha = None
    for palavra in palavras:
        y = (palavra[1] + palavra[3]) / 2
        if y_linha is None or y - y_linha > 3:
            linhas.append([])
            y_linha = y
        linhas[-1].append(palavra)
    return [sorted(linha, key=lambda w: w[0]) for linha in linhas]


def _texto(linha) -> str:
    return " ".join(w[4] for w in linha)


def _valor_do_rotulo(textos: list[str], rotulo: str) -> tuple[Decimal, str] | None:
    """Valor (e D/C, se houver) que vem logo depois de um rótulo do resumo financeiro."""
    padrao = re.compile(re.escape(rotulo) + r"\s+(\d{1,3}(?:\.\d{3})*,\d{2})(?:\s+([DC])\b)?")
    for texto in textos:
        achado = padrao.search(texto)
        if achado:
            return _decimal(achado.group(1)), achado.group(2) or ""
    return None


def _cabecalho_negocios(linhas) -> dict[str, float] | None:
    for linha in linhas:
        nomes = {w[4]: w[0] for w in linha}
        if "Quantidade" in nomes and "C/V" in nomes:
            especificacao = next((w[0] for w in linha if w[4].startswith("Especifica")), None)
            obs = nomes.get("Obs.")
            if especificacao is None or obs is None:
                return None
            return {"prazo": nomes.get("Prazo", especificacao), "especificacao": especificacao, "obs": obs}
    return None


def _ler_negocio(linha, colunas: dict[str, float]) -> Negocio | None:
    palavras = [w for w in linha]
    if len(palavras) < 8 or palavras[0][4] != "1-BOVESPA":
        return None
    qtd, preco, valor, dc = (w[4] for w in palavras[-4:])
    if not (_PADRAO_QUANTIDADE.match(qtd) and _PADRAO_NUMERO.match(preco) and _PADRAO_NUMERO.match(valor)):
        return None
    if dc not in {"D", "C"}:
        return None
    cv = palavras[1][4]
    meio = palavras[2:-4]
    mercado = [w[4] for w in meio if w[0] < colunas["prazo"] - 2]
    especificacao = [w[4] for w in meio if colunas["prazo"] - 2 <= w[0] < colunas["obs"] - 4]
    obs = [w[4] for w in meio if w[0] >= colunas["obs"] - 4]
    # o prazo (opções e termo) fica entre o mercado e a especificação - é sempre uma data
    especificacao = [p for p in especificacao if not re.match(r"^\d\d/\d\d(/\d{2,4})?$", p)]
    return Negocio(
        cv=cv, mercado=_sem_acento(" ".join(mercado)).upper(), especificacao=" ".join(especificacao),
        obs="".join(obs), quantidade=int(qtd.replace(".", "")), preco=_decimal(preco), valor=_decimal(valor),
    )


def _corretora(texto: str) -> str:
    for linha in texto.splitlines():
        linha = linha.strip()
        if re.search(r"\b(CTVM|DTVM|CCTVM|CVC|CORRETORA)\b", linha.upper()) and len(linha) < 80:
            return linha
    return "Corretora"


def _dados_cabecalho(texto: str) -> tuple[str, date] | None:
    numero = re.search(r"Nr\.\s*nota\s*\n\s*([\d.]+)", texto)
    data = re.search(r"Data preg[ãa]o\s*\n\s*(\d\d/\d\d/\d{4})", texto)
    if not numero or not data:
        return None
    return numero.group(1).replace(".", ""), datetime.strptime(data.group(1), "%d/%m/%Y").date()


def ler_notas_pdf(conteudo: bytes, nome_arquivo: str = "") -> ResultadoLeitura:
    """Lê todas as notas de um PDF (uma nota pode ocupar várias folhas, com o resumo só na última)."""
    import pymupdf

    try:
        documento = pymupdf.open(stream=conteudo, filetype="pdf")
    except Exception as exc:  # PDF corrompido ou outro tipo de arquivo
        raise NotaInvalida(f"{nome_arquivo or 'Arquivo'}: não é um PDF válido ({exc}).") from exc

    resultado = ResultadoLeitura()
    por_numero: dict[str, NotaLida] = {}
    for indice, pagina in enumerate(documento, start=1):
        texto = pagina.get_text()
        cabecalho = _dados_cabecalho(texto)
        if cabecalho is None:
            resultado.avisos.append(f"{nome_arquivo} - página {indice}: não parece uma nota de negociação, ignorada.")
            continue
        numero, data_pregao = cabecalho
        linhas = _linhas(pagina)
        textos = [_texto(linha) for linha in linhas]

        if "1-BOVESPA" not in texto and "Mercadoria" in texto:
            # na nota BM&F os valores ficam na linha de baixo do rótulo - o último é o total líquido
            liquido = None
            for posicao, linha_texto in enumerate(textos[:-1]):
                if "Total líquido da nota" in linha_texto:
                    valores = re.findall(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*\|?\s*([DC])?", textos[posicao + 1])
                    if valores:
                        liquido = (_decimal(valores[-1][0]), valores[-1][1])
            detalhe = ""
            if liquido:
                sinal = "-" if liquido[1] == "D" else ""
                detalhe = f" Total líquido da nota: R$ {sinal}{liquido[0]}."
            resultado.avisos.append(
                f"Nota {numero} de {data_pregao:%d/%m/%Y} é de mercado futuro (BM&F) e não é importada.{detalhe} "
                "Se foi day trade com prejuízo, informe o valor em \"Prejuízos anteriores ao sistema\" (day trade)."
            )
            continue

        nota = por_numero.get(numero)
        if nota is None:
            nota = por_numero[numero] = NotaLida(
                corretora=_corretora(texto), numero=numero, data=data_pregao, arquivo=nome_arquivo,
            )
        colunas = _cabecalho_negocios(linhas)
        if colunas is None:
            nota.avisos.append(f"Página {indice}: tabela de negócios não reconhecida.")
            continue
        for linha in linhas:
            negocio = _ler_negocio(linha, colunas)
            if negocio:
                nota.negocios.append(negocio)

        liquido = None
        # "Líquido para dd/mm/aaaa valor D/C" - a data fica entre o rótulo e o valor
        for linha_texto in textos:
            achado = re.search(r"Líquido para \d\d/\d\d/\d{4}\s+(\d{1,3}(?:\.\d{3})*,\d{2})(?:\s+([DC]))?", linha_texto)
            if achado:
                liquido = (_decimal(achado.group(1)), achado.group(2) or "")
                break
        if liquido is not None:  # resumo financeiro (só na última folha da nota)
            custos = ZERO
            for rotulo in ("Taxa de liquidação", "Taxa de Registro", "Total Bovespa / Soma", "Total Custos / Despesas"):
                valor = _valor_do_rotulo(textos, rotulo)
                if valor:
                    custos += valor[0]
            nota.custos = custos
            irrf = re.search(r"I\.R\.R\.F\. s/ operações, base R\$\s*[\d.,]+\s+(\d{1,3}(?:\.\d{3})*,\d{2})", "\n".join(textos))
            nota.irrf = _decimal(irrf.group(1)) if irrf else ZERO
            nota.liquido = liquido[0] if liquido[1] != "D" else -liquido[0]

    for nota in por_numero.values():
        _conferir_nota(nota)
        resultado.notas.append(nota)
    if not resultado.notas and not resultado.avisos:
        raise NotaInvalida(f"{nome_arquivo or 'Arquivo'}: nenhuma nota de negociação encontrada.")
    return resultado


def _conferir_nota(nota: NotaLida) -> None:
    """Avisos sobre o que não será importado e sobre totais que não batem com a nota."""
    for negocio in nota.negocios:
        if negocio.mercado not in MERCADOS_IMPORTADOS:
            nota.avisos.append(
                f"{negocio.especificacao} ({negocio.mercado.lower()}) não é importado - "
                "só ações, FIIs, ETFs e BDRs no mercado à vista e fracionário."
            )
        elif _centavos(negocio.preco * negocio.quantidade) != negocio.valor:
            nota.avisos.append(
                f"{negocio.especificacao}: {negocio.quantidade} x {negocio.preco} não dá {negocio.valor} - confira a nota."
            )
    if not nota.negocios:
        nota.avisos.append("Nenhum negócio encontrado na nota.")
        return
    if all(n.mercado in MERCADOS_IMPORTADOS for n in nota.negocios):
        esperado = nota.valor_vendas - nota.valor_compras - nota.custos
        if abs(esperado - nota.liquido) > CENTAVO and abs(esperado - nota.irrf - nota.liquido) > CENTAVO:
            nota.avisos.append(
                f"O líquido da nota (R$ {nota.liquido}) não bate com vendas - compras - custos (R$ {esperado}) - "
                "confira as taxas."
            )


# --------------------------------------------------------------------------
# Especificação do título -> ticker


_MARCAS_IGNORADAS = {
    "NM", "N1", "N2", "MA", "MB", "ATZ", "EJ", "ED", "EDJ", "ER", "EB", "ES", "EC", "EG", "EP", "EX", "EDR", "EBR",
    "ERJ", "ERS", "EJS", "EDS", "#",
}


def _partes_titulo(especificacao: str) -> tuple[str, str]:
    """("MAGAZ LUIZA", "ON") a partir de "MAGAZ LUIZA ON NM"."""
    palavras = especificacao.upper().split()
    for indice in range(len(palavras) - 1, 0, -1):
        if palavras[indice] in SUFIXO_CLASSE:
            return " ".join(palavras[:indice]), palavras[indice]
    palavras = [p for p in palavras if p not in _MARCAS_IGNORADAS]
    return " ".join(palavras), ""


def chave_titulo(especificacao: str) -> str:
    """Especificação sem governança e marcas de "ex" (EJ, ED...), que mudam de um pregão para o outro."""
    nome, classe = _partes_titulo(especificacao)
    return f"{nome} {classe}".strip()


def classe_do_titulo(especificacao: str) -> str:
    _, classe = _partes_titulo(especificacao)
    return SUFIXO_CLASSE.get(classe, ("", ""))[1]


def _palavras(texto: str) -> list[str]:
    return [p for p in re.split(r"[^A-Z0-9]+", _sem_acento(texto).upper()) if p]


def sugerir_ticker(especificacao: str) -> str:
    """Ticker provável: o já confirmado antes, a tabela de nomes de pregão ou a lista de ações da B3."""
    chave = chave_titulo(especificacao)
    salvo = MapeamentoTitulo.objects.filter(especificacao=chave).values_list("ticker", flat=True).first()
    if salvo:
        return salvo
    nome, classe = _partes_titulo(especificacao)
    sufixo = SUFIXO_CLASSE.get(classe, ("", ""))[0]
    if not sufixo:
        return ""
    if nome in RAIZ_POR_NOME:
        return RAIZ_POR_NOME[nome] + sufixo

    termos = _palavras(nome)
    if not termos:
        return ""
    candidatos = []
    for ticker, razao in AcaoB3.objects.filter(ticker__regex=rf"^[A-Z0-9]{{4}}{sufixo}$").values_list("ticker", "nome"):
        palavras_razao = _palavras(razao)
        if all(any(p.startswith(t) for p in palavras_razao) for t in termos):
            candidatos.append(ticker)
    return candidatos[0] if len(candidatos) == 1 else ""


# --------------------------------------------------------------------------
# Gravação


@dataclass
class LinhaImportada:
    data: date
    nota: str
    ticker: str
    cv: str
    quantidade: int
    preco: Decimal
    taxas: Decimal
    destino: str  # "lote novo" ou em quais lotes a venda deu baixa
    day_trade: bool = False

    @property
    def valor(self) -> Decimal:
        return _centavos(self.preco * self.quantidade)


@dataclass
class ResumoImportacao:
    notas_importadas: list[NotaCorretagem] = field(default_factory=list)
    notas_repetidas: list[str] = field(default_factory=list)
    linhas: list[LinhaImportada] = field(default_factory=list)


def _ratear(total: Decimal, pesos: list[Decimal]) -> list[Decimal]:
    """Divide um valor em centavos proporcionalmente aos pesos - o arredondamento vai para a maior parte."""
    soma = sum(pesos, ZERO)
    if not pesos or not soma:
        return [ZERO] * len(pesos)
    partes = [_centavos(total * peso / soma) for peso in pesos]
    maior = max(range(len(pesos)), key=lambda i: pesos[i])
    partes[maior] += total - sum(partes, ZERO)
    return partes


def _ativo(ticker: str, especificacao: str) -> Ativo:
    ativo = Ativo.objects.filter(ticker=ticker).first()
    if ativo:
        return ativo
    nome = AcaoB3.objects.filter(ticker=ticker).values_list("nome", flat=True).first() or ""
    return Ativo.objects.create(ticker=ticker, nome=nome, classe=classe_do_titulo(especificacao))


def _consolidar(nota: NotaLida, tickers: dict[str, str]) -> list[dict]:
    """Negócios iguais da nota (mesmo ativo, lado e preço) viram uma linha só, na ordem da nota."""
    grupos: dict[tuple, dict] = {}
    for negocio in nota.importaveis:
        ticker = (tickers.get(negocio.chave) or "").strip().upper()
        if not ticker:
            raise ImportacaoInvalida(f"Informe o ticker de \"{negocio.especificacao}\".")
        chave = (negocio.cv, ticker, negocio.preco)
        grupo = grupos.setdefault(chave, {
            "cv": negocio.cv, "ticker": ticker, "preco": negocio.preco, "quantidade": 0, "valor": ZERO,
            "especificacao": negocio.especificacao, "day_trade": False,
        })
        grupo["quantidade"] += negocio.quantidade
        grupo["valor"] += negocio.valor
        grupo["day_trade"] = grupo["day_trade"] or negocio.day_trade
    lista = list(grupos.values())
    for grupo, taxa in zip(lista, _ratear(nota.custos, [g["valor"] for g in lista])):
        grupo["taxas"] = taxa
    return lista


def _compra_lancada_a_mao(usuario, nota: NotaLida, grupo: dict) -> Operacao | None:
    """
    Compra já lançada à mão que corresponde a esta linha da nota: mesmo ativo,
    data e quantidade, sem nota vinculada e sem ajuste de evento corporativo.
    O preço pode diferir um pouco (o lançado à mão costuma ser aproximado).
    """
    return (
        Operacao.objects.filter(
            usuario=usuario, tipo=Operacao.COMPRA, ativo__ticker=grupo["ticker"], data_operacao=nota.data,
            quantidade=grupo["quantidade"], evento__isnull=True, quantidade_original__isnull=True,
            notas_corretagem__isnull=True,
        )
        .order_by("pk")
        .first()
    )


def _dar_baixa(usuario, nota: NotaLida, registro: NotaCorretagem, grupo: dict) -> str:
    """Grava a venda nos lotes com saldo: primeiro os comprados no mesmo dia, depois os mais antigos."""
    lotes = [
        lote for lote in Operacao.objects.filter(
            usuario=usuario, tipo=Operacao.COMPRA, ativo__ticker=grupo["ticker"], data_operacao__lte=nota.data,
        ).order_by("data_operacao", "pk")
        if lote.saldo > 0
    ]
    lotes.sort(key=lambda lote: (lote.data_operacao != nota.data, lote.data_operacao, lote.pk))
    disponivel = sum(lote.saldo for lote in lotes)
    if disponivel < grupo["quantidade"]:
        raise ImportacaoInvalida(
            f"Nota {nota.numero} ({nota.data:%d/%m/%Y}): venda de {grupo['quantidade']} {grupo['ticker']}, mas só há "
            f"{disponivel} em carteira nessa data. Importe antes a nota da compra ou lance a compra à mão."
        )
    restante = grupo["quantidade"]
    partes = []
    for lote in lotes:
        if not restante:
            break
        quantidade = min(restante, lote.saldo)
        partes.append((lote, quantidade))
        restante -= quantidade
    destinos = []
    for (lote, quantidade), taxa in zip(partes, _ratear(grupo["taxas"], [Decimal(q) for _, q in partes])):
        venda = VendaLote.objects.create(
            operacao=lote, quantidade=quantidade, preco=grupo["preco"], data=nota.data, taxas=taxa,
        )
        registro.vendas.add(venda)
        recalcular_resumo_vendas(lote)
        destinos.append(f"{quantidade} do lote de {lote.data_operacao:%d/%m}")
    return ", ".join(destinos)


@transaction.atomic
def importar_notas(usuario, notas: list[NotaLida], tickers: dict[str, str]) -> ResumoImportacao:
    """
    Grava as notas, da mais antiga para a mais recente (compras de cada nota
    antes das vendas). Nota já importada (mesma corretora e número) é pulada.
    Tudo ou nada: se uma venda não tiver saldo, nada é gravado.
    """
    resumo = ResumoImportacao()
    for nota in sorted(notas, key=lambda n: (n.data, n.numero)):
        if NotaCorretagem.objects.filter(usuario=usuario, corretora=nota.corretora, numero=nota.numero).exists():
            resumo.notas_repetidas.append(f"{nota.numero} ({nota.data:%d/%m/%Y})")
            continue
        grupos = _consolidar(nota, tickers)
        registro = NotaCorretagem.objects.create(
            usuario=usuario, corretora=nota.corretora, numero=nota.numero, data_pregao=nota.data,
            arquivo=nota.arquivo[:255], valor_compras=nota.valor_compras, valor_vendas=nota.valor_vendas,
            custos=nota.custos, irrf=nota.irrf, liquido=nota.liquido,
        )
        for grupo in (g for g in grupos if g["cv"] == "C"):
            lote = _compra_lancada_a_mao(usuario, nota, grupo)
            if lote:
                # a compra já foi lançada à mão: a nota assume esse lote (com o
                # preço e as taxas oficiais) em vez de duplicar a posição
                lote.preco_unitario = grupo["preco"]
                lote.taxas_compra = grupo["taxas"]
                lote.observacao = " ".join(filter(None, [
                    lote.observacao, f"Vinculada à nota {nota.numero} ({nota.corretora}).",
                ]))
                lote.save(update_fields=["preco_unitario", "taxas_compra", "observacao"])
                destino = "lote existente"
            else:
                lote = Operacao.objects.create(
                    usuario=usuario, ativo=_ativo(grupo["ticker"], grupo["especificacao"]), tipo=Operacao.COMPRA,
                    quantidade=grupo["quantidade"], preco_unitario=grupo["preco"], data_operacao=nota.data,
                    taxas_compra=grupo["taxas"], observacao=f"Importada da nota {nota.numero} ({nota.corretora}).",
                )
                destino = "lote novo"
            sincronizar_lancamento_compra(lote)
            registro.operacoes.add(lote)
            resumo.linhas.append(LinhaImportada(
                data=nota.data, nota=nota.numero, ticker=grupo["ticker"], cv="C", quantidade=grupo["quantidade"],
                preco=grupo["preco"], taxas=grupo["taxas"], destino=destino, day_trade=grupo["day_trade"],
            ))
        for grupo in (g for g in grupos if g["cv"] == "V"):
            _ativo(grupo["ticker"], grupo["especificacao"])
            destino = _dar_baixa(usuario, nota, registro, grupo)
            resumo.linhas.append(LinhaImportada(
                data=nota.data, nota=nota.numero, ticker=grupo["ticker"], cv="V", quantidade=grupo["quantidade"],
                preco=grupo["preco"], taxas=grupo["taxas"], destino=destino, day_trade=grupo["day_trade"],
            ))
        resumo.notas_importadas.append(registro)

    for nota in notas:
        for negocio in nota.importaveis:
            ticker = (tickers.get(negocio.chave) or "").strip().upper()
            if ticker:
                MapeamentoTitulo.objects.update_or_create(especificacao=negocio.chave, defaults={"ticker": ticker})
    return resumo


def simular_importacao(usuario, notas: list[NotaLida], tickers: dict[str, str]):
    """
    Prévia: grava as notas dentro de uma transação, apura o IR dos meses
    afetados e desfaz tudo. Devolve (resumo, meses apurados, erro).
    """
    from .services import apurar_meses

    class _Desfazer(Exception):
        pass

    saida = {}
    try:
        with transaction.atomic():
            resumo = importar_notas(usuario, notas, tickers)
            meses_afetados = {(n.data.year, n.data.month) for n in notas}
            saida["resumo"] = resumo
            saida["meses"] = [m for m in apurar_meses(usuario) if (m.ano, m.mes) in meses_afetados]
            raise _Desfazer
    except _Desfazer:
        return saida["resumo"], saida["meses"], ""
    except ImportacaoInvalida as exc:
        return None, [], str(exc)


# --------------------------------------------------------------------------
# Exclusão


@transaction.atomic
def excluir_notas(usuario, notas) -> int:
    """
    Apaga as notas e as compras e vendas criadas por elas (com os lançamentos
    da conta corrente). Bloqueia se uma compra dessas notas tiver venda que
    não é delas - excluir a compra apagaria essa venda junto.
    """
    notas = list(notas.filter(usuario=usuario).prefetch_related("operacoes", "vendas"))
    if not notas:
        return 0
    ids_vendas = {venda.pk for nota in notas for venda in nota.vendas.all()}
    ids_lotes = {lote.pk for nota in notas for lote in nota.operacoes.all()}
    conflitos = (
        VendaLote.objects.filter(operacao_id__in=ids_lotes).exclude(pk__in=ids_vendas)
        .select_related("operacao__ativo").order_by("data")
    )
    if conflitos:
        descricao = "; ".join(
            f"{v.quantidade} {v.operacao.ativo.ticker} vendida em {v.data:%d/%m/%Y}" for v in conflitos[:5]
        )
        raise ExclusaoBloqueada(
            "Compras destas notas já têm vendas de outras notas ou lançadas à mão "
            f"({descricao}). Exclua primeiro essas vendas (ou as notas delas)."
        )
    lotes_com_baixa = set(
        VendaLote.objects.filter(pk__in=ids_vendas).exclude(operacao_id__in=ids_lotes).values_list("operacao_id", flat=True)
    )
    VendaLote.objects.filter(pk__in=ids_vendas).delete()
    Operacao.objects.filter(pk__in=ids_lotes, usuario=usuario).delete()
    for lote in Operacao.objects.filter(pk__in=lotes_com_baixa):
        recalcular_resumo_vendas(lote)
    NotaCorretagem.objects.filter(pk__in=[n.pk for n in notas]).delete()
    return len(notas)

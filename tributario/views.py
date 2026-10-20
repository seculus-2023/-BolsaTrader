from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from core.models import Operacao

from .services import COMUM, DAY_TRADE, MODALIDADE_LABELS, apurar_operacoes


def _preco_medio_dos_lotes(usuario) -> dict[str, Decimal]:
    """Preço médio simples dos lotes ainda em carteira, por ativo - a visão da tela de operações, para comparar com o fiscal."""
    totais: dict[str, list] = {}
    for lote in Operacao.objects.filter(usuario=usuario, tipo=Operacao.COMPRA).select_related("ativo"):
        if lote.saldo <= 0:
            continue
        quantidade_custo = totais.setdefault(lote.ativo.ticker, [0, Decimal("0")])
        quantidade_custo[0] += lote.saldo
        quantidade_custo[1] += lote.saldo * lote.preco_unitario
    return {
        ticker: (custo / quantidade).quantize(Decimal("0.01"))
        for ticker, (quantidade, custo) in totais.items() if quantidade
    }


@login_required
def posicao_fiscal(request):
    """
    Posição de cada ativo pelo preço médio ponderado (o custo que vai para a
    declaração em Bens e Direitos) e o ganho/perda de cada venda separado em
    operação comum e day trade, no ano escolhido.
    """
    apuracao = apurar_operacoes(request.user)
    anos = apuracao.anos_com_vendas()
    hoje = timezone.localdate()
    try:
        ano = int(request.GET.get("ano", ""))
    except ValueError:
        ano = anos[0] if anos else hoje.year
    if ano not in anos:
        anos = sorted({*anos, ano}, reverse=True)

    precos_lotes = _preco_medio_dos_lotes(request.user)
    posicoes = [
        {"posicao": posicao, "preco_medio_lotes": precos_lotes.get(posicao.ticker)}
        for posicao in apuracao.posicoes_em_aberto()
    ]

    resultados = sorted(apuracao.resultados_do_ano(ano), key=lambda r: (r.data, r.ticker), reverse=True)
    totais = []
    for modalidade in (COMUM, DAY_TRADE):
        do_tipo = [r for r in resultados if r.modalidade == modalidade]
        if do_tipo:
            totais.append({
                "label": MODALIDADE_LABELS[modalidade],
                "vendido": sum((r.valor_venda for r in do_tipo), Decimal("0")).quantize(Decimal("0.01")),
                "resultado": sum((r.resultado for r in do_tipo), Decimal("0")),
            })

    return render(request, "tributario/posicao_fiscal.html", {
        "posicoes": posicoes,
        "custo_total_carteira": sum((p["posicao"].custo_total_centavos for p in posicoes), Decimal("0")),
        "resultados": resultados,
        "totais": totais,
        "ano": ano,
        "anos": anos,
        "inconsistencias": apuracao.inconsistencias,
        "data_referencia": hoje,
    })

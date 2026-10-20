from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import Operacao

from .forms import SaldoInicialFiscalForm
from .models import SaldoInicialFiscal
from .services import (
    COMUM, DAY_TRADE, GRUPO_COMUM, GRUPO_LABELS, GRUPOS, MODALIDADE_LABELS, apurar_meses, apurar_operacoes,
    parametro_vigente, prejuizos_iniciais_do_usuario,
)


def _ano_escolhido(request, anos: list[int]) -> tuple[int, list[int]]:
    """Ano do filtro (?ano=), ou o mais recente com vendas; garante que ele esteja na lista do combo."""
    try:
        ano = int(request.GET.get("ano", ""))
    except ValueError:
        ano = anos[0] if anos else timezone.localdate().year
    return ano, sorted({*anos, ano}, reverse=True)


@login_required
def imposto_renda(request):
    """
    Painel do imposto de renda sobre a bolsa: quanto ainda dá para vender no
    mês com isenção, a apuração mês a mês do ano escolhido (resultado por
    grupo, prejuízo compensado, IR retido, imposto a pagar e vencimento) e os
    prejuízos que ainda podem ser compensados.
    """
    hoje = timezone.localdate()
    apuracao = apurar_operacoes(request.user)
    meses = apurar_meses(request.user, apuracao)
    ano, anos = _ano_escolhido(request, sorted({m.ano for m in meses}, reverse=True))

    mes_atual = next((m for m in meses if (m.ano, m.mes) == (hoje.year, hoje.month)), None)
    parametro = mes_atual.parametro if mes_atual else parametro_vigente(hoje)
    vendas_acoes_mes = mes_atual.vendas_acoes if mes_atual else Decimal("0")
    limite = parametro.limite_isencao_acoes

    meses_do_ano = [m for m in meses if m.ano == ano]
    prejuizos = (
        {grupo: meses[-1].grupos[grupo].prejuizo_a_compensar for grupo in GRUPOS}
        if meses else prejuizos_iniciais_do_usuario(request.user)
    )
    saldo_inicial = SaldoInicialFiscal.objects.filter(usuario=request.user).first()

    return render(request, "tributario/imposto_renda.html", {
        "hoje": hoje,
        "mes_atual": mes_atual,
        "vendas_acoes_mes": vendas_acoes_mes,
        "limite_isencao": limite,
        "disponivel_isencao": max(Decimal("0"), limite - vendas_acoes_mes),
        "percentual_limite": min(100, int(vendas_acoes_mes / limite * 100)) if limite else 100,
        "parametro": parametro,
        "meses": list(reversed(meses_do_ano)),
        "total_imposto_ano": sum((m.imposto_bruto for m in meses_do_ano), Decimal("0")),
        "total_irrf_ano": sum((m.irrf_mes for m in meses_do_ano), Decimal("0")),
        "total_darf_ano": sum((m.valor_darf for m in meses_do_ano), Decimal("0")),
        "total_isento_ano": sum((m.grupos[GRUPO_COMUM].isento for m in meses_do_ano), Decimal("0")),
        "prejuizos": [{"label": GRUPO_LABELS[g], "valor": prejuizos[g]} for g in GRUPOS],
        "proximo_darf": next((m for m in meses if m.valor_darf and m.vencimento >= hoje), None),
        "ano": ano,
        "anos": anos,
        "inconsistencias": apuracao.inconsistencias,
        "form_saldo": SaldoInicialFiscalForm(instance=saldo_inicial),
    })


@login_required
@require_POST
def saldo_inicial_salvar(request):
    """Grava os prejuízos acumulados antes das operações cadastradas no sistema."""
    saldo, _ = SaldoInicialFiscal.objects.get_or_create(usuario=request.user)
    form = SaldoInicialFiscalForm(request.POST, instance=saldo)
    if form.is_valid():
        form.save()
        messages.success(request, "Prejuízos anteriores salvos - a apuração já considera os novos valores.")
    else:
        messages.error(request, "Não foi possível salvar: informe os prejuízos como valores positivos.")
    return redirect("tributario:imposto_renda")


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
    ano, anos = _ano_escolhido(request, apuracao.anos_com_vendas())

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
        "data_referencia": timezone.localdate(),
    })

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET, require_POST

from core.models import Operacao, VendaLote

from .darf import URL_SICALC, acrescimos_ate, desfazer_pagamento, registrar_pagamento, situacao_das_darfs
from .forms import PagamentoDarfForm, SaldoInicialFiscalForm
from .models import Darf, SaldoInicialFiscal
from .services import (
    COMUM, DAY_TRADE, GRUPO_COMUM, GRUPO_LABELS, GRUPOS, MODALIDADE_LABELS, apurar_meses, apurar_operacoes,
    parametro_vigente, prejuizos_iniciais_do_usuario, simular_venda,
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
    situacoes = situacao_das_darfs(request.user, meses)
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
        "proximo_darf": next(
            iter(sorted((s for s in situacoes if s.status != "PAGA"), key=lambda s: s.darf.vencimento)), None
        ),
        "darfs_vencidas": sum(1 for s in situacoes if s.status == "VENCIDA"),
        "ano": ano,
        "anos": anos,
        "inconsistencias": apuracao.inconsistencias,
        "form_saldo": SaldoInicialFiscalForm(instance=saldo_inicial),
    })


def _decimal_do_get(request, nome: str, padrao: str | None = None) -> Decimal:
    valor = (request.GET.get(nome) or padrao or "").strip().replace(",", ".")
    return Decimal(valor)


@login_required
@require_GET
def simular_venda_view(request):
    """
    Efeito de uma venda no imposto ANTES de salvar (chamado pela tela de
    venda enquanto o usuário digita): vendas de ações do mês com e sem ela,
    se perde a isenção, ganho pelo preço médio, IR adicional e lucro após IR.
    """
    operacao = get_object_or_404(
        Operacao, id=request.GET.get("operacao") or 0, usuario=request.user, tipo=Operacao.COMPRA
    )
    try:
        quantidade = int(request.GET.get("quantidade") or 0)
        preco = _decimal_do_get(request, "preco")
        percentual = _decimal_do_get(request, "percentual_corretora", "0")
        taxas = _decimal_do_get(request, "taxas", "0")
        data = parse_date(request.GET.get("data") or "") or timezone.localdate()
    except (ValueError, InvalidOperation):
        return JsonResponse({"ok": False, "erro": "Preencha quantidade e preço para simular o imposto."})
    if quantidade <= 0 or preco <= 0:
        return JsonResponse({"ok": False, "erro": "Preencha quantidade e preço para simular o imposto."})

    venda = VendaLote(
        operacao=operacao, quantidade=quantidade, preco=preco, data=data,
        percentual_corretora=percentual, taxas=taxas,
    )
    venda_id = request.GET.get("venda")
    if venda_id:
        venda.pk = get_object_or_404(VendaLote, id=venda_id, operacao=operacao).pk
    return JsonResponse({"ok": True, **simular_venda(request.user, venda).como_dict()})


@login_required
def darf_lista(request):
    """Guias DARF de cada mês com imposto: em aberto, vencidas (com multa e juros até hoje) e pagas."""
    situacoes = situacao_das_darfs(request.user)
    return render(request, "tributario/darfs.html", {
        "situacoes": situacoes,
        "em_aberto": sum((s.total_hoje for s in situacoes if s.status != "PAGA"), Decimal("0")),
        "vencidas": sum(1 for s in situacoes if s.status == "VENCIDA"),
        "url_sicalc": URL_SICALC,
        "hoje": timezone.localdate(),
    })


@login_required
def darf_pagar(request, darf_id):
    """Registra o pagamento de uma guia (data e valor pagos) e lança o débito na conta corrente."""
    darf = get_object_or_404(Darf, id=darf_id, usuario=request.user)
    hoje = timezone.localdate()

    if request.method == "POST":
        form = PagamentoDarfForm(request.POST)
        if form.is_valid():
            registrar_pagamento(darf, form.cleaned_data["data_pagamento"], form.cleaned_data["valor_pago"])
            messages.success(
                request,
                f"DARF de {darf.competencia:%m/%Y} marcada como paga - débito de R$ {darf.valor_pago} "
                "lançado na conta corrente.",
            )
            return redirect("tributario:darf_lista")
    else:
        data_inicial = darf.data_pagamento or hoje
        acrescimos = acrescimos_ate(darf, data_inicial)
        form = PagamentoDarfForm(initial={
            "data_pagamento": data_inicial,
            "valor_pago": darf.valor_pago or darf.valor_principal + acrescimos.multa + acrescimos.juros,
        })

    acrescimos_hoje = acrescimos_ate(darf, hoje)
    return render(request, "tributario/darf_pagar.html", {
        "darf": darf,
        "form": form,
        "acrescimos": acrescimos_hoje,
        "total_hoje": darf.valor_principal + acrescimos_hoje.multa + acrescimos_hoje.juros,
        "url_sicalc": URL_SICALC,
    })


@login_required
@require_POST
def darf_desfazer_pagamento(request, darf_id):
    """Volta uma guia marcada como paga por engano para "em aberto" e remove o débito da conta corrente."""
    darf = get_object_or_404(Darf, id=darf_id, usuario=request.user, data_pagamento__isnull=False)
    desfazer_pagamento(darf)
    messages.success(request, f"Pagamento da DARF de {darf.competencia:%m/%Y} desfeito - o débito saiu da conta corrente.")
    return redirect("tributario:darf_lista")


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

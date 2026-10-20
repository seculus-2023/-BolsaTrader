from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_GET, require_POST

from core.models import Ativo, Operacao, VendaLote

from .darf import URL_SICALC, acrescimos_ate, desfazer_pagamento, registrar_pagamento, situacao_das_darfs
from .eventos import EventoInvalido, aplicar_evento, desfazer_evento
from .forms import EventoCorporativoForm, PagamentoDarfForm, ProventoForm, SaldoInicialFiscalForm
from .models import Darf, EventoCorporativo, Provento, SaldoInicialFiscal
from .proventos import excluir_provento, ir_retido_sugerido, sincronizar_lancamento_provento
from .relatorio import AVISO as AVISO_RELATORIO
from .relatorio import gerar_excel_relatorio_anual, gerar_pdf_relatorio_anual, gerar_relatorio_anual
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


def _salvar_provento(request, form: ProventoForm) -> Provento:
    provento = form.save(commit=False)
    provento.usuario = request.user
    provento.ativo, _ = Ativo.objects.get_or_create(ticker=form.cleaned_data["ticker"])
    if form.cleaned_data.get("ir_retido") is None:
        provento.ir_retido = ir_retido_sugerido(
            request.user, provento.ativo, provento.tipo, provento.data_pagamento, provento.valor_bruto,
            excluir_id=provento.pk,
        )
    provento.save()
    sincronizar_lancamento_provento(provento, form.cleaned_data.get("lancar_conta_corrente", False))
    return provento


@login_required
def proventos(request):
    """Dividendos, JCP e rendimentos de FII recebidos no ano escolhido, com cadastro de novos."""
    if request.method == "POST":
        form = ProventoForm(request.POST)
        if form.is_valid():
            provento = _salvar_provento(request, form)
            messages.success(
                request,
                f"{provento.get_tipo_display()} de {provento.ativo.ticker} registrado: bruto R$ {provento.valor_bruto}, "
                f"IR retido R$ {provento.ir_retido}, líquido R$ {provento.valor_liquido}.",
            )
            return redirect(f"{reverse('tributario:proventos')}?ano={provento.data_pagamento.year}")
    else:
        form = ProventoForm()

    todos = Provento.objects.filter(usuario=request.user).select_related("ativo")
    ano, anos = _ano_escolhido(request, sorted({p.data_pagamento.year for p in todos}, reverse=True))
    do_ano = [p for p in todos if p.data_pagamento.year == ano]
    totais = []
    for tipo, rotulo in Provento.TIPO_CHOICES:
        do_tipo = [p for p in do_ano if p.tipo == tipo]
        if do_tipo:
            totais.append({
                "label": rotulo,
                "bruto": sum((p.valor_bruto for p in do_tipo), Decimal("0")),
                "ir": sum((p.ir_retido for p in do_tipo), Decimal("0")),
                "liquido": sum((p.valor_liquido for p in do_tipo), Decimal("0")),
            })
    return render(request, "tributario/proventos.html", {
        "form": form, "proventos": do_ano, "totais": totais, "ano": ano, "anos": anos,
    })


@login_required
def provento_editar(request, provento_id):
    provento = get_object_or_404(Provento, id=provento_id, usuario=request.user)
    if request.method == "POST":
        form = ProventoForm(request.POST, instance=provento)
        if form.is_valid():
            provento = _salvar_provento(request, form)
            messages.success(request, f"{provento.get_tipo_display()} de {provento.ativo.ticker} atualizado.")
            return redirect(f"{reverse('tributario:proventos')}?ano={provento.data_pagamento.year}")
    else:
        form = ProventoForm(instance=provento)
    return render(request, "tributario/provento_editar.html", {"form": form, "provento": provento})


@login_required
@require_POST
def provento_excluir(request, provento_id):
    provento = get_object_or_404(Provento, id=provento_id, usuario=request.user)
    ano = provento.data_pagamento.year
    excluir_provento(provento)
    messages.success(request, "Provento excluído (e o crédito na conta corrente, se havia).")
    return redirect(f"{reverse('tributario:proventos')}?ano={ano}")


@login_required
def eventos_corporativos(request):
    """Desdobramentos, grupamentos e bonificações cadastrados, com o formulário para registrar um novo."""
    if request.method == "POST":
        form = EventoCorporativoForm(request.POST, usuario=request.user)
        if form.is_valid():
            evento = form.save(commit=False)
            evento.usuario = request.user
            try:
                with transaction.atomic():
                    evento.save()
                    criados = aplicar_evento(evento)
            except EventoInvalido as exc:
                form.add_error(None, str(exc))
            else:
                ajuste = ", ".join(f"{lote.quantidade} a R$ {lote.preco_unitario}" for lote in criados)
                messages.success(request, f"{evento} registrado - lote(s) ajustado(s): {ajuste}.")
                return redirect("tributario:eventos_corporativos")
    else:
        form = EventoCorporativoForm(usuario=request.user)

    eventos = EventoCorporativo.objects.filter(usuario=request.user).select_related("ativo").prefetch_related("lotes_gerados")
    return render(request, "tributario/eventos.html", {"form": form, "eventos": eventos})


@login_required
@require_POST
def evento_desfazer(request, evento_id):
    evento = get_object_or_404(EventoCorporativo, id=evento_id, usuario=request.user)
    descricao = str(evento)
    try:
        desfazer_evento(evento)
    except EventoInvalido as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{descricao} desfeito - os lotes voltaram ao que eram antes.")
    return redirect("tributario:eventos_corporativos")


def _ano_relatorio(request) -> int:
    try:
        return int(request.GET.get("ano", ""))
    except ValueError:
        return timezone.localdate().year - 1  # a declaração é sempre do ano anterior


@login_required
def relatorio_anual(request):
    """Relatório para a declaração de IR: o que preencher em cada ficha no ano-calendário escolhido."""
    ano = _ano_relatorio(request)
    hoje = timezone.localdate()
    return render(request, "tributario/relatorio_anual.html", {
        "secoes": gerar_relatorio_anual(request.user, ano),
        "ano": ano,
        "anos": list(range(hoje.year, hoje.year - 6, -1)),
        "aviso": AVISO_RELATORIO,
    })


def _download(conteudo: bytes, tipo: str, nome: str) -> HttpResponse:
    resposta = HttpResponse(conteudo, content_type=tipo)
    resposta["Content-Disposition"] = f'attachment; filename="{nome}"'
    return resposta


@login_required
def relatorio_anual_excel(request):
    ano = _ano_relatorio(request)
    nome = request.user.first_name or request.user.username
    conteudo = gerar_excel_relatorio_anual(gerar_relatorio_anual(request.user, ano), ano, nome)
    return _download(
        conteudo, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", f"declaracao_ir_{ano}.xlsx",
    )


@login_required
def relatorio_anual_pdf(request):
    ano = _ano_relatorio(request)
    nome = request.user.first_name or request.user.username
    conteudo = gerar_pdf_relatorio_anual(gerar_relatorio_anual(request.user, ano), ano, nome)
    return _download(conteudo, "application/pdf", f"declaracao_ir_{ano}.pdf")


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

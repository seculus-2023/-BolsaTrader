import calendar
import hashlib
import hmac
import json
import re
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Max
from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .forms import (
    OperacaoForm,
    VendaLoteForm,
    ConfirmarCompraForm,
    EditarCompraForm,
    EditarReservaForm,
    FonteNoticiaForm,
    SaldoInicialContaCorrenteForm,
    TransferenciaContaCorrenteForm,
)
from .models import (
    Operacao, Alerta, Ativo, Cotacao, MensagemWhatsapp, FonteNoticia, AcaoB3, RegistroAtualizacaoCarteira,
    LancamentoContaCorrente,
)
from .services import (
    calcular_posicoes,
    gerar_alertas_para_usuario,
    gerar_sinais_robo_para_usuario,
    limpar_alertas_antigos,
    mercado_b3_aberto,
    atualizar_cotacao_diaria,
    atualizar_cotacoes_ativos,
    ativos_distintos_comprados,
    consumo_api_ultimos_30_dias,
    limite_automatico_api,
    limite_absoluto_api,
    orcamento_automatico_disponivel,
    projetar_consumo_mensal,
    analisar_indicadores_tecnicos,
    buscar_cotacao_atual_com_historico,
    calcular_rsi,
    _classificar_rsi,
    RSI_LABELS,
    RSI_CLASSES,
    buscar_maiores_variacoes,
    sincronizar_acoes_b3,
    atividade_recente,
    construir_grafico_cotacoes,
    construir_comparativo_valores,
    gerar_excel_posicoes,
    gerar_pdf_posicoes,
    gerar_excel_operacoes,
    gerar_pdf_operacoes,
    gerar_excel_robo,
    gerar_pdf_robo,
    atualizar_noticias_fonte,
    NoticiaScrapingError,
    BrapiError,
    backfill_historico_cotacoes,
    backtest_sinais_robo,
    calcular_concentracao_setor,
    calcular_metricas_risco,
    calcular_comparativo_benchmark,
    atualizar_benchmarks,
    registrar_atualizacao_carteira,
    excluir_registros_atualizacao_antigos,
    excluir_registros_atualizacao_por_periodo,
    calcular_variacoes_historico,
    construir_grafico_atualizacoes_dia,
    gerar_excel_historico_atualizacoes,
    gerar_pdf_historico_atualizacoes,
    obter_ou_criar_conta_corrente,
    saldo_conta_corrente,
    sincronizar_lancamento_compra,
    sincronizar_lancamento_venda,
    registrar_transferencia_conta_corrente,
    extrato_conta_corrente,
    gerar_excel_extrato_conta_corrente,
    gerar_pdf_extrato_conta_corrente,
    escanear_carteira,
    salvar_post_it,
)

TICKER_VALIDO = re.compile(r"^[A-Z0-9]{1,15}$")


@login_required
def dashboard(request):
    """
    Painel principal: resumo da carteira e lucro/perda consolidado. "Ações
    rápidas" e "Alertas recentes" foram para Posições em Carteira, e
    "Atividade da comunidade" para Histórico de Atualizações - o Painel ficou
    só com o resumo mais direto.
    """
    posicoes = calcular_posicoes(request.user)
    posicoes_compradas = [p for p in posicoes if not p.apenas_reservado]

    valor_investido_total = sum((p.valor_investido for p in posicoes), Decimal("0"))
    valor_atual_total = sum((p.valor_atual for p in posicoes if p.valor_atual is not None), Decimal("0"))
    lucro_perda_total = valor_atual_total - valor_investido_total
    lucro_perda_pct_total = (
        (lucro_perda_total / valor_investido_total) * 100 if valor_investido_total else None
    )

    contexto = {
        # só as compradas de verdade - reservas (intenção de compra) não
        # aparecem mais nessa tabela resumida do Painel (ver Posições em
        # Carteira para o detalhamento completo, com reservas inclusive).
        "posicoes": posicoes_compradas,
        "valor_investido_total": valor_investido_total,
        "valor_atual_total": valor_atual_total,
        "lucro_perda_total": lucro_perda_total,
        "lucro_perda_pct_total": lucro_perda_pct_total,
        "total_ativos": len(posicoes_compradas),
        "comparativo_benchmark": calcular_comparativo_benchmark(request.user, posicoes=posicoes),
    }
    # "Variações de hoje" (mesmo gráfico de Histórico de Atualizações) ao
    # lado de "Posições em carteira" - só precisa de grafico_dia/
    # total_registros_hoje daqui, mas reaproveita o helper inteiro.
    contexto.update(_contexto_historico_atualizacoes(request.user, limite=20))
    return render(request, "core/dashboard.html", contexto)


@login_required
def menu(request):
    """Menu principal do sistema com atalhos para todas as áreas."""
    return render(request, "core/menu.html")


_CHAVES_PERIODO = [
    "data_de_carteira", "data_ate_carteira",
    "data_de_reservada", "data_ate_reservada",
    "data_de_venda", "data_ate_venda",
]


def _periodo_e_operacoes(request):
    """
    Resolve os filtros de período de cada grid - Em carteira e Reservadas
    filtram pela data da operação (compra/reserva); Vendidas filtra pela data
    da VENDA, não da compra, já que o que importa nessa grid é quando o lote
    foi vendido - e o filtro de ativo (?ativo=TICKER), comum às três. Retorna
    as operações do usuário já filtradas, divididas nos três estados em que
    uma operação pode estar.

    Usado tanto pela listagem quanto pelos relatórios (Excel/PDF), para que
    o relatório sempre reflita exatamente o mesmo filtro aplicado na tela.

    Sem nenhum filtro de data na URL (ex: ao abrir a página pelo menu), o
    padrão é o mês atual (dia 1 até o último dia) para as três grids; "Limpar
    todos os filtros" manda os campos vazios de propósito, pra mostrar tudo
    em vez de cair de novo no padrão do mês atual.
    """
    if any(chave in request.GET for chave in _CHAVES_PERIODO):
        valores = {chave: request.GET.get(chave, "") for chave in _CHAVES_PERIODO}
    else:
        hoje = timezone.localdate()
        ultimo_dia_mes = calendar.monthrange(hoje.year, hoje.month)[1]
        primeiro_dia_str = hoje.replace(day=1).isoformat()
        ultimo_dia_str = hoje.replace(day=ultimo_dia_mes).isoformat()
        valores = {
            chave: (primeiro_dia_str if chave.startswith("data_de") else ultimo_dia_str)
            for chave in _CHAVES_PERIODO
        }

    datas = {chave: (parse_date(valor) if valor else None) for chave, valor in valores.items()}
    ativo_selecionado = request.GET.get("ativo", "").strip().upper()

    todas_operacoes = Operacao.objects.filter(usuario=request.user).select_related("ativo")
    existe_alguma_operacao = todas_operacoes.exists()
    tickers_disponiveis = list(
        todas_operacoes.order_by("ativo__ticker").values_list("ativo__ticker", flat=True).distinct()
    )
    if ativo_selecionado:
        todas_operacoes = todas_operacoes.filter(ativo__ticker=ativo_selecionado)
    todas_operacoes = list(todas_operacoes)

    def no_periodo(valor_data, chave_de: str, chave_ate: str) -> bool:
        data_de, data_ate = datas[chave_de], datas[chave_ate]
        if data_de and (valor_data is None or valor_data < data_de):
            return False
        if data_ate and (valor_data is None or valor_data > data_ate):
            return False
        return True

    operacoes_reservadas = [
        op for op in todas_operacoes
        if op.tipo == Operacao.RESERVAR
        and no_periodo(op.data_operacao, "data_de_reservada", "data_ate_reservada")
    ]
    operacoes_compradas = [
        op for op in todas_operacoes
        if op.tipo == Operacao.COMPRA and op.saldo > 0
        and no_periodo(op.data_operacao, "data_de_carteira", "data_ate_carteira")
    ]
    operacoes_vendidas = sorted(
        (
            op for op in todas_operacoes
            if op.tipo == Operacao.COMPRA and op.saldo <= 0
            and no_periodo(op.data_venda, "data_de_venda", "data_ate_venda")
        ),
        key=lambda op: op.data_venda or op.data_operacao,
        reverse=True,
    )

    return {
        "operacoes_compradas": operacoes_compradas,
        "operacoes_vendidas": operacoes_vendidas,
        "operacoes_reservadas": operacoes_reservadas,
        **valores,
        "ativo_selecionado": ativo_selecionado,
        "tickers_disponiveis": tickers_disponiveis,
        "existe_alguma_operacao": existe_alguma_operacao,
    }


def _periodo_label(data_de_str: str, data_ate_str: str, ativo_selecionado: str = "", campo: str = "Período") -> str:
    """Descrição legível de um período/ativo filtrados, usada no relatório."""
    data_de = parse_date(data_de_str) if data_de_str else None
    data_ate = parse_date(data_ate_str) if data_ate_str else None
    if data_de and data_ate:
        label = f"{campo}: {data_de.strftime('%d/%m/%Y')} a {data_ate.strftime('%d/%m/%Y')}"
    elif data_de:
        label = f"{campo}: a partir de {data_de.strftime('%d/%m/%Y')}"
    elif data_ate:
        label = f"{campo}: até {data_ate.strftime('%d/%m/%Y')}"
    else:
        label = f"{campo}: todas as operações"
    if ativo_selecionado:
        label += f" - Ativo: {ativo_selecionado}"
    return label


@login_required
def operacao_lista(request):
    """
    Lista as operações do usuário divididas em três grids: compras ainda em
    carteira (saldo > 0), compras totalmente vendidas (saldo zerado) e
    reservas (intenção de compra futura) - reflete os três estados em que uma
    operação pode estar, em vez de misturar tudo numa tabela só.
    """
    dados = _periodo_e_operacoes(request)
    operacoes_compradas = dados["operacoes_compradas"]
    operacoes_vendidas = dados["operacoes_vendidas"]
    operacoes_reservadas = dados["operacoes_reservadas"]

    lotes_com_venda = [op for op in (operacoes_compradas + operacoes_vendidas) if op.quantidade_vendida > 0]
    resumo_vendas = None
    if lotes_com_venda:
        total_comprado = sum(
            (op.preco_unitario * op.quantidade_vendida for op in lotes_com_venda), Decimal("0")
        )
        total_vendido = sum(
            (op.preco_venda * op.quantidade_vendida for op in lotes_com_venda), Decimal("0")
        )
        lucro_total = total_vendido - total_comprado
        resumo_vendas = {
            "total_comprado": total_comprado.quantize(Decimal("0.01")),
            "total_vendido": total_vendido.quantize(Decimal("0.01")),
            "lucro_total": lucro_total.quantize(Decimal("0.01")),
            "lucro_pct_total": (
                (lucro_total / total_comprado * 100).quantize(Decimal("0.01")) if total_comprado else None
            ),
        }

    return render(
        request, "core/operacao_lista.html",
        {
            "operacoes": operacoes_compradas + operacoes_vendidas + operacoes_reservadas,
            "operacoes_compradas": operacoes_compradas,
            "operacoes_vendidas": operacoes_vendidas,
            "operacoes_reservadas": operacoes_reservadas,
            "resumo_vendas": resumo_vendas,
            "data_de_carteira": dados["data_de_carteira"],
            "data_ate_carteira": dados["data_ate_carteira"],
            "data_de_reservada": dados["data_de_reservada"],
            "data_ate_reservada": dados["data_ate_reservada"],
            "data_de_venda": dados["data_de_venda"],
            "data_ate_venda": dados["data_ate_venda"],
            "ativo_selecionado": dados["ativo_selecionado"],
            "tickers_disponiveis": dados["tickers_disponiveis"],
            "existe_alguma_operacao": dados["existe_alguma_operacao"],
        },
    )


@login_required
def operacoes_exportar_excel(request):
    """Exporta as operações do usuário logado (mesmos filtros da página) como planilha .xlsx."""
    dados = _periodo_e_operacoes(request)
    conteudo = gerar_excel_operacoes(
        dados["operacoes_compradas"],
        dados["operacoes_vendidas"],
        dados["operacoes_reservadas"],
        _periodo_label(dados["data_de_carteira"], dados["data_ate_carteira"], dados["ativo_selecionado"]),
        _periodo_label(dados["data_de_venda"], dados["data_ate_venda"], dados["ativo_selecionado"], campo="Período (data da venda)"),
        _periodo_label(dados["data_de_reservada"], dados["data_ate_reservada"], dados["ativo_selecionado"]),
    )
    resposta = HttpResponse(
        conteudo,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    nome_arquivo = f"operacoes_{timezone.localdate().isoformat()}.xlsx"
    resposta["Content-Disposition"] = f'attachment; filename="{nome_arquivo}"'
    return resposta


@login_required
def operacoes_exportar_pdf(request):
    """Exporta as operações do usuário logado (mesmos filtros da página) como PDF."""
    dados = _periodo_e_operacoes(request)
    nome_usuario = request.user.first_name or request.user.username
    conteudo = gerar_pdf_operacoes(
        dados["operacoes_compradas"],
        dados["operacoes_vendidas"],
        dados["operacoes_reservadas"],
        _periodo_label(dados["data_de_carteira"], dados["data_ate_carteira"], dados["ativo_selecionado"]),
        _periodo_label(dados["data_de_venda"], dados["data_ate_venda"], dados["ativo_selecionado"], campo="Período (data da venda)"),
        _periodo_label(dados["data_de_reservada"], dados["data_ate_reservada"], dados["ativo_selecionado"]),
        nome_usuario,
    )
    resposta = HttpResponse(conteudo, content_type="application/pdf")
    nome_arquivo = f"operacoes_{timezone.localdate().isoformat()}.pdf"
    resposta["Content-Disposition"] = f'attachment; filename="{nome_arquivo}"'
    return resposta


@login_required
def operacao_nova(request):
    if request.method == "POST":
        form = OperacaoForm(request.POST, usuario=request.user)
        if form.is_valid():
            operacao = form.save(commit=False)
            operacao.usuario = request.user
            operacao.save()
            sincronizar_lancamento_compra(operacao)

            # tenta atualizar a cotação do ativo imediatamente para já refletir no dashboard
            try:
                atualizar_cotacao_diaria(operacao.ativo)
            except BrapiError:
                pass  # a cotação será buscada depois pelo comando de atualização diária

            # ativo com menos de 2 cotações = acabou de ser cadastrado (só a
            # de hoje, se a chamada acima teve sucesso) - preenche de uma vez
            # o histórico recente, pra não deixar RSI/MACD "aguardando
            # histórico" por semanas até acumular os pregões dia a dia (ver
            # core.services.backfill_historico_cotacoes)
            if operacao.ativo.cotacoes.count() < 2:
                try:
                    backfill_historico_cotacoes(operacao.ativo)
                except BrapiError:
                    pass  # sem histórico disponível agora - o robô fica "aguardando histórico" normalmente

            tipo_label = operacao.get_tipo_display()
            messages.success(
                request,
                f"{tipo_label} de {operacao.quantidade}x {operacao.ativo.ticker} registrada com sucesso.",
            )
            return redirect("core:operacao_lista")
    else:
        form = OperacaoForm(usuario=request.user, initial={"data_operacao": timezone.localdate()})

    return render(request, "core/operacao_form.html", {"form": form})


@login_required
def operacao_vender(request, operacao_id):
    """
    Registra a venda (total ou parcial) de um lote de compra existente, ou
    corrige os dados de uma venda já registrada (o mesmo formulário serve
    para os dois casos - só muda o texto exibido).
    """
    operacao = get_object_or_404(
        Operacao, id=operacao_id, usuario=request.user, tipo=Operacao.COMPRA
    )
    ja_estava_vendido = operacao.quantidade_vendida > 0

    if request.method == "POST":
        form = VendaLoteForm(request.POST, instance=operacao)
        if form.is_valid():
            operacao = form.save()
            sincronizar_lancamento_venda(operacao)

            acao = "atualizada" if ja_estava_vendido else "registrada"
            mensagem = (
                f"Venda de {operacao.quantidade_vendida}x {operacao.ativo.ticker} {acao} com sucesso."
            )
            if operacao.lucro_perda_realizado is not None:
                resultado = "lucro" if operacao.lucro_perda_realizado >= 0 else "perda"
                mensagem += f" {resultado.capitalize()} realizado: R$ {operacao.lucro_perda_realizado}."
            messages.success(request, mensagem)
            return redirect("core:operacao_lista")
    else:
        form = VendaLoteForm(instance=operacao)

    return render(request, "core/operacao_vender.html", {"form": form, "operacao": operacao})


@login_required
def operacao_comprar(request, operacao_id):
    """Efetiva uma reserva (intenção de compra) como uma compra real."""
    operacao = get_object_or_404(
        Operacao, id=operacao_id, usuario=request.user, tipo=Operacao.RESERVAR
    )

    if request.method == "POST":
        form = ConfirmarCompraForm(request.POST, instance=operacao)
        if form.is_valid():
            operacao = form.save()
            sincronizar_lancamento_compra(operacao)
            messages.success(
                request,
                f"Reserva efetivada: compra de {operacao.quantidade}x {operacao.ativo.ticker} registrada com sucesso.",
            )
            return redirect("core:operacao_lista")
    else:
        form = ConfirmarCompraForm(instance=operacao)

    return render(request, "core/operacao_comprar.html", {"form": form, "operacao": operacao})


@login_required
def operacao_editar(request, operacao_id):
    """Corrige os dados de uma compra já registrada (quantidade, preço, data e metas)."""
    operacao = get_object_or_404(
        Operacao, id=operacao_id, usuario=request.user, tipo=Operacao.COMPRA
    )

    if request.method == "POST":
        form = EditarCompraForm(request.POST, instance=operacao)
        if form.is_valid():
            operacao = form.save()
            sincronizar_lancamento_compra(operacao)
            sincronizar_lancamento_venda(operacao)
            messages.success(request, f"Compra de {operacao.ativo.ticker} atualizada com sucesso.")
            return redirect("core:operacao_lista")
    else:
        form = EditarCompraForm(instance=operacao)

    return render(request, "core/operacao_editar.html", {"form": form, "operacao": operacao})


@login_required
def operacao_editar_reserva(request, operacao_id):
    """Corrige os dados de uma reserva já registrada (ticker, nome, preço pretendido, data e metas)."""
    operacao = get_object_or_404(
        Operacao, id=operacao_id, usuario=request.user, tipo=Operacao.RESERVAR
    )

    if request.method == "POST":
        form = EditarReservaForm(request.POST, instance=operacao)
        if form.is_valid():
            operacao = form.save()
            messages.success(request, f"Reserva de {operacao.ativo.ticker} atualizada com sucesso.")
            return redirect("core:operacao_lista")
    else:
        form = EditarReservaForm(instance=operacao)

    return render(request, "core/operacao_editar_reserva.html", {"form": form, "operacao": operacao})


@login_required
@require_POST
def operacao_excluir(request, operacao_id):
    """
    Exclui definitivamente uma operação (compra ou reserva) do usuário
    logado. Volta para "Minhas Operações" por padrão, ou para a página de
    origem se o formulário mandar um campo oculto "next" (ex: excluir uma
    reserva direto da tela Posições em Carteira).
    """
    operacao = get_object_or_404(Operacao, id=operacao_id, usuario=request.user)
    ticker = operacao.ativo.ticker
    tipo_label = operacao.get_tipo_display()
    operacao.delete()
    messages.success(request, f"{tipo_label} de {ticker} excluída com sucesso.")
    return redirect(request.POST.get("next") or "core:operacao_lista")


@login_required
def posicoes(request):
    """
    Lista de posições em carteira com percentual de lucro/perda e tendência,
    dividida em três grids: Compradas (saldo real em carteira), Reservadas
    (intenção de compra cadastrada manualmente) e Reservadas pelo Robô
    (sugestão automática do robô consultor - ver
    core.services.gerar_sinais_robo_para_usuario).
    """
    lista_posicoes = calcular_posicoes(request.user)

    valor_investido_total = sum((p.valor_investido for p in lista_posicoes), Decimal("0"))
    valor_atual_total = sum((p.valor_atual for p in lista_posicoes if p.valor_atual is not None), Decimal("0"))
    lucro_perda_total = valor_atual_total - valor_investido_total
    lucro_perda_pct_total = (
        (lucro_perda_total / valor_investido_total) * 100 if valor_investido_total else None
    )

    posicoes_compradas = [p for p in lista_posicoes if not p.apenas_reservado]
    posicoes_reservadas = [p for p in lista_posicoes if p.apenas_reservado and not p.reserva_do_robo]
    posicoes_reservadas_robo = [p for p in lista_posicoes if p.apenas_reservado and p.reserva_do_robo]

    contexto = {
        "posicoes": lista_posicoes,
        "posicoes_compradas": posicoes_compradas,
        "posicoes_reservadas": posicoes_reservadas,
        "posicoes_reservadas_robo": posicoes_reservadas_robo,
        "comparativo": construir_comparativo_valores(lista_posicoes),
        "total_ativos": len(posicoes_compradas),
        "valor_investido_total": valor_investido_total,
        "valor_atual_total": valor_atual_total,
        "lucro_perda_total": lucro_perda_total,
        "lucro_perda_pct_total": lucro_perda_pct_total,
        "concentracao_setor": calcular_concentracao_setor(lista_posicoes),
        "metricas_risco": calcular_metricas_risco(lista_posicoes),
        "comparativo_benchmark": calcular_comparativo_benchmark(request.user, posicoes=lista_posicoes),
        "alertas_recentes": request.user.alertas.all()[:8],
    }
    contexto.update(_contexto_historico_atualizacoes(request.user, limite=20))
    return render(request, "core/posicoes.html", contexto)


@login_required
def posicoes_exportar_excel(request):
    """
    Exporta as posições em carteira do usuário logado como planilha .xlsx -
    inclui, além da grid principal, as análises "Sua carteira x mercado",
    "Concentração por setor" e "Indicadores de risco" já mostradas na tela
    Posições em Carteira (ver core.views.posicoes).
    """
    lista_posicoes = calcular_posicoes(request.user)
    conteudo = gerar_excel_posicoes(
        lista_posicoes,
        comparativo_benchmark=calcular_comparativo_benchmark(request.user, posicoes=lista_posicoes),
        concentracao_setor=calcular_concentracao_setor(lista_posicoes),
        metricas_risco=calcular_metricas_risco(lista_posicoes),
    )
    resposta = HttpResponse(
        conteudo,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    nome_arquivo = f"posicoes_{timezone.localdate().isoformat()}.xlsx"
    resposta["Content-Disposition"] = f'attachment; filename="{nome_arquivo}"'
    return resposta


@login_required
def posicoes_exportar_pdf(request):
    """
    Exporta as posições em carteira do usuário logado como PDF - inclui,
    além da tabela principal, as análises "Sua carteira x mercado",
    "Concentração por setor" e "Indicadores de risco" já mostradas na tela
    Posições em Carteira (ver core.views.posicoes).
    """
    nome_usuario = request.user.first_name or request.user.username
    lista_posicoes = calcular_posicoes(request.user)
    conteudo = gerar_pdf_posicoes(
        lista_posicoes,
        nome_usuario,
        comparativo_benchmark=calcular_comparativo_benchmark(request.user, posicoes=lista_posicoes),
        concentracao_setor=calcular_concentracao_setor(lista_posicoes),
        metricas_risco=calcular_metricas_risco(lista_posicoes),
    )
    resposta = HttpResponse(conteudo, content_type="application/pdf")
    nome_arquivo = f"posicoes_{timezone.localdate().isoformat()}.pdf"
    resposta["Content-Disposition"] = f'attachment; filename="{nome_arquivo}"'
    return resposta


@login_required
def scanner_tecnico(request):
    """
    Scanner Técnico: combina IFR, médias móveis, MACD, volume, volatilidade e
    tendência de curto prazo de cada ação realmente comprada (saldo > 0) num
    só veredito por ativo - deixando explícito quando os indicadores estão
    conflitantes entre si, em vez de fingir uma previsão certa de alta ou
    baixa (ver core.services.escanear_carteira).
    """
    resultados = escanear_carteira(request.user)
    return render(request, "core/scanner_tecnico.html", {"resultados": resultados})


@login_required
@require_POST
def post_it_salvar(request):
    """
    Endpoint leve (JSON) chamado pelo JS do post-it (ver templates/core/
    _post_it.html) para salvar sozinho, enquanto o usuário digita ou
    minimiza/restaura a notinha - sem recarregar a página. Cada campo
    ("texto", "minimizado") só é gravado quando enviado nesta chamada.
    """
    texto = request.POST.get("texto")
    minimizado_bruto = request.POST.get("minimizado")
    minimizado = minimizado_bruto == "1" if minimizado_bruto is not None else None
    post_it = salvar_post_it(request.user, texto=texto, minimizado=minimizado)
    return JsonResponse({
        "ok": True,
        "atualizado_em": timezone.localtime(post_it.atualizado_em).strftime("%H:%M:%S"),
    })


@login_required
def alertas(request):
    """
    Lista de avisos/lembretes do usuário; também dispara a verificação de
    metas e o robô consultor de sinais técnicos de compra/venda. Aproveita a
    visita à página para limpar os avisos antigos de TODOS os usuários (ver
    core.services.limpar_alertas_antigos), mantendo só os de hoje.
    """
    gerar_alertas_para_usuario(request.user)
    gerar_sinais_robo_para_usuario(request.user)
    limpar_alertas_antigos()
    lista_alertas = request.user.alertas.all()
    return render(request, "core/alertas.html", {"alertas": lista_alertas})


@login_required
def alerta_marcar_lido(request, alerta_id):
    alerta = get_object_or_404(Alerta, id=alerta_id, usuario=request.user)
    alerta.lido = True
    alerta.save(update_fields=["lido"])
    return redirect("core:alertas")


def _sinais_robo(usuario):
    """
    Indicadores técnicos (tendência, RSI, MACD, sinal geral) do robô
    consultor pra cada ativo que o usuário tem efetivamente comprado (saldo
    > 0 em carteira) - usado pela página Análise de Mercado e pelo
    relatório de indicação do robô (Excel/PDF). Ativos só reservados (sem
    compra de verdade) não entram mais aqui.
    """
    ativos = Ativo.objects.filter(id__in=ativos_distintos_comprados(usuario))
    return [
        {
            "ativo": ativo,
            "em_carteira": True,
            **analisar_indicadores_tecnicos(ativo),
        }
        for ativo in ativos
    ]


@login_required
def analise_mercado(request):
    """
    Página de análise: maiores altas e baixas do dia no mercado geral, e os
    indicadores técnicos (tendência por média móvel, RSI, MACD) de cada ativo
    acompanhado, com um sinal geral de compra/venda - uma referência simples
    de apoio à decisão, não uma recomendação de investimento. O histórico
    detalhado e o gráfico de cada ativo ficam na página "Histórico e Gráficos".
    """
    sinais = _sinais_robo(request.user)

    maiores_altas, maiores_baixas = [], []
    erro_variacoes = None
    try:
        maiores_altas, maiores_baixas = buscar_maiores_variacoes(limite=10)
    except BrapiError:
        erro_variacoes = "Não foi possível carregar as maiores altas e baixas do dia agora. Tente novamente em instantes."

    contexto = {
        "sinais": sinais,
        "maiores_altas": maiores_altas,
        "maiores_baixas": maiores_baixas,
        "erro_variacoes": erro_variacoes,
    }
    return render(request, "core/analise_mercado.html", contexto)


@login_required
def robo_exportar_excel(request):
    """Exporta a indicação atual do robô consultor para os ativos do usuário logado como planilha .xlsx."""
    conteudo = gerar_excel_robo(_sinais_robo(request.user))
    resposta = HttpResponse(
        conteudo,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    nome_arquivo = f"indicacao_robo_{timezone.localdate().isoformat()}.xlsx"
    resposta["Content-Disposition"] = f'attachment; filename="{nome_arquivo}"'
    return resposta


@login_required
def robo_exportar_pdf(request):
    """Exporta a indicação atual do robô consultor para os ativos do usuário logado como PDF."""
    nome_usuario = request.user.first_name or request.user.username
    conteudo = gerar_pdf_robo(_sinais_robo(request.user), nome_usuario)
    resposta = HttpResponse(conteudo, content_type="application/pdf")
    nome_arquivo = f"indicacao_robo_{timezone.localdate().isoformat()}.pdf"
    resposta["Content-Disposition"] = f'attachment; filename="{nome_arquivo}"'
    return resposta


@login_required
def backtest_robo(request):
    """
    Backtesting do robô consultor: para cada ativo que o usuário acompanha,
    simula os sinais que o robô teria dado no passado (com base só no
    histórico de cotações já salvo) e mostra a taxa de acerto - responde à
    pergunta "esse robô presta?" antes de confiar capital de verdade nele
    (ver core.services.backtest_sinais_robo). Só uma estatística sobre o
    passado, não garante desempenho futuro.
    """
    dias_retorno = 5
    try:
        dias_retorno = int(request.GET.get("dias_retorno", dias_retorno))
    except (TypeError, ValueError):
        pass
    dias_retorno = max(1, min(dias_retorno, 60))

    ativos = Ativo.objects.filter(operacoes__usuario=request.user).distinct().order_by("ticker")
    resultados = [backtest_sinais_robo(ativo, dias_retorno=dias_retorno) for ativo in ativos]

    return render(
        request, "core/backtest_robo.html",
        {"resultados": resultados, "dias_retorno": dias_retorno},
    )


@login_required
def historico_graficos(request):
    """
    Página dedicada ao histórico de cotações diárias e ao gráfico de
    acompanhamento de cada ativo que o usuário já comprou ou vendeu, e
    também das reservas em aberto (intenção de compra futura) - o gráfico de
    uma reserva mostra o preço desde a data em que ela foi feita, não os
    últimos 30 pregões, pra deixar claro como o preço andou desde então.

    Aceita um filtro opcional (?ativo=TICKER) para mostrar só o(s) gráfico(s)
    do ativo escolhido, em vez da lista com todos - útil quando o usuário
    acompanha muitos ativos diferentes.
    """
    ativo_selecionado = request.GET.get("ativo", "").strip().upper()

    todos_ativos = Ativo.objects.filter(operacoes__usuario=request.user).distinct()
    tickers_disponiveis = list(todos_ativos.order_by("ticker").values_list("ticker", flat=True))

    ativos_comprados = Ativo.objects.filter(
        operacoes__usuario=request.user, operacoes__tipo=Operacao.COMPRA
    ).distinct()
    if ativo_selecionado:
        ativos_comprados = ativos_comprados.filter(ticker=ativo_selecionado)

    itens = []
    for ativo in ativos_comprados:
        historico = list(ativo.cotacoes.order_by("-data")[:30])
        itens.append({
            "ativo": ativo,
            "historico": historico,
            "grafico": construir_grafico_cotacoes(historico),
        })

    reservas = (
        Operacao.objects.filter(usuario=request.user, tipo=Operacao.RESERVAR)
        .select_related("ativo").order_by("-data_operacao")
    )
    if ativo_selecionado:
        reservas = reservas.filter(ativo__ticker=ativo_selecionado)

    itens_reservadas = []
    for reserva in reservas:
        historico = list(reserva.ativo.cotacoes.filter(data__gte=reserva.data_operacao).order_by("-data"))
        itens_reservadas.append({
            "ativo": reserva.ativo,
            "operacao": reserva,
            "historico": historico,
            "grafico": construir_grafico_cotacoes(historico, id_svg_sufixo=f"-reserva-{reserva.id}"),
        })

    return render(
        request, "core/historico_graficos.html",
        {
            "itens": itens,
            "itens_reservadas": itens_reservadas,
            "tickers_disponiveis": tickers_disponiveis,
            "ativo_selecionado": ativo_selecionado,
        },
    )


@login_required
def atualizar_cotacoes_agora(request):
    """
    Permite ao usuário forçar a atualização das cotações de sua carteira pelo
    próprio painel - só durante o horário de negociação da B3 (fora do
    pregão o preço não muda, então não há nada de novo pra buscar).
    """
    if not mercado_b3_aberto():
        messages.warning(
            request,
            f"A B3 está fechada agora (horário de negociação: {settings.B3_HORARIO_ABERTURA} às "
            f"{settings.B3_HORARIO_FECHAMENTO}, dias úteis). As cotações só mudam durante o pregão.",
        )
        return redirect("core:dashboard")

    ativos = Ativo.objects.filter(operacoes__usuario=request.user).distinct()
    atualizados, falhas = atualizar_cotacoes_ativos(ativos)

    if atualizados:
        messages.success(request, f"{atualizados} cotação(ões) atualizada(s) com sucesso.")
    if falhas:
        messages.warning(request, f"Não foi possível atualizar {falhas} ativo(s) agora. Tente novamente em instantes.")

    gerar_alertas_para_usuario(request.user)
    gerar_sinais_robo_para_usuario(request.user)
    registrar_atualizacao_carteira(request.user)
    return redirect("core:dashboard")


@login_required
def verificar_atualizacao_cotacoes(request):
    """
    Endpoint leve (JSON) consultado por polling pela tela Posições em
    Carteira - permite ao navegador perceber quando as cotações foram
    atualizadas em segundo plano (pelo agendador embutido no servidor, ver
    core.services.iniciar_agendador_cotacoes_embutido, ou pelo comando
    "atualizar_cotacoes --loop") e recarregar a tela sozinha, sem o usuário
    precisar apertar F5 manualmente pra ver o resultado.
    """
    ultima = Ativo.objects.filter(operacoes__usuario=request.user).aggregate(Max("atualizado_em"))
    ultima_atualizacao = ultima["atualizado_em__max"]
    return JsonResponse({"ultima_atualizacao": ultima_atualizacao.isoformat() if ultima_atualizacao else None})


@login_required
def atualizar_benchmarks_agora(request):
    """
    Permite forçar pelo próprio painel a atualização do histórico de
    benchmarks (Ibovespa e CDI) usado no comparativo "Sua carteira x
    mercado" - equivalente a rodar `python manage.py atualizar_benchmarks`
    na mão. Os benchmarks são globais (não dependem do usuário), então
    qualquer usuário logado pode disparar a atualização.
    """
    resultado = atualizar_benchmarks()
    if resultado["erros"]:
        messages.warning(
            request,
            f"Benchmarks atualizados parcialmente (Ibovespa: {resultado['ibovespa']} nova(s), "
            f"CDI: {resultado['cdi']} nova(s)). Erros: {'; '.join(resultado['erros'])}",
        )
    else:
        messages.success(
            request,
            f"Benchmarks atualizados: Ibovespa ({resultado['ibovespa']} cotação(ões) nova(s)) e "
            f"CDI ({resultado['cdi']} cotação(ões) nova(s)).",
        )
    return redirect(request.GET.get("next") or "core:dashboard")


def _contexto_historico_atualizacoes(usuario, limite=500):
    """
    Monta o gráfico "Variações de hoje" e a lista de registros (com variação
    entre atualizações) do Histórico de Atualizações de um usuário - usado
    tanto pela tela dedicada (core.views.historico_atualizacoes) quanto pela
    aba "Histórico de Atualizações" embutida em Posições em Carteira
    (core.views.posicoes), essa última com um `limite` menor pra não pesar
    a tela principal.
    """
    registros = list(
        RegistroAtualizacaoCarteira.objects.filter(usuario=usuario).order_by("-criado_em")[:limite]
    )
    hoje = timezone.localdate()
    registros_hoje = [r for r in registros if timezone.localtime(r.criado_em).date() == hoje]

    return {
        "registros": registros,
        "registros_com_variacao": calcular_variacoes_historico(registros),
        "grafico_dia": construir_grafico_atualizacoes_dia(list(reversed(registros_hoje))),
        "total_registros_hoje": len(registros_hoje),
    }


@login_required
def historico_atualizacoes(request):
    """
    Tela "Histórico de Atualizações": lista os retratos (snapshots) da
    carteira gravados a cada atualização de cotações (ver
    core.services.registrar_atualizacao_carteira), com um gráfico da
    variação (%) de hoje e a opção de excluir registros antigos por dias.
    Também traz o mural "Atividade da comunidade" (saiu do Painel de
    Controle pra cá) e recarrega sozinha quando uma atualização de cotações
    acontece em segundo plano (mesmo mecanismo de Posições em Carteira - ver
    core.views.verificar_atualizacao_cotacoes).
    """
    contexto = _contexto_historico_atualizacoes(request.user)
    contexto["atividade_recente"] = atividade_recente(limite=15)
    return render(request, "core/historico_atualizacoes.html", contexto)


@login_required
@require_POST
def historico_atualizacoes_excluir(request):
    """Exclui os registros de atualização da carteira do usuário com mais de N dias (formulário "Excluir por dias")."""
    try:
        dias = int(request.POST.get("dias", ""))
    except ValueError:
        dias = None

    if not dias or dias < 1:
        messages.error(request, "Informe um número de dias válido (maior que zero) para excluir.")
    else:
        excluidos = excluir_registros_atualizacao_antigos(request.user, dias)
        if excluidos:
            messages.success(request, f"{excluidos} registro(s) com mais de {dias} dia(s) excluído(s).")
        else:
            messages.info(request, f"Nenhum registro com mais de {dias} dia(s) encontrado para excluir.")
    return redirect("core:historico_atualizacoes")


@login_required
@require_POST
def historico_atualizacoes_excluir_por_periodo(request):
    """Exclui os registros de atualização da carteira do usuário entre duas datas (formulário "Excluir por período")."""
    data_inicio = parse_date(request.POST.get("data_inicio", ""))
    data_fim = parse_date(request.POST.get("data_fim", ""))

    if not data_inicio or not data_fim:
        messages.error(request, "Informe a data de início e a data de fim para excluir por período.")
    elif data_inicio > data_fim:
        messages.error(request, "A data de início não pode ser depois da data de fim.")
    else:
        excluidos = excluir_registros_atualizacao_por_periodo(request.user, data_inicio, data_fim)
        periodo_label = f"{data_inicio.strftime('%d/%m/%Y')} a {data_fim.strftime('%d/%m/%Y')}"
        if excluidos:
            messages.success(request, f"{excluidos} registro(s) entre {periodo_label} excluído(s).")
        else:
            messages.info(request, f"Nenhum registro entre {periodo_label} encontrado para excluir.")
    return redirect("core:historico_atualizacoes")


@login_required
def historico_atualizacoes_exportar_excel(request):
    """Exporta o histórico de atualizações da carteira do usuário logado como planilha .xlsx."""
    registros = RegistroAtualizacaoCarteira.objects.filter(usuario=request.user).order_by("-criado_em")
    conteudo = gerar_excel_historico_atualizacoes(list(registros))
    resposta = HttpResponse(
        conteudo,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    nome_arquivo = f"historico_atualizacoes_{timezone.localdate().isoformat()}.xlsx"
    resposta["Content-Disposition"] = f'attachment; filename="{nome_arquivo}"'
    return resposta


@login_required
def historico_atualizacoes_exportar_pdf(request):
    """Exporta o histórico de atualizações da carteira do usuário logado como PDF."""
    nome_usuario = request.user.first_name or request.user.username
    registros = RegistroAtualizacaoCarteira.objects.filter(usuario=request.user).order_by("-criado_em")
    conteudo = gerar_pdf_historico_atualizacoes(list(registros), nome_usuario)
    resposta = HttpResponse(conteudo, content_type="application/pdf")
    nome_arquivo = f"historico_atualizacoes_{timezone.localdate().isoformat()}.pdf"
    resposta["Content-Disposition"] = f'attachment; filename="{nome_arquivo}"'
    return resposta


_CHAVES_PERIODO_CONTA_CORRENTE = ["data_de_extrato", "data_ate_extrato"]


def _periodo_conta_corrente(request):
    """
    Mesma convenção de período de _periodo_e_operacoes: sem filtro de data na
    URL, cai no mês atual por padrão; "Limpar filtro" manda os campos vazios
    de propósito, pra mostrar o extrato inteiro.
    """
    if any(chave in request.GET for chave in _CHAVES_PERIODO_CONTA_CORRENTE):
        data_de_str = request.GET.get("data_de_extrato", "")
        data_ate_str = request.GET.get("data_ate_extrato", "")
    else:
        hoje = timezone.localdate()
        ultimo_dia_mes = calendar.monthrange(hoje.year, hoje.month)[1]
        data_de_str = hoje.replace(day=1).isoformat()
        data_ate_str = hoje.replace(day=ultimo_dia_mes).isoformat()

    return data_de_str, data_ate_str, parse_date(data_de_str) if data_de_str else None, parse_date(data_ate_str) if data_ate_str else None


@login_required
def conta_corrente(request):
    """
    Tela "Conta Corrente": saldo (inicial + créditos de venda - débitos de
    compra + transferências manuais) e extrato por período, com a
    descrição de cada lançamento automático trazendo o nome do ativo
    comprado/vendido (ver core.services.sincronizar_lancamento_compra/venda).
    """
    conta = obter_ou_criar_conta_corrente(request.user)
    data_de_str, data_ate_str, data_inicio, data_fim = _periodo_conta_corrente(request)

    linhas = extrato_conta_corrente(conta, data_inicio, data_fim)
    saldo_atual = saldo_conta_corrente(conta)

    contexto = {
        "conta": conta,
        "saldo_atual": saldo_atual,
        "linhas": linhas,
        "data_de_extrato": data_de_str,
        "data_ate_extrato": data_ate_str,
        "form_saldo_inicial": SaldoInicialContaCorrenteForm(instance=conta),
        "form_transferencia": TransferenciaContaCorrenteForm(),
    }
    return render(request, "core/conta_corrente.html", contexto)


@login_required
@require_POST
def conta_corrente_definir_saldo(request):
    """Define/corrige o saldo inicial da conta corrente do usuário logado."""
    conta = obter_ou_criar_conta_corrente(request.user)
    form = SaldoInicialContaCorrenteForm(request.POST, instance=conta)
    if form.is_valid():
        form.save()
        messages.success(request, f"Saldo inicial definido em R$ {conta.saldo_inicial}.")
    else:
        for erros in form.errors.values():
            for erro in erros:
                messages.error(request, erro)
    return redirect("core:conta_corrente")


@login_required
@require_POST
def conta_corrente_transferencia_nova(request):
    """Registra uma transferência manual (crédito ou débito) de/para outra conta (ex: Nubank, corretora)."""
    form = TransferenciaContaCorrenteForm(request.POST)
    if form.is_valid():
        registrar_transferencia_conta_corrente(
            request.user,
            tipo=form.cleaned_data["tipo"],
            valor=form.cleaned_data["valor"],
            descricao=form.cleaned_data["descricao"],
            data=form.cleaned_data["data"],
        )
        messages.success(request, "Transferência registrada com sucesso.")
    else:
        for erros in form.errors.values():
            for erro in erros:
                messages.error(request, erro)
    return redirect("core:conta_corrente")


@login_required
@require_POST
def conta_corrente_lancamento_excluir(request, lancamento_id):
    """
    Exclui um lançamento manual (transferência) da conta corrente do usuário
    logado. Lançamentos automáticos de compra/venda não podem ser excluídos
    por aqui - eles somem sozinhos junto com a Operacao (on_delete=CASCADE) -
    ver core.views.operacao_excluir.
    """
    lancamento = get_object_or_404(
        LancamentoContaCorrente,
        id=lancamento_id,
        conta__usuario=request.user,
        origem=LancamentoContaCorrente.ORIGEM_TRANSFERENCIA,
    )
    lancamento.delete()
    messages.success(request, "Transferência excluída com sucesso.")
    return redirect("core:conta_corrente")


@login_required
def conta_corrente_exportar_excel(request):
    """Exporta o extrato da conta corrente do usuário logado (no período filtrado) como planilha .xlsx."""
    conta = obter_ou_criar_conta_corrente(request.user)
    _, _, data_inicio, data_fim = _periodo_conta_corrente(request)
    linhas = extrato_conta_corrente(conta, data_inicio, data_fim)
    conteudo = gerar_excel_extrato_conta_corrente(linhas, conta.saldo_inicial, saldo_conta_corrente(conta))
    resposta = HttpResponse(
        conteudo,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    nome_arquivo = f"extrato_conta_corrente_{timezone.localdate().isoformat()}.xlsx"
    resposta["Content-Disposition"] = f'attachment; filename="{nome_arquivo}"'
    return resposta


@login_required
def conta_corrente_exportar_pdf(request):
    """Exporta o extrato da conta corrente do usuário logado (no período filtrado) como PDF."""
    conta = obter_ou_criar_conta_corrente(request.user)
    nome_usuario = request.user.first_name or request.user.username
    data_de_str, data_ate_str, data_inicio, data_fim = _periodo_conta_corrente(request)
    linhas = extrato_conta_corrente(conta, data_inicio, data_fim)
    periodo_label = _periodo_label(data_de_str, data_ate_str, campo="Extrato")
    conteudo = gerar_pdf_extrato_conta_corrente(
        linhas, conta.saldo_inicial, saldo_conta_corrente(conta), periodo_label, nome_usuario,
    )
    resposta = HttpResponse(conteudo, content_type="application/pdf")
    nome_arquivo = f"extrato_conta_corrente_{timezone.localdate().isoformat()}.pdf"
    resposta["Content-Disposition"] = f'attachment; filename="{nome_arquivo}"'
    return resposta


def offline_view(request):
    """
    Página exibida pelo Service Worker (PWA) quando o usuário está sem conexão
    e tenta abrir uma página que não está no cache. Não exige login, pois
    precisa funcionar mesmo sem sessão/rede disponível.
    """
    return render(request, "offline.html")


def service_worker_view(request):
    """
    Serve o Service Worker do PWA a partir da raiz do site (/sw.js), para que
    seu escopo cubra todas as páginas (um service worker só controla as URLs
    dentro do diretório em que é servido).
    """
    caminho = settings.BASE_DIR / "static" / "js" / "service-worker-source.js"
    conteudo = caminho.read_text(encoding="utf-8")
    resposta = HttpResponse(conteudo, content_type="application/javascript")
    resposta["Service-Worker-Allowed"] = "/"
    resposta["Cache-Control"] = "no-cache"
    return resposta


@login_required
def mensagens_whatsapp(request):
    """Mural com as mensagens recebidas via WhatsApp (webhook) ou lançadas pelo administrador."""
    mensagens = MensagemWhatsapp.objects.all()[:50]
    return render(request, "core/mensagens_whatsapp.html", {"mensagens": mensagens})


@login_required
def noticias(request):
    """
    Feed de manchetes das fontes de notícias/análises de mercado configuradas
    (compartilhado entre todos os usuários, como o mural de atividade). Nesta
    mesma página dá pra cadastrar uma nova fonte - a lista de sites é
    totalmente configurável, não fixa no código.
    """
    if request.method == "POST":
        form = FonteNoticiaForm(request.POST)
        if form.is_valid():
            fonte = form.save()
            messages.success(request, f"Fonte \"{fonte.nome}\" cadastrada com sucesso.")
            return redirect("core:noticias")
    else:
        form = FonteNoticiaForm()

    # ids gravados na sessão pela última atualização (ver noticias_atualizar) -
    # "pop" pra destacar as novidades só na primeira visita após o clique em
    # "Atualizar notícias", não em toda visita seguinte à página.
    noticias_novas_ids = set(request.session.pop("noticias_novas_ids", []))

    fontes = FonteNoticia.objects.all()
    itens = []
    for fonte in fontes:
        if not fonte.ativa:
            continue  # fonte inativa some do feed abaixo (continua na tabela de gerenciamento acima)
        noticias_fonte = list(fonte.noticias.all()[:15])
        itens.append({
            "fonte": fonte,
            "noticias": noticias_fonte,
            "tem_novas": any(n.id in noticias_novas_ids for n in noticias_fonte),
        })

    return render(
        request, "core/noticias.html",
        {"form": form, "fontes": fontes, "itens": itens, "noticias_novas_ids": noticias_novas_ids},
    )


@login_required
def noticias_atualizar(request):
    """Busca as manchetes atuais de todas as fontes ativas e grava as que ainda não existem."""
    fontes_ativas = FonteNoticia.objects.filter(ativa=True)
    novas_ids_total, falhas = [], 0

    for fonte in fontes_ativas:
        try:
            novas_ids_total.extend(atualizar_noticias_fonte(fonte))
        except NoticiaScrapingError:
            falhas += 1

    if novas_ids_total:
        request.session["noticias_novas_ids"] = novas_ids_total
        messages.success(request, f"{len(novas_ids_total)} notícia(s) nova(s) encontrada(s).")
    else:
        messages.info(request, "Nenhuma notícia nova encontrada.")
    if falhas:
        messages.warning(request, f"Não foi possível ler {falhas} fonte(s) agora. Tente novamente em instantes.")

    return redirect("core:noticias")


@login_required
@require_POST
def noticia_fonte_alternar(request, fonte_id):
    """Ativa/desativa uma fonte de notícias (sem excluí-la nem seu histórico)."""
    fonte = get_object_or_404(FonteNoticia, id=fonte_id)
    fonte.ativa = not fonte.ativa
    fonte.save(update_fields=["ativa"])
    return redirect("core:noticias")


@login_required
@require_POST
def noticia_fonte_excluir(request, fonte_id):
    """Exclui uma fonte de notícias e as manchetes já capturadas dela."""
    fonte = get_object_or_404(FonteNoticia, id=fonte_id)
    nome = fonte.nome
    fonte.delete()
    messages.success(request, f"Fonte \"{nome}\" excluída com sucesso.")
    return redirect("core:noticias")


@login_required
def acoes_b3(request):
    """Catálogo com todas as ações da B3 (ver core.models.AcaoB3) - lista de consulta, não é a carteira do usuário."""
    acoes = AcaoB3.objects.all()
    return render(request, "core/acoes_b3.html", {"acoes": acoes})


@login_required
def acoes_b3_atualizar(request):
    """Apaga o catálogo de ações da B3 e cadastra de novo a partir da API brapi.dev."""
    try:
        total = sincronizar_acoes_b3()
        messages.success(request, f"Lista atualizada: {total} ação(ões) da B3 cadastrada(s).")
    except BrapiError as exc:
        messages.warning(request, f"Não foi possível atualizar a lista agora: {exc}")
    return redirect("core:acoes_b3")


@login_required
def tradingview(request):
    """
    Página com o gráfico avançado do TradingView embutido (widget oficial) -
    o site br.tradingview.com não pode ser aberto direto num iframe (ele
    bloqueia isso por política própria), então usamos o widget que a própria
    TradingView disponibiliza para embutir em outros sites.
    """
    tickers = sorted({p.ativo.ticker for p in calcular_posicoes(request.user) if not p.apenas_reservado})
    contexto = {
        "tickers": tickers,
        "tradingview_url_pesquisa": settings.TRADINGVIEW_URL_PESQUISA,
    }
    return render(request, "core/tradingview.html", contexto)


@login_required
def detalhes_cotacoes(request):
    """
    Consulta pontual da cotação de qualquer ação, direto da API - ver
    core.views.consultar_cotacao_avulsa, chamada via AJAX pelo formulário
    desta tela (não depende de ter o ativo em carteira). Também oferece a
    lista dos ativos comprados pelo usuário para escolher em vez de digitar.
    """
    tickers_comprados = list(
        Ativo.objects.filter(id__in=ativos_distintos_comprados(request.user))
        .order_by("ticker").values_list("ticker", flat=True)
    )
    consumo = consumo_api_ultimos_30_dias()
    total_ativos_acompanhados = Ativo.objects.filter(operacoes__isnull=False).distinct().count()
    contexto = {
        "tickers_comprados": tickers_comprados,
        "consumo_api": {
            "usado": consumo,
            "limite": settings.BRAPI_LIMITE_MENSAL,
            "limite_automatico": limite_automatico_api(),
            "limite_absoluto": limite_absoluto_api(),
            "percentual": round(consumo / settings.BRAPI_LIMITE_MENSAL * 100, 1),
            "automatico_pausado": not orcamento_automatico_disponivel(),
            "projecao": projetar_consumo_mensal(total_ativos_acompanhados),
            "total_ativos": total_ativos_acompanhados,
            "intervalo_minutos": settings.COTACOES_INTERVALO_MINUTOS,
        },
    }
    return render(request, "core/detalhes_cotacoes.html", contexto)


@login_required
@require_GET
def consultar_cotacao_avulsa(request):
    """
    Consulta pontual (via AJAX) da cotação atual de qualquer ticker na API
    brapi.dev, junto com o IFR/RSI calculado a partir do histórico recente
    (ver core.services.buscar_cotacao_atual_com_historico e calcular_rsi) -
    não precisa ser um ativo que o usuário já comprou. Usada pelo campo de
    busca na página de Detalhes de Cotações. Não grava nada no banco.
    """
    ticker = (request.GET.get("ticker") or "").strip().upper()
    if not ticker or not TICKER_VALIDO.match(ticker):
        return JsonResponse({"ok": False, "erro": "Informe um ticker válido (ex.: PETR4)."}, status=400)

    try:
        dados, precos_historico = buscar_cotacao_atual_com_historico(ticker)
    except BrapiError as exc:
        return JsonResponse({"ok": False, "erro": str(exc)}, status=502)

    preco = dados.get("regularMarketPrice")
    if preco is None:
        return JsonResponse(
            {"ok": False, "erro": f"API não retornou preço para {ticker}."}, status=502
        )

    hora_formatada = None
    instante = parse_datetime(dados.get("regularMarketTime") or "")
    if instante:
        hora_formatada = timezone.localtime(instante).strftime("%d/%m/%Y %H:%M")

    rsi = calcular_rsi(precos_historico)
    rsi_classe_chave = _classificar_rsi(rsi)

    return JsonResponse({
        "ok": True,
        "ticker": ticker,
        "nome": dados.get("shortName") or dados.get("longName") or ticker,
        "logo_url": dados.get("logourl"),
        "moeda": dados.get("currency") or "BRL",
        "preco": preco,
        "variacao_pct": dados.get("regularMarketChangePercent"),
        "maxima_dia": dados.get("regularMarketDayHigh"),
        "minima_dia": dados.get("regularMarketDayLow"),
        "abertura": dados.get("regularMarketOpen"),
        "fechamento_anterior": dados.get("regularMarketPreviousClose"),
        "volume": dados.get("regularMarketVolume"),
        "valor_mercado": dados.get("marketCap"),
        "minima_52_semanas": dados.get("fiftyTwoWeekLow"),
        "maxima_52_semanas": dados.get("fiftyTwoWeekHigh"),
        "hora_cotacao": hora_formatada,
        "rsi": rsi,
        "rsi_label": RSI_LABELS[rsi_classe_chave],
        "rsi_classe": RSI_CLASSES[rsi_classe_chave],
    })


def _assinatura_whatsapp_valida(request) -> bool:
    """
    Confere a assinatura X-Hub-Signature-256 enviada pela Meta Cloud API,
    calculada com o App Secret do app cadastrado na Meta. Sem WHATSAPP_APP_SECRET
    configurado não há como validar - o webhook aceita a requisição mesmo assim
    (útil para testes locais), mas isso deve ser configurado antes de expor a
    URL do webhook publicamente.
    """
    if not settings.WHATSAPP_APP_SECRET:
        return True

    assinatura_recebida = request.headers.get("X-Hub-Signature-256", "")
    assinatura_esperada = "sha256=" + hmac.new(
        settings.WHATSAPP_APP_SECRET.encode(), request.body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(assinatura_recebida, assinatura_esperada)


@csrf_exempt
def whatsapp_webhook(request):
    """
    Endpoint do webhook da Meta Cloud API (WhatsApp Business Platform).

    GET  -> verificação da URL do webhook (feita uma vez, ao cadastrar a URL no
            painel da Meta): confere hub.verify_token e ecoa hub.challenge.
    POST -> recebe as notificações de mensagens novas e grava em MensagemWhatsapp.

    Não usa @login_required nem exige CSRF: é chamado pelos servidores da Meta,
    não por um usuário logado no navegador. A validação de autenticidade é feita
    pela assinatura X-Hub-Signature-256 (ver _assinatura_whatsapp_valida).
    """
    if request.method == "GET":
        modo = request.GET.get("hub.mode")
        token_recebido = request.GET.get("hub.verify_token")
        desafio = request.GET.get("hub.challenge", "")
        if modo == "subscribe" and settings.WHATSAPP_VERIFY_TOKEN and token_recebido == settings.WHATSAPP_VERIFY_TOKEN:
            return HttpResponse(desafio)
        return HttpResponse(status=403)

    if request.method != "POST":
        return HttpResponseNotAllowed(["GET", "POST"])

    if not _assinatura_whatsapp_valida(request):
        return HttpResponse(status=403)

    try:
        dados = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    for entrada in dados.get("entry", []):
        for mudanca in entrada.get("changes", []):
            valor = mudanca.get("value", {})
            nomes_por_numero = {
                contato.get("wa_id"): contato.get("profile", {}).get("name")
                for contato in valor.get("contacts", [])
            }
            for msg in valor.get("messages", []):
                texto = msg.get("text", {}).get("body")
                if not texto:
                    continue  # ignora mensagens que não são texto (imagem, áudio, etc.)
                numero = msg.get("from", "desconhecido")
                remetente = nomes_por_numero.get(numero) or numero
                MensagemWhatsapp.objects.create(remetente=remetente, texto=texto)

    return HttpResponse(status=200)

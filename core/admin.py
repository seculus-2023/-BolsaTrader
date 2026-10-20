from django.contrib import admin

from .models import (
    Ativo, Operacao, Cotacao, Alerta, MensagemWhatsapp, FonteNoticia, Noticia, CotacaoIndice,
    ContaCorrente, LancamentoContaCorrente, RegistroCotacao, PrevisaoAnalista, VendaLote,
)


@admin.register(Ativo)
class AtivoAdmin(admin.ModelAdmin):
    list_display = ("ticker", "nome", "classe", "setor", "criado_em")
    list_editable = ("classe",)
    list_filter = ("classe",)
    search_fields = ("ticker", "nome", "setor")


@admin.register(CotacaoIndice)
class CotacaoIndiceAdmin(admin.ModelAdmin):
    list_display = ("indice", "data", "valor")
    list_filter = ("indice",)


class VendaLoteInline(admin.TabularInline):
    model = VendaLote
    extra = 0


@admin.register(Operacao)
class OperacaoAdmin(admin.ModelAdmin):
    list_display = ("usuario", "ativo", "tipo", "quantidade", "preco_unitario", "data_operacao")
    inlines = [VendaLoteInline]

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        # vendas alteradas pelo admin também precisam refletir no resumo do lote e na conta corrente
        from .services import recalcular_resumo_vendas
        if form.instance.tipo == Operacao.COMPRA and any(formset.has_changed() for formset in formsets):
            recalcular_resumo_vendas(form.instance)
    list_filter = ("tipo", "data_operacao")
    search_fields = ("ativo__ticker", "usuario__username")


@admin.register(RegistroCotacao)
class RegistroCotacaoAdmin(admin.ModelAdmin):
    list_display = ("usuario", "ativo", "data", "hora", "preco", "origem")
    list_filter = ("origem", "data")
    search_fields = ("ativo__ticker", "usuario__username")


@admin.register(Cotacao)
class CotacaoAdmin(admin.ModelAdmin):
    list_display = ("ativo", "data", "preco_fechamento", "variacao_dia_pct", "volume")
    list_filter = ("data",)
    search_fields = ("ativo__ticker",)


@admin.register(Alerta)
class AlertaAdmin(admin.ModelAdmin):
    list_display = ("usuario", "ativo", "tipo", "mensagem", "lido", "criado_em")
    list_filter = ("tipo", "lido")
    search_fields = ("usuario__username", "ativo__ticker", "mensagem")


@admin.register(MensagemWhatsapp)
class MensagemWhatsappAdmin(admin.ModelAdmin):
    list_display = ("remetente", "texto", "recebido_em")
    search_fields = ("remetente", "texto")
    list_filter = ("recebido_em",)


@admin.register(FonteNoticia)
class FonteNoticiaAdmin(admin.ModelAdmin):
    list_display = ("nome", "url", "ativa", "criado_em")
    list_filter = ("ativa",)
    search_fields = ("nome", "url")


@admin.register(Noticia)
class NoticiaAdmin(admin.ModelAdmin):
    list_display = ("titulo", "fonte", "capturada_em")
    list_filter = ("fonte",)
    search_fields = ("titulo", "url")


class LancamentoContaCorrenteInline(admin.TabularInline):
    model = LancamentoContaCorrente
    extra = 0
    fields = ("data", "tipo", "origem", "valor", "descricao", "operacao")
    readonly_fields = ("operacao",)
    ordering = ("-data", "-criado_em")


@admin.register(ContaCorrente)
class ContaCorrenteAdmin(admin.ModelAdmin):
    list_display = ("usuario", "saldo_inicial", "criado_em", "atualizado_em")
    search_fields = ("usuario__username",)
    inlines = [LancamentoContaCorrenteInline]


@admin.register(LancamentoContaCorrente)
class LancamentoContaCorrenteAdmin(admin.ModelAdmin):
    list_display = ("conta", "data", "tipo", "origem", "valor", "descricao")
    list_filter = ("tipo", "origem", "data")
    search_fields = ("conta__usuario__username", "descricao")
    autocomplete_fields = ("conta", "operacao")


@admin.register(PrevisaoAnalista)
class PrevisaoAnalistaAdmin(admin.ModelAdmin):
    list_display = ("usuario", "ativo", "data_registro", "preco_registro", "alvo_medio", "recomendacao", "origem")
    list_filter = ("origem", "recomendacao")
    search_fields = ("ativo__ticker", "usuario__username")

from django.contrib import admin

from .models import (
    Ativo, Operacao, Cotacao, Alerta, MensagemWhatsapp, FonteNoticia, Noticia, CotacaoIndice,
)


@admin.register(Ativo)
class AtivoAdmin(admin.ModelAdmin):
    list_display = ("ticker", "nome", "setor", "criado_em")
    search_fields = ("ticker", "nome", "setor")


@admin.register(CotacaoIndice)
class CotacaoIndiceAdmin(admin.ModelAdmin):
    list_display = ("indice", "data", "valor")
    list_filter = ("indice",)


@admin.register(Operacao)
class OperacaoAdmin(admin.ModelAdmin):
    list_display = ("usuario", "ativo", "tipo", "quantidade", "preco_unitario", "data_operacao")
    list_filter = ("tipo", "data_operacao")
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

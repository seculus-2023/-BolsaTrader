from django.contrib import admin

from .models import (
    AvisoLimiteIsencao, Darf, EventoCorporativo, ParametroTributario, Provento, SaldoInicialFiscal, TaxaSelicMensal,
)


@admin.register(ParametroTributario)
class ParametroTributarioAdmin(admin.ModelAdmin):
    list_display = (
        "vigencia_inicio", "vigencia_fim", "aliquota_comum_pct", "aliquota_day_trade_pct", "aliquota_fii_pct",
        "limite_isencao_acoes",
    )


@admin.register(SaldoInicialFiscal)
class SaldoInicialFiscalAdmin(admin.ModelAdmin):
    list_display = ("usuario", "prejuizo_comum", "prejuizo_day_trade", "prejuizo_fii", "atualizado_em")
    search_fields = ("usuario__username",)


@admin.register(Darf)
class DarfAdmin(admin.ModelAdmin):
    list_display = ("usuario", "competencia", "valor_principal", "vencimento", "data_pagamento", "valor_pago")
    list_filter = ("data_pagamento",)
    search_fields = ("usuario__username",)


@admin.register(TaxaSelicMensal)
class TaxaSelicMensalAdmin(admin.ModelAdmin):
    list_display = ("mes", "taxa_pct")


@admin.register(AvisoLimiteIsencao)
class AvisoLimiteIsencaoAdmin(admin.ModelAdmin):
    list_display = ("usuario", "competencia", "tipo", "criado_em")


@admin.register(Provento)
class ProventoAdmin(admin.ModelAdmin):
    list_display = ("usuario", "ativo", "tipo", "data_pagamento", "valor_bruto", "ir_retido")
    list_filter = ("tipo",)
    search_fields = ("usuario__username", "ativo__ticker")


@admin.register(EventoCorporativo)
class EventoCorporativoAdmin(admin.ModelAdmin):
    list_display = ("usuario", "ativo", "tipo", "data", "proporcao", "custo_unitario_bonificacao")
    list_filter = ("tipo",)

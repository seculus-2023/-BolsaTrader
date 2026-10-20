from django.contrib import admin

from .models import ParametroTributario, SaldoInicialFiscal


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

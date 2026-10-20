"""Cadastra as regras de renda variável em vigor (as mesmas desde 2005; a MP 1.303/2025 não virou lei)."""
from datetime import date
from decimal import Decimal

from django.db import migrations


def cadastrar(apps, schema_editor):
    ParametroTributario = apps.get_model("tributario", "ParametroTributario")
    if ParametroTributario.objects.exists():
        return
    ParametroTributario.objects.create(
        vigencia_inicio=date(2005, 1, 1),
        aliquota_comum_pct=Decimal("15"),
        aliquota_day_trade_pct=Decimal("20"),
        aliquota_fii_pct=Decimal("20"),
        limite_isencao_acoes=Decimal("20000"),
        units_isentas=False,
        irrf_comum_pct=Decimal("0.005"),
        irrf_day_trade_pct=Decimal("1"),
        darf_minimo=Decimal("10"),
        observacao="Lei 11.033/2004 e IN RFB 1.585/2015 - DARF código 6015.",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("tributario", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(cadastrar, migrations.RunPython.noop),
    ]

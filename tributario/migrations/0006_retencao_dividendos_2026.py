"""
Retenção de 10% sobre dividendos acima de R$ 50 mil por mês da mesma empresa
(Lei 15.270/2025, a partir de 01/01/2026): fecha o parâmetro em vigor em
31/12/2025 e cria um novo a partir de 2026 com as mesmas regras de renda
variável mais o limite de dividendos.
"""
from datetime import date
from decimal import Decimal

from django.db import migrations

INICIO_2026 = date(2026, 1, 1)


def criar_parametro_2026(apps, schema_editor):
    ParametroTributario = apps.get_model("tributario", "ParametroTributario")
    if ParametroTributario.objects.filter(vigencia_inicio__gte=INICIO_2026).exists():
        return
    vigente = ParametroTributario.objects.filter(vigencia_fim__isnull=True).order_by("-vigencia_inicio").first()
    if vigente is None:
        return
    campos = {
        campo.name: getattr(vigente, campo.name)
        for campo in ParametroTributario._meta.fields if campo.name not in ("id", "vigencia_inicio", "vigencia_fim")
    }
    vigente.vigencia_fim = date(2025, 12, 31)
    vigente.save(update_fields=["vigencia_fim"])
    campos.update(
        limite_dividendos_mes=Decimal("50000"),
        observacao="Lei 11.033/2004, IN RFB 1.585/2015 e Lei 15.270/2025 (retenção sobre dividendos) - DARF 6015.",
    )
    ParametroTributario.objects.create(vigencia_inicio=INICIO_2026, **campos)


def desfazer(apps, schema_editor):
    ParametroTributario = apps.get_model("tributario", "ParametroTributario")
    ParametroTributario.objects.filter(vigencia_inicio=INICIO_2026).delete()
    ParametroTributario.objects.filter(vigencia_fim=date(2025, 12, 31)).update(vigencia_fim=None)


class Migration(migrations.Migration):

    dependencies = [
        ("tributario", "0005_parametrotributario_irrf_dividendos_pct_and_more"),
    ]

    operations = [
        migrations.RunPython(criar_parametro_2026, desfazer),
    ]

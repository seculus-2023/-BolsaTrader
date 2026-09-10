from django.db import migrations

FONTES_PADRAO = [
    ("TradingView Brasil", "https://br.tradingview.com/"),
    ("Bora Investir (B3)", "https://borainvestir.b3.com.br/"),
    ("InfoMoney - Mercados", "https://www.infomoney.com.br/mercados/"),
    ("Money Times", "https://www.moneytimes.com.br/"),
]


def criar_fontes_padrao(apps, schema_editor):
    FonteNoticia = apps.get_model("core", "FonteNoticia")
    for nome, url in FONTES_PADRAO:
        FonteNoticia.objects.get_or_create(url=url, defaults={"nome": nome})


def remover_fontes_padrao(apps, schema_editor):
    FonteNoticia = apps.get_model("core", "FonteNoticia")
    FonteNoticia.objects.filter(url__in=[url for _, url in FONTES_PADRAO]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0007_fontenoticia_noticia"),
    ]

    operations = [
        migrations.RunPython(criar_fontes_padrao, remover_fontes_padrao),
    ]

"""
Dados para a base do módulo tributário:

- preenche a classe (ação, FII, ETF, BDR, unit) dos ativos já cadastrados,
  pela mesma inferência usada no Ativo.save();
- cria um VendaLote para cada lote que já tinha venda registrada nos campos
  de resumo (até aqui, um lote só guardava uma venda) e liga a ele o
  lançamento de venda da conta corrente que já existia.
"""
from django.db import migrations

from core.classificacao import inferir_classe_ativo


def preencher(apps, schema_editor):
    Ativo = apps.get_model("core", "Ativo")
    Operacao = apps.get_model("core", "Operacao")
    VendaLote = apps.get_model("core", "VendaLote")
    LancamentoContaCorrente = apps.get_model("core", "LancamentoContaCorrente")

    for ativo in Ativo.objects.filter(classe=""):
        ativo.classe = inferir_classe_ativo(ativo.ticker, ativo.nome)
        ativo.save(update_fields=["classe"])

    lotes_vendidos = Operacao.objects.filter(
        tipo="COMPRA", quantidade_vendida__gt=0, preco_venda__isnull=False, vendas__isnull=True,
    )
    for lote in lotes_vendidos:
        venda = VendaLote.objects.create(
            operacao=lote,
            quantidade=lote.quantidade_vendida,
            preco=lote.preco_venda,
            data=lote.data_venda or lote.data_operacao,
            percentual_corretora=lote.percentual_corretora,
        )
        LancamentoContaCorrente.objects.filter(
            operacao=lote, origem="VENDA", venda_lote__isnull=True
        ).update(venda_lote=venda)


def desfazer(apps, schema_editor):
    # Os campos de resumo do lote continuam preenchidos. Antes do VendaLote
    # só cabia um lançamento de venda por lote: fica o da última venda (com o
    # valor somado de todas), os outros são apagados, e as vendas também.
    LancamentoContaCorrente = apps.get_model("core", "LancamentoContaCorrente")
    VendaLote = apps.get_model("core", "VendaLote")
    lancamentos_por_lote = {}
    for lancamento in LancamentoContaCorrente.objects.filter(venda_lote__isnull=False).order_by("data", "pk"):
        lancamentos_por_lote.setdefault(lancamento.operacao_id, []).append(lancamento)
    for lancamentos in lancamentos_por_lote.values():
        ultimo = lancamentos[-1]
        ultimo.valor = sum(lancamento.valor for lancamento in lancamentos)
        for lancamento in lancamentos[:-1]:
            lancamento.delete()
        ultimo.venda_lote = None
        ultimo.save(update_fields=["valor", "venda_lote"])
    VendaLote.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0027_tributario_base"),
    ]

    operations = [
        migrations.RunPython(preencher, desfazer),
    ]

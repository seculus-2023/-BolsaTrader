from django.urls import path

from . import views

app_name = "tributario"

urlpatterns = [
    path("", views.imposto_renda, name="imposto_renda"),
    path("prejuizos-anteriores/", views.saldo_inicial_salvar, name="saldo_inicial_salvar"),
    path("posicao-fiscal/", views.posicao_fiscal, name="posicao_fiscal"),
    path("simular-venda/", views.simular_venda_view, name="simular_venda"),
    path("proventos/", views.proventos, name="proventos"),
    path("proventos/<int:provento_id>/editar/", views.provento_editar, name="provento_editar"),
    path("proventos/<int:provento_id>/excluir/", views.provento_excluir, name="provento_excluir"),
    path("eventos/", views.eventos_corporativos, name="eventos_corporativos"),
    path("eventos/<int:evento_id>/desfazer/", views.evento_desfazer, name="evento_desfazer"),
    path("declaracao/", views.relatorio_anual, name="relatorio_anual"),
    path("declaracao/excel/", views.relatorio_anual_excel, name="relatorio_anual_excel"),
    path("declaracao/pdf/", views.relatorio_anual_pdf, name="relatorio_anual_pdf"),
    path("darfs/", views.darf_lista, name="darf_lista"),
    path("darfs/<int:darf_id>/pagar/", views.darf_pagar, name="darf_pagar"),
    path("darfs/<int:darf_id>/desfazer-pagamento/", views.darf_desfazer_pagamento, name="darf_desfazer_pagamento"),
]

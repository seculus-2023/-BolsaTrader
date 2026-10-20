from django.urls import path

from . import views

app_name = "tributario"

urlpatterns = [
    path("", views.imposto_renda, name="imposto_renda"),
    path("prejuizos-anteriores/", views.saldo_inicial_salvar, name="saldo_inicial_salvar"),
    path("posicao-fiscal/", views.posicao_fiscal, name="posicao_fiscal"),
    path("simular-venda/", views.simular_venda_view, name="simular_venda"),
    path("darfs/", views.darf_lista, name="darf_lista"),
    path("darfs/<int:darf_id>/pagar/", views.darf_pagar, name="darf_pagar"),
    path("darfs/<int:darf_id>/desfazer-pagamento/", views.darf_desfazer_pagamento, name="darf_desfazer_pagamento"),
]

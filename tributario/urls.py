from django.urls import path

from . import views

app_name = "tributario"

urlpatterns = [
    path("", views.imposto_renda, name="imposto_renda"),
    path("prejuizos-anteriores/", views.saldo_inicial_salvar, name="saldo_inicial_salvar"),
    path("posicao-fiscal/", views.posicao_fiscal, name="posicao_fiscal"),
]

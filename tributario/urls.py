from django.urls import path

from . import views

app_name = "tributario"

urlpatterns = [
    path("posicao-fiscal/", views.posicao_fiscal, name="posicao_fiscal"),
]

from django.urls import path
from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.LoginFuturistaView.as_view(), name="login"),
    path("logout/", views.LogoutFuturistaView.as_view(), name="logout"),
    path("cadastro/", views.cadastro_view, name="cadastro"),
    path("minha-conta/", views.minha_conta_view, name="minha_conta"),
    path("minha-conta/limpar-dados/", views.limpar_dados_view, name="limpar_dados"),
]

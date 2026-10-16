from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.LoginFuturistaView.as_view(), name="login"),
    path("logout/", views.LogoutFuturistaView.as_view(), name="logout"),
    path("cadastro/", views.cadastro_view, name="cadastro"),
    path("minha-conta/", views.minha_conta_view, name="minha_conta"),
    path("minha-conta/logo/", views.logo_conta_view, name="logo_conta"),
    path("minha-conta/painel-tv/",views.painel_tv_link_view, name="painel_tv_link"),
    path("minha-conta/limpar-dados/",views.limpar_dados_view, name="limpar_dados"),
    # Recuperação de senha por e-mail ("Esqueci minha senha" na tela de login)
    # - views prontas do Django, só com os templates e URLs do BolsaTrader.
    path(
        "senha/esqueci/",
        auth_views.PasswordResetView.as_view(
            template_name="accounts/senha_esqueci.html",
            email_template_name="accounts/senha_email.txt",
            subject_template_name="accounts/senha_email_assunto.txt",
            success_url=reverse_lazy("accounts:senha_enviada"),
        ),
        name="senha_esqueci",
    ),
    path(
        "senha/enviada/",
        auth_views.PasswordResetDoneView.as_view(template_name="accounts/senha_enviada.html"),
        name="senha_enviada",
    ),
    path(
        "senha/redefinir/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="accounts/senha_redefinir.html",
            success_url=reverse_lazy("accounts:senha_redefinida"),
        ),
        name="senha_redefinir",
    ),
    path(
        "senha/redefinida/",
        auth_views.PasswordResetCompleteView.as_view(template_name="accounts/senha_redefinida.html"),
        name="senha_redefinida",
    ),
    # Troca de senha de quem já está logado (link em Minha Conta)
    path(
        "senha/alterar/",
        auth_views.PasswordChangeView.as_view(
            template_name="accounts/senha_alterar.html",
            success_url=reverse_lazy("accounts:senha_alterada"),
        ),
        name="senha_alterar",
    ),
    path(
        "senha/alterada/",
        auth_views.PasswordChangeDoneView.as_view(template_name="accounts/senha_alterada.html"),
        name="senha_alterada",
    ),
]

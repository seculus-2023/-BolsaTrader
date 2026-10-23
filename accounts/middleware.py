from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect

from .models import acesso_expirado

MENSAGEM_ACESSO_VENCIDO = "O prazo de acesso da sua conta terminou. Fale com o administrador para renovar."


class ValidadeAcessoMiddleware:
    """
    Encerra a sessão de quem já estava logado quando a data de acesso da conta
    (PerfilUsuario.acesso_ate) vence - o login novo é barrado em
    accounts.views.LoginFuturistaView.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if acesso_expirado(request.user):
            logout(request)
            messages.error(request, MENSAGEM_ACESSO_VENCIDO)
            return redirect("accounts:login")
        return self.get_response(request)

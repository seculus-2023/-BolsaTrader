from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.shortcuts import render, redirect
from django.urls import reverse_lazy
from django.contrib import messages

from .forms import CadastroForm, PerfilUsuarioForm
from .models import PerfilUsuario


class LoginFuturistaView(LoginView):
    template_name = "accounts/login.html"
    redirect_authenticated_user = True

    def form_invalid(self, form):
        messages.error(self.request, "Usuário ou senha inválidos.")
        return super().form_invalid(form)


class LogoutFuturistaView(LogoutView):
    next_page = reverse_lazy("accounts:login")


def cadastro_view(request):
    """
    Cadastro de novo usuário. Acessível sem login (tela de "criar conta") e
    também pelo menu quando já logado (para cadastrar outro usuário) - nesse
    caso a sessão atual não é trocada pela do usuário recém-criado.
    """
    if request.method == "POST":
        form = CadastroForm(request.POST)
        if form.is_valid():
            user = form.save()
            if request.user.is_authenticated:
                messages.success(request, f"Usuário {user.username} cadastrado com sucesso.")
                return redirect("accounts:cadastro")
            login(request, user)
            messages.success(request, f"Bem-vindo(a), {user.first_name}! Sua conta foi criada com sucesso.")
            return redirect("core:dashboard")
    else:
        form = CadastroForm()

    return render(request, "accounts/cadastro.html", {"form": form})


@login_required
def minha_conta_view(request):
    """
    Tela para o usuário informar/atualizar seu número de WhatsApp, usado para
    receber avisos proativos (meta de lucro/perda atingida, sinal do robô
    consultor - ver core.services.enviar_whatsapp) - o perfil é criado sob
    demanda na primeira visita a esta tela (get_or_create), não em todo
    cadastro de usuário.
    """
    perfil, _ = PerfilUsuario.objects.get_or_create(usuario=request.user)

    if request.method == "POST":
        form = PerfilUsuarioForm(request.POST, instance=perfil)
        if form.is_valid():
            form.save()
            messages.success(request, "Dados da conta atualizados com sucesso.")
            return redirect("accounts:minha_conta")
    else:
        form = PerfilUsuarioForm(instance=perfil)

    return render(request, "accounts/minha_conta.html", {"form": form})

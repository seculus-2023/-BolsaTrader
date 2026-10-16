import logging
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.core.cache import cache
from django.shortcuts import render, redirect
from django.urls import reverse, reverse_lazy
from django.contrib import messages
from django.views.decorators.http import require_POST

from core.services import limpar_dados_sistema, limpar_dados_usuario

from .forms import CadastroForm, PerfilUsuarioForm
from .models import PerfilUsuario

logger = logging.getLogger(__name__)


def _chave_tentativas_login(request) -> str:
    usuario = request.POST.get("username", "").strip().lower()
    ip = request.META.get("REMOTE_ADDR", "")
    return f"login_falhas:{usuario}:{ip}"


class LoginFuturistaView(LoginView):
    """
    Login com limite de tentativas: depois de LOGIN_MAX_TENTATIVAS senhas
    erradas seguidas para o mesmo usuário a partir do mesmo IP, novas
    tentativas são recusadas por LOGIN_BLOQUEIO_MINUTOS (contra força bruta).
    """

    template_name = "accounts/login.html"
    redirect_authenticated_user = True

    def post(self, request, *args, **kwargs):
        if cache.get(_chave_tentativas_login(request), 0) >= settings.LOGIN_MAX_TENTATIVAS:
            messages.error(
                request,
                f"Muitas tentativas de login sem sucesso. Aguarde {settings.LOGIN_BLOQUEIO_MINUTOS} "
                "minuto(s) e tente de novo.",
            )
            return redirect("accounts:login")
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        cache.delete(_chave_tentativas_login(self.request))
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        contexto = super().get_context_data(**kwargs)
        contexto["cadastro_publico"] = settings.CADASTRO_PUBLICO
        return contexto

    def form_invalid(self, form):
        chave = _chave_tentativas_login(self.request)
        falhas = cache.get(chave, 0) + 1
        cache.set(chave, falhas, settings.LOGIN_BLOQUEIO_MINUTOS * 60)
        if falhas == settings.LOGIN_MAX_TENTATIVAS:
            logger.warning(
                "Login bloqueado por %s min após %s senhas erradas: usuário %r, IP %s.",
                settings.LOGIN_BLOQUEIO_MINUTOS, falhas,
                self.request.POST.get("username", ""), self.request.META.get("REMOTE_ADDR", ""),
            )
        messages.error(self.request, "Usuário ou senha inválidos.")
        return super().form_invalid(form)


class LogoutFuturistaView(LogoutView):
    next_page = reverse_lazy("accounts:login")


def cadastro_view(request):
    """
    Cadastro de novo usuário. Sem login, só com CADASTRO_PUBLICO=True (tela
    de "criar conta"); logado, só staff/superusuário, pelo menu (para
    cadastrar outro usuário) - nesse caso a sessão atual não é trocada pela
    do usuário recém-criado.
    """
    if request.user.is_authenticated and not request.user.is_staff:
        messages.error(request, "Só administradores podem cadastrar novos usuários.")
        return redirect("core:dashboard")
    if not request.user.is_authenticated and not settings.CADASTRO_PUBLICO:
        messages.error(request, "O cadastro de novos usuários está fechado. Peça acesso ao administrador.")
        return redirect("accounts:login")

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
    Tela para o usuário informar/atualizar seu número de WhatsApp de contato
    e o bot do Telegram por onde recebe os avisos de alerta - o perfil é criado sob demanda na primeira visita a esta tela
    (get_or_create), não em todo cadastro de usuário.
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

    contexto = {"form": form}
    if perfil.painel_tv_token:
        contexto["painel_tv_url"] = request.build_absolute_uri(
            reverse("core:painel_tv", args=[perfil.painel_tv_token])
        )
    if request.user.is_superuser:
        contexto["usuarios_limpeza"] = get_user_model().objects.order_by("username")
    return render(request, "accounts/minha_conta.html", contexto)


@login_required
@require_POST
def painel_tv_link_view(request):
    """
    Gera (ou troca) e desativa o link secreto do painel para TV/monitor
    (core.views.painel_tv). Gerar um novo invalida o anterior na hora -
    útil se o link vazou ou a TV mudou de lugar.
    """
    perfil, _ = PerfilUsuario.objects.get_or_create(usuario=request.user)
    if request.POST.get("acao") == "desativar":
        perfil.painel_tv_token = None
        perfil.save(update_fields=["painel_tv_token", "atualizado_em"])
        messages.success(request, "Link do painel para TV desativado - quem tinha o link perdeu o acesso.")
    else:
        perfil.painel_tv_token = secrets.token_urlsafe(32)
        perfil.save(update_fields=["painel_tv_token", "atualizado_em"])
        messages.success(request, "Novo link do painel para TV gerado. Se havia um link anterior, ele parou de funcionar.")
    return redirect(reverse("accounts:minha_conta") + "#painel-tv")


@login_required
@require_POST
def limpar_dados_view(request):
    """
    Apaga os dados da conta escolhida no combo (core.services.limpar_dados_usuario)
    ou, com a opção "todos", de TODOS os usuários (core.services.limpar_dados_sistema).
    Em qualquer caso ficam os usuários e Minha Conta. Só superusuário pode
    usar, e precisa confirmar digitando a própria senha.
    """
    if not request.user.is_superuser:
        messages.error(request, "Só o administrador do sistema pode limpar os dados.")
        return redirect("accounts:minha_conta")

    senha = request.POST.get("senha", "")
    if not senha or not request.user.check_password(senha):
        messages.error(request, "Senha incorreta - nenhum dado foi apagado.")
        return redirect("accounts:minha_conta")

    escolha = request.POST.get("usuario", "")
    if escolha == "todos":
        total = limpar_dados_sistema()
        messages.success(
            request,
            f"Dados do sistema apagados ({total} registro(s)). Ações da B3, usuários e Minha Conta foram mantidos.",
        )
        return redirect("accounts:minha_conta")

    alvo = get_user_model().objects.filter(pk=escolha).first() if escolha.isdigit() else None
    if alvo is None:
        messages.error(request, "Escolha a conta que terá os dados apagados - nenhum dado foi apagado.")
        return redirect("accounts:minha_conta")

    total = limpar_dados_usuario(alvo)
    messages.success(
        request,
        f"Dados da conta {alvo.username} apagados ({total} registro(s)). O usuário e Minha Conta foram mantidos.",
    )
    return redirect("accounts:minha_conta")

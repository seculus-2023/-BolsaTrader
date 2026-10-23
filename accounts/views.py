import logging
import secrets
from datetime import date, timedelta
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.shortcuts import render, redirect
from django.urls import reverse, reverse_lazy
from django.contrib import messages
from django.views.decorators.http import require_POST

from core.services import MANTIDOS_NA_LIMPEZA_GERAL, limpar_dados_sistema, limpar_dados_usuario

from .forms import CadastroForm, LogoContaForm, PerfilUsuarioForm
from .middleware import MENSAGEM_ACESSO_VENCIDO
from .models import ConviteCadastro, PerfilUsuario, acesso_expirado

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
        if acesso_expirado(form.get_user()):
            messages.error(self.request, MENSAGEM_ACESSO_VENCIDO)
            return redirect("accounts:login")
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

    contexto = {"form": form, "form_logo": LogoContaForm(), "perfil": perfil}
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
def logo_conta_view(request):
    """
    Envia ou remove a logo da conta (PerfilUsuario.logo) - mostrada no lugar
    da marca "⚡ BOLSATRADER" na barra de menu e no painel para TV; sem logo,
    volta a marca padrão.
    """
    perfil, _ = PerfilUsuario.objects.get_or_create(usuario=request.user)
    if request.POST.get("acao") == "remover":
        perfil.logo = None
        perfil.save(update_fields=["logo", "atualizado_em"])
        messages.success(request, "Logo removida - voltou a marca padrão do BolsaTrader.")
    else:
        form = LogoContaForm(request.POST, request.FILES)
        if form.is_valid():
            perfil.logo = form.cleaned_data["logo"]
            perfil.save(update_fields=["logo", "atualizado_em"])
            messages.success(request, "Logo da conta atualizada.")
        else:
            for erro in form.errors.get("logo", ["Escolha uma imagem para enviar."]):
                messages.error(request, erro)
    return redirect(reverse("accounts:minha_conta") + "#logo-conta")


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
            f"Dados do sistema apagados ({total} registro(s)). Foram mantidos: {MANTIDOS_NA_LIMPEZA_GERAL}.",
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


# --------------------------------------------------------------------------
# Convites: link para a pessoa criar a conta com o cadastro público fechado
# --------------------------------------------------------------------------

@login_required
def convites_view(request):
    """Superusuário gera links de convite (validade CONVITE_VALIDADE_DIAS) e acompanha a lista."""
    if not request.user.is_superuser:
        messages.error(request, "Só o administrador do sistema pode gerar convites.")
        return redirect("core:dashboard")

    if request.method == "POST":
        acesso_ate, erro = _ler_data_acesso(request.POST.get("acesso_ate", ""))
        if erro:
            messages.error(request, erro)
            return redirect("accounts:convites")
        convite = ConviteCadastro.objects.create(
            token=secrets.token_urlsafe(24),
            observacao=request.POST.get("observacao", "").strip()[:100],
            criado_por=request.user,
            expira_em=timezone.now() + timedelta(days=settings.CONVITE_VALIDADE_DIAS),
            acesso_ate=acesso_ate,
        )
        messages.success(request, "Convite gerado - copie o link ou envie pelo WhatsApp.")
        return redirect(reverse("accounts:convites") + f"#convite-{convite.pk}")

    convites = list(ConviteCadastro.objects.select_related("usado_por__perfil")[:100])
    for convite in convites:
        convite.link = request.build_absolute_uri(reverse("accounts:convite_cadastro", args=[convite.token]))
        texto = (
            "Você foi convidado(a) para o BolsaTrader! Crie sua conta e instale o aplicativo "
            f"por este link (vale até {timezone.localtime(convite.expira_em):%d/%m/%Y}): {convite.link}"
        )
        convite.link_whatsapp = "https://wa.me/?text=" + quote(texto)
    for convite in convites:
        perfil = getattr(convite.usado_por, "perfil", None) if convite.usado_por else None
        convite.perfil_conta = perfil
    return render(request, "accounts/convites.html", {
        "convites": convites,
        "validade_dias": settings.CONVITE_VALIDADE_DIAS,
        "hoje": timezone.localdate(),
    })


def _ler_data_acesso(texto):
    """Data "aaaa-mm-dd" do campo de validade de acesso -> (data ou None, mensagem de erro ou None)."""
    texto = (texto or "").strip()
    if not texto:
        return None, None
    try:
        data = date.fromisoformat(texto)
    except ValueError:
        return None, "Data de validade do acesso inválida."
    if data < timezone.localdate():
        return None, "A validade do acesso não pode ser uma data que já passou."
    return data, None


@login_required
@require_POST
def convite_acesso_view(request, pk):
    """Superusuário altera (ou tira) a data limite de acesso da conta criada pelo convite."""
    if not request.user.is_superuser:
        messages.error(request, "Só o administrador do sistema pode alterar a validade do acesso.")
        return redirect("core:dashboard")
    convite = ConviteCadastro.objects.select_related("usado_por").filter(pk=pk).first()
    if convite is None or convite.usado_por is None:
        messages.error(request, "Esse convite ainda não tem conta criada.")
        return redirect("accounts:convites")
    acesso_ate, erro = _ler_data_acesso(request.POST.get("acesso_ate", ""))
    if erro:
        messages.error(request, erro)
        return redirect("accounts:convites")
    PerfilUsuario.objects.update_or_create(usuario=convite.usado_por, defaults={"acesso_ate": acesso_ate})
    if acesso_ate:
        messages.success(request, f"Acesso de {convite.usado_por.username} válido até {acesso_ate:%d/%m/%Y}.")
    else:
        messages.success(request, f"Acesso de {convite.usado_por.username} agora é sem limite de data.")
    return redirect(reverse("accounts:convites") + f"#convite-{convite.pk}")


@login_required
@require_POST
def convite_cancelar_view(request, pk):
    if not request.user.is_superuser:
        messages.error(request, "Só o administrador do sistema pode cancelar convites.")
        return redirect("core:dashboard")
    atualizados = ConviteCadastro.objects.filter(pk=pk, usado_em__isnull=True).update(cancelado=True)
    if atualizados:
        messages.success(request, "Convite cancelado - o link parou de funcionar.")
    else:
        messages.error(request, "Esse convite já foi usado e não pode ser cancelado.")
    return redirect("accounts:convites")


def convite_cadastro_view(request, token):
    """
    Cadastro pelo link de convite. Funciona mesmo com CADASTRO_PUBLICO=False;
    o convite vale uma vez só e até expira_em. Depois de criar a conta, a
    pessoa já entra logada na tela de instalação do aplicativo.
    """
    convite = ConviteCadastro.objects.filter(token=token).first()
    if convite is None or not convite.valido:
        motivo = {
            "usado": "Este convite já foi usado.",
            "vencido": "Este convite venceu.",
            "cancelado": "Este convite foi cancelado.",
        }.get(convite.situacao if convite else "", "Convite não encontrado.")
        return render(request, "accounts/convite_invalido.html", {"motivo": motivo}, status=410 if convite else 404)

    if request.user.is_authenticated:
        return render(request, "accounts/convite_invalido.html", {
            "motivo": f"Você já está logado como {request.user.username}. Saia da conta para usar este convite.",
        })

    if request.method == "POST":
        form = CadastroForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                # trava o convite: dois envios ao mesmo tempo não criam duas contas
                convite = ConviteCadastro.objects.select_for_update().get(pk=convite.pk)
                if not convite.valido:
                    return render(request, "accounts/convite_invalido.html",
                                  {"motivo": "Este convite já foi usado."}, status=410)
                user = form.save()
                if convite.acesso_ate:
                    PerfilUsuario.objects.update_or_create(usuario=user, defaults={"acesso_ate": convite.acesso_ate})
                convite.usado_em = timezone.now()
                convite.usado_por = user
                convite.save(update_fields=["usado_em", "usado_por"])
            login(request, user)
            messages.success(request, f"Bem-vindo(a), {user.first_name}! Sua conta foi criada.")
            return redirect("accounts:instalar_app")
    else:
        form = CadastroForm()

    return render(request, "accounts/cadastro.html", {"form": form, "convite": convite})


@login_required
def instalar_app_view(request):
    """Passo a passo para instalar o BolsaTrader (PWA) no computador ou celular."""
    return render(request, "accounts/instalar_app.html")

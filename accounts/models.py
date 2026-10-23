import base64

from django.conf import settings
from django.db import models

from .criptografia import CampoTextoCriptografado


class PerfilUsuario(models.Model):
    """
    Dados complementares do usuário, além do que o User padrão do Django já
    guarda: o número de WhatsApp de contato (o BolsaTrader não envia
    mensagens para ele) e o bot do Telegram do próprio usuário, por onde ele
    recebe os avisos de alerta (core.services.notificar_alertas_telegram), e
    o login (usuário/senha) e a assinatura eletrônica da corretora, só para
    consulta do usuário.

    Criado no cadastro quando o usuário informa o telefone (accounts.forms.
    CadastroForm) ou sob demanda (get_or_create) pela view
    accounts.views.minha_conta - por isso é OneToOne opcional, não uma
    extensão obrigatória do User.
    """

    usuario = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="perfil"
    )
    numero_whatsapp = models.CharField(
        "Número do WhatsApp", max_length=20, blank=True,
        help_text="Formato internacional, só dígitos (ex: 5565999998888). Opcional.",
    )
    # Cada usuário usa o seu próprio bot (criado no @BotFather) - sem os dois
    # campos preenchidos, nada é enviado pelo Telegram para este usuário.
    telegram_bot_token = CampoTextoCriptografado(
        "Token do bot do Telegram", max_length=100, blank=True,
        help_text="Token do seu bot, criado no @BotFather (ex: 123456789:AAF...). Opcional.",
    )
    telegram_chat_id = models.CharField(
        "Chat ID do Telegram", max_length=32, blank=True,
        help_text="ID do chat que recebe os avisos (ex: 2065125150). Opcional.",
    )
    # Token, login/senha da corretora e assinatura eletrônica ficam cifrados no
    # banco (ver accounts.criptografia).
    # Login da corretora e assinatura eletrônica que ela (ex: Clear) pede para
    # confirmar o envio de ordens no Home Broker - só guardados aqui para
    # consulta do próprio usuário; o BolsaTrader não acessa a corretora.
    corretora_usuario = CampoTextoCriptografado(
        "Usuário da corretora", max_length=100, blank=True,
        help_text="Login (CPF, e-mail ou código) usado para entrar no Home Broker. Opcional.",
    )
    corretora_senha = CampoTextoCriptografado(
        "Senha da corretora", max_length=128, blank=True,
        help_text="Senha de acesso ao Home Broker. Opcional.",
    )
    assinatura_eletronica = CampoTextoCriptografado(
        "Assinatura eletrônica da corretora", max_length=32, blank=True,
        help_text="Assinatura que a corretora pede para confirmar ordens no Home Broker. Opcional.",
    )
    # Link secreto do "painel para TV/monitor" (core.views.painel_tv): quem
    # tem o link vê o painel desta conta sem login, só leitura. Vazio = link
    # desativado; gerar um novo invalida o anterior (accounts.views.
    # painel_tv_link_view).
    painel_tv_token = models.CharField(
        "Token do painel para TV", max_length=64, null=True, blank=True, unique=True, editable=False,
    )
    # Logo da conta, mostrada no lugar do "⚡ BOLSATRADER" da barra de menu e
    # do painel para TV. Fica no banco (PNG já reduzido por accounts.forms.
    # LogoContaForm, poucos KB) e não em arquivo, pra não depender de volume
    # de mídia no Docker. Vazio = usa a marca padrão do BolsaTrader.
    logo = models.BinaryField("Logo da conta", null=True, blank=True, editable=False)
    # Data limite de acesso da conta (inclusive), definida pelo superusuário no
    # convite ou depois em "Convites"/admin. Vazio = sem limite. Vencida, o
    # login é recusado e a sessão aberta é encerrada (accounts.middleware).
    acesso_ate = models.DateField(
        "Acesso válido até", null=True, blank=True,
        help_text="Último dia em que a conta pode entrar no sistema. Vazio = sem limite.",
    )
    atualizado_em = models.DateTimeField(auto_now=True)

    @property
    def acesso_vencido(self) -> bool:
        from django.utils import timezone

        return bool(self.acesso_ate) and self.acesso_ate < timezone.localdate()

    @property
    def logo_data_uri(self) -> str:
        """A logo pronta pra um <img src="...">, ou "" se a conta não tem logo."""
        if not self.logo:
            return ""
        return "data:image/png;base64," + base64.b64encode(bytes(self.logo)).decode("ascii")

    class Meta:
        verbose_name = "Perfil do usuário"
        verbose_name_plural = "Perfis dos usuários"

    def __str__(self):
        return f"Perfil de {self.usuario.username}"


class ConviteCadastro(models.Model):
    """
    Link de convite para criar conta sem abrir o cadastro ao público: o
    superusuário gera o link (accounts.views.convites_view), manda para a
    pessoa, e ela cria a conta em accounts.views.convite_cadastro_view.
    Vale CONVITE_VALIDADE_DIAS e só uma vez; não fica preso a um e-mail.
    """

    token = models.CharField(max_length=64, unique=True, editable=False)
    observacao = models.CharField(
        "Para quem é", max_length=100, blank=True,
        help_text="Só para você identificar o convite na lista (ex: João da contabilidade). Opcional.",
    )
    criado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="convites_criados"
    )
    criado_em = models.DateTimeField(auto_now_add=True)
    expira_em = models.DateTimeField()
    usado_em = models.DateTimeField(null=True, blank=True)
    usado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="convite_usado",
    )
    cancelado = models.BooleanField(default=False)
    # Validade de acesso da conta criada por este convite (só superusuário
    # define); copiada para PerfilUsuario.acesso_ate no cadastro.
    acesso_ate = models.DateField("Acesso da conta válido até", null=True, blank=True)

    class Meta:
        ordering = ["-criado_em"]
        verbose_name = "Convite de cadastro"
        verbose_name_plural = "Convites de cadastro"

    def __str__(self):
        return f"Convite {self.observacao or self.token[:8]} ({self.situacao})"

    @property
    def situacao(self) -> str:
        from django.utils import timezone

        if self.usado_em:
            return "usado"
        if self.cancelado:
            return "cancelado"
        if self.expira_em <= timezone.now():
            return "vencido"
        return "pendente"

    @property
    def valido(self) -> bool:
        return self.situacao == "pendente"


def acesso_expirado(usuario) -> bool:
    """True se a conta tem data limite de acesso já vencida (superusuário nunca expira)."""
    if not usuario.is_authenticated or usuario.is_superuser:
        return False
    acesso_ate = PerfilUsuario.objects.filter(usuario=usuario).values_list("acesso_ate", flat=True).first()
    return PerfilUsuario(acesso_ate=acesso_ate).acesso_vencido

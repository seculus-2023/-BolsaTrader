from django.conf import settings
from django.db import models


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
    telegram_bot_token = models.CharField(
        "Token do bot do Telegram", max_length=100, blank=True,
        help_text="Token do seu bot, criado no @BotFather (ex: 123456789:AAF...). Opcional.",
    )
    telegram_chat_id = models.CharField(
        "Chat ID do Telegram", max_length=32, blank=True,
        help_text="ID do chat que recebe os avisos (ex: 2065125150). Opcional.",
    )
    # Login da corretora e assinatura eletrônica que ela (ex: Clear) pede para
    # confirmar o envio de ordens no Home Broker - só guardados aqui para
    # consulta do próprio usuário; o BolsaTrader não acessa a corretora.
    corretora_usuario = models.CharField(
        "Usuário da corretora", max_length=100, blank=True,
        help_text="Login (CPF, e-mail ou código) usado para entrar no Home Broker. Opcional.",
    )
    corretora_senha = models.CharField(
        "Senha da corretora", max_length=128, blank=True,
        help_text="Senha de acesso ao Home Broker. Opcional.",
    )
    assinatura_eletronica = models.CharField(
        "Assinatura eletrônica da corretora", max_length=32, blank=True,
        help_text="Assinatura que a corretora pede para confirmar ordens no Home Broker. Opcional.",
    )
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Perfil do usuário"
        verbose_name_plural = "Perfis dos usuários"

    def __str__(self):
        return f"Perfil de {self.usuario.username}"

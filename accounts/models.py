from django.conf import settings
from django.db import models


class PerfilUsuario(models.Model):
    """
    Dados complementares do usuário, além do que o User padrão do Django já
    guarda - hoje só o número de WhatsApp para onde o BolsaTrader envia
    avisos proativos (meta de lucro/perda atingida, sinal do robô consultor)
    quando o envio está configurado (ver core.services.enviar_whatsapp e
    core.services.WHATSAPP_ENVIO_CONFIGURADO).

    Criado sob demanda (get_or_create) pela view accounts.views.minha_conta,
    não em todo cadastro de usuário - por isso é OneToOne opcional, não uma
    extensão obrigatória do User.
    """

    usuario = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="perfil"
    )
    numero_whatsapp = models.CharField(
        "Número do WhatsApp para avisos", max_length=20, blank=True,
        help_text="Formato internacional, só dígitos (ex: 5565999998888). Deixe em branco para não "
        "receber avisos automáticos por WhatsApp - os avisos continuam aparecendo em Alertas normalmente.",
    )
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Perfil do usuário"
        verbose_name_plural = "Perfis dos usuários"

    def __str__(self):
        return f"Perfil de {self.usuario.username}"

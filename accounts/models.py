from django.conf import settings
from django.db import models


class PerfilUsuario(models.Model):
    """
    Dados complementares do usuário, além do que o User padrão do Django já
    guarda - hoje só o número de WhatsApp de contato do usuário (o
    BolsaTrader não envia mensagens para ele; os alertas aparecem só na tela).

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
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Perfil do usuário"
        verbose_name_plural = "Perfis dos usuários"

    def __str__(self):
        return f"Perfil de {self.usuario.username}"

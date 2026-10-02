from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User

from .models import PerfilUsuario


class CadastroForm(UserCreationForm):
    """Formulário de cadastro de novo usuário (investidor)."""

    email = forms.EmailField(required=True, label="E-mail")
    first_name = forms.CharField(required=True, label="Nome")
    # Gravado em PerfilUsuario.numero_whatsapp - é o mesmo número editado depois em "Minha Conta".
    telefone = forms.CharField(
        required=False, label="Telefone (WhatsApp)", max_length=20,
        widget=forms.TextInput(attrs={"placeholder": "5565999998888", "inputmode": "tel"}),
        help_text="Formato internacional, com DDI e DDD (ex: 5565999998888). Opcional - usado para "
        "receber avisos automáticos por WhatsApp.",
    )

    field_order = ["username", "first_name", "email", "telefone", "password1", "password2"]

    class Meta:
        model = User
        fields = ["username", "first_name", "email", "password1", "password2"]
        labels = {
            "username": "Usuário",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({"class": "form-control-futurista"})

    def clean_telefone(self):
        # Aceita a máscara que o usuário costuma digitar (+55 (65) 99999-8888) e guarda só os dígitos.
        telefone = self.cleaned_data["telefone"].strip()
        numero = "".join(c for c in telefone if c not in " ()-+.")
        if numero and not numero.isdigit():
            raise forms.ValidationError("Informe um telefone válido, só com números (ex: 5565999998888).")
        return numero

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        user.first_name = self.cleaned_data["first_name"]
        if commit:
            user.save()
            if self.cleaned_data["telefone"]:
                PerfilUsuario.objects.update_or_create(
                    usuario=user, defaults={"numero_whatsapp": self.cleaned_data["telefone"]}
                )
        return user


class LoginFuturistaForm(forms.Form):
    """Apenas para estilizar o form de login padrão do Django, se necessário."""

    username = forms.CharField(label="Usuário")
    password = forms.CharField(widget=forms.PasswordInput, label="Senha")


class PerfilUsuarioForm(forms.ModelForm):
    """
    Edita o número de WhatsApp para onde o BolsaTrader envia avisos
    proativos (meta de lucro/perda atingida, sinal do robô consultor) - ver
    core.services.enviar_whatsapp. Deixar em branco desliga o envio pra esse
    usuário; os alertas continuam aparecendo normalmente na tela.
    """

    class Meta:
        model = PerfilUsuario
        fields = ["numero_whatsapp"]
        labels = {"numero_whatsapp": "Número do WhatsApp para avisos"}
        widgets = {
            "numero_whatsapp": forms.TextInput(attrs={"placeholder": "5565999998888"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({"class": "form-control-futurista"})

    def clean_numero_whatsapp(self):
        numero = self.cleaned_data["numero_whatsapp"].strip()
        if numero and not numero.isdigit():
            raise forms.ValidationError(
                "Use só dígitos, no formato internacional (ex: 5565999998888), sem espaços, "
                "parênteses, traço ou o sinal de +."
            )
        return numero

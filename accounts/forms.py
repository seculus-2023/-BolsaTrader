import re

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
        help_text="Formato internacional, com DDI e DDD (ex: 5565999998888). Opcional.",
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
    Edita os dados da conta do usuário (todos opcionais): o número de
    WhatsApp de contato (o BolsaTrader não envia mensagens para ele) e o bot
    do Telegram por onde o usuário recebe os avisos de alerta.
    """

    class Meta:
        model = PerfilUsuario
        fields = ["numero_whatsapp", "telegram_bot_token", "telegram_chat_id"]
        labels = {"numero_whatsapp": "Número do WhatsApp"}
        widgets = {
            "numero_whatsapp": forms.TextInput(attrs={"placeholder": "5565999998888"}),
            "telegram_bot_token": forms.TextInput(
                attrs={"placeholder": "123456789:AAF...", "autocomplete": "off"}
            ),
            "telegram_chat_id": forms.TextInput(attrs={"placeholder": "2065125150"}),
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

    def clean_telegram_bot_token(self):
        token = self.cleaned_data["telegram_bot_token"].strip()
        if token and not re.fullmatch(r"\d+:[\w-]+", token):
            raise forms.ValidationError(
                "Token inválido - copie exatamente como o @BotFather mostrou (ex: 123456789:AAF...)."
            )
        return token

    def clean_telegram_chat_id(self):
        chat_id = self.cleaned_data["telegram_chat_id"].strip()
        if chat_id and not re.fullmatch(r"-?\d+", chat_id):
            raise forms.ValidationError("Chat ID inválido - use só números (ex: 2065125150).")
        return chat_id

    def clean(self):
        dados = super().clean()
        # só valida o par quando os dois campos passaram na validação individual
        if "telegram_bot_token" in dados and "telegram_chat_id" in dados:
            if bool(dados["telegram_bot_token"]) != bool(dados["telegram_chat_id"]):
                raise forms.ValidationError(
                    "Para receber avisos pelo Telegram, preencha o token do bot e o Chat ID "
                    "(ou deixe os dois em branco)."
                )
        return dados

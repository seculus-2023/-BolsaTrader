"""
Criptografia de campos sensíveis do perfil (senha e assinatura eletrônica da
corretora, token do bot do Telegram) - gravados no banco cifrados com Fernet
(AES-128 + HMAC), para que um vazamento do banco ou de um backup não exponha
as credenciais em texto puro.

A chave vem de CHAVE_CRIPTOGRAFIA_CAMPOS no .env; sem ela, é derivada da
SECRET_KEY - nesse caso, trocar a SECRET_KEY torna os valores já gravados
ilegíveis (o campo volta vazio e o usuário precisa informá-los de novo).
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

# Marca os valores cifrados - valores sem o prefixo são texto puro antigo
# (anterior à criptografia) e são lidos como estão, até serem salvos de novo.
PREFIXO = "enc1:"


def _fernet() -> Fernet:
    chave = getattr(settings, "CHAVE_CRIPTOGRAFIA_CAMPOS", "")
    if not chave:
        chave = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
    return Fernet(chave)


def criptografar(valor: str) -> str:
    if not valor or valor.startswith(PREFIXO):
        return valor
    return PREFIXO + _fernet().encrypt(valor.encode()).decode()


def descriptografar(valor: str) -> str:
    if not valor or not valor.startswith(PREFIXO):
        return valor
    try:
        return _fernet().decrypt(valor[len(PREFIXO):].encode()).decode()
    except InvalidToken:
        # chave trocada: melhor um campo vazio que o texto cifrado na tela
        return ""


class CampoTextoCriptografado(models.TextField):
    """
    Texto gravado cifrado no banco e devolvido decifrado ao Python. O
    max_length vale só para a validação do formulário (o valor cifrado é
    bem maior que o original). Como a cifra muda a cada gravação, buscas por
    igualdade (filter(campo="x")) não funcionam - só vazio/não vazio.
    """

    def from_db_value(self, value, expression, connection):
        return descriptografar(value) if value is not None else value

    def to_python(self, value):
        if isinstance(value, str):
            return descriptografar(value)
        return value

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        return criptografar(value) if isinstance(value, str) else value

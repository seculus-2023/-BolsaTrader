"""
Configurações do projeto BolsaTrader.

Sistema de compra e venda de ações (B3) com login, dashboard de posições,
cálculo de lucro/perda, cotações diárias e análise simples de tendência
de mercado (alta/baixa).
"""

import os
from pathlib import Path
from dotenv import load_dotenv
from decouple import config, Csv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _safe_env_value(name, default=""):
    """Normaliza valores vindos do ambiente para evitar erros de codificação
    em parâmetros de conexão do PostgreSQL (ex.: .env em ISO-8859-1/Windows).
    """
    value = config(name, default=default)
    if isinstance(value, str):
        clean = value.strip()
        clean = "".join(ch for ch in clean if ch.isascii())
        return clean or default
    return value


def _safe_db_value(name, default=""):
    """Remove caracteres não-ASCII de valores de conexão do PostgreSQL
    para evitar UnicodeDecodeError em ambientes Windows com .env corrompido.
    """
    return _safe_env_value(name, default)

# --------------------------------------------------------------------------
# Segurança
# --------------------------------------------------------------------------
SECRET_KEY = config(
    "SECRET_KEY",
    default="django-insecure-troque-esta-chave-em-producao-0000000000",
)

DEBUG = config("DEBUG", default=True, cast=bool)

ALLOWED_HOSTS = config("ALLOWED_HOSTS", default="127.0.0.1,localhost", cast=Csv())


# --------------------------------------------------------------------------
# HTTPS em produção - o Service Worker do PWA (ver static/js/service-worker-
# source.js) só registra em conexões HTTPS ou em localhost, então sem isso o
# app não fica instalável nem funciona offline em produção. Só entra em vigor
# com DEBUG=False, pra não quebrar o runserver local (HTTP simples).
# --------------------------------------------------------------------------
if not DEBUG:
    SECURE_SSL_REDIRECT = config("SECURE_SSL_REDIRECT", default=True, cast=bool)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = config("SECURE_HSTS_SECONDS", default=31536000, cast=int)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

# Só habilite ATRAS_DE_PROXY_HTTPS se o Django estiver atrás de um proxy/load
# balancer confiável que sobrescreve o cabeçalho X-Forwarded-Proto (ex: Nginx,
# Cloudflare) - nunca ligue isso com o Django exposto direto à internet, pois
# nesse caso um cliente poderia forjar o cabeçalho e enganar o Django fazendo-o
# achar que uma conexão HTTP comum é segura.
if config("ATRAS_DE_PROXY_HTTPS", default=False, cast=bool):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


# --------------------------------------------------------------------------
# Aplicações
# --------------------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    # apps do projeto
    "accounts",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "bolsatrader.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.alertas_pendentes",
                "core.context_processors.whatsapp_link",
                "core.context_processors.ultima_atualizacao_cotacoes",
                "core.context_processors.horario_b3",
            ],
        },
    },
]

WSGI_APPLICATION = "bolsatrader.wsgi.application"


# --------------------------------------------------------------------------
# Banco de dados - PostgreSQL
# --------------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _safe_db_value("DB_NAME", "bolsatrader"),
        "USER": _safe_db_value("DB_USER", "postgres"),
        "PASSWORD": _safe_db_value("DB_PASSWORD", "admin"),
        "HOST": _safe_db_value("DB_HOST", "localhost"),
        "PORT": _safe_db_value("DB_PORT", "5432"),
    }
}


# --------------------------------------------------------------------------
# Validação de senha
# --------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# --------------------------------------------------------------------------
# Internacionalização
# --------------------------------------------------------------------------
LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_I18N = True
USE_TZ = True


# --------------------------------------------------------------------------
# Arquivos estáticos
# --------------------------------------------------------------------------
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Serve os arquivos estáticos direto pelo Gunicorn (sem precisar de Nginx em
# frente), com compressão e cache de longa duração - usado em produção/Docker.
# Só entra em vigor com DEBUG=False: o storage com manifest exige que
# "collectstatic" já tenha rodado, o que nunca acontece no runserver/testes
# locais - por isso o padrão do Django é usado em desenvolvimento.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        if not DEBUG
        else "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------
# Autenticação
# --------------------------------------------------------------------------
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"


# --------------------------------------------------------------------------
# Integração com API de cotações (brapi.dev - cotações da B3)
# --------------------------------------------------------------------------
BRAPI_BASE_URL = config("BRAPI_BASE_URL", default="https://brapi.dev/api")
BRAPI_TOKEN = config("BRAPI_TOKEN", default="v2rExiL92yBndDgQxdSUT9")  # opcional, ver manual de instalação

# De quantos em quantos minutos o comando "atualizar_cotacoes --loop" busca as
# cotações de novo (ver core/management/commands/atualizar_cotacoes.py). Não
# afeta o botão "Atualizar cotações agora" do site, que é sempre manual/na hora.
COTACOES_INTERVALO_MINUTOS = config("COTACOES_INTERVALO_MINUTOS", default=60, cast=int)

# Parâmetros padrão para avisos de lucro/perda quando o usuário não define
META_LUCRO_PADRAO = config("META_LUCRO_PADRAO", default=5.0, cast=float)   # %
META_PERDA_PADRAO = config("META_PERDA_PADRAO", default=-5.0, cast=float)  # %

# Quantidade máxima de ativos DIFERENTES que cada usuário pode ter comprados
# (em carteira) ao mesmo tempo - não conta reservas, nem limita a quantidade
# de ações dentro de um ativo já comprado, só o número de tickers distintos.
# Ao tentar comprar (ou efetivar uma reserva) um ativo novo além do limite, o
# sistema recusa a operação com uma mensagem explicando a cota atingida (ver
# core.forms.OperacaoForm e core.forms.ConfirmarCompraForm).
MAX_ATIVOS_EM_CARTEIRA = config("MAX_ATIVOS_EM_CARTEIRA", default=10, cast=int)

# De quantos em quantos minutos o comando "atualizar_noticias --loop" busca as
# manchetes de novo (ver core/management/commands/atualizar_noticias.py). Não
# afeta o botão "Atualizar notícias agora" do site, que é sempre manual/na hora.
# Padrão: 1440 min (24h) - as fontes de notícias são lidas uma vez por dia.
NOTICIAS_INTERVALO_MINUTOS = config("NOTICIAS_INTERVALO_MINUTOS", default=1440, cast=int)


# --------------------------------------------------------------------------
# TradingView: URL padrão do campo "abrir no TradingView" da tela TradingView
# (ver core/views.py:tradingview) - o usuário pode digitar/colar outra URL na
# hora, este é só o valor pré-preenchido no campo.
# --------------------------------------------------------------------------
TRADINGVIEW_URL_PESQUISA = config(
    "TRADINGVIEW_URL_PESQUISA",
    default="https://br.tradingview.com/markets/stocks-brazil/market-movers-all-stocks/",
)


# --------------------------------------------------------------------------
# Horário de negociação da B3 (Bolsa de Valores) - mostrado em destaque nas
# telas de Painel de Controle, Posições em Carteira e Minhas Operações (ver
# core.context_processors.horario_b3). Horário padrão: pregão regular
# (10h-17h, sem pré/pós-mercado), de segunda a sexta.
# --------------------------------------------------------------------------
B3_HORARIO_ABERTURA = config("B3_HORARIO_ABERTURA", default="10:00")
B3_HORARIO_FECHAMENTO = config("B3_HORARIO_FECHAMENTO", default="17:00")


# --------------------------------------------------------------------------
# WhatsApp: ícone de contato + webhook para receber mensagens (Meta Cloud API)
# --------------------------------------------------------------------------
# Número usado pelo ícone "Falar no WhatsApp" (formato internacional, só dígitos).
WHATSAPP_NUMERO = config("WHATSAPP_NUMERO", default="5565981132995")

# Token de verificação do webhook (definido também no painel da Meta Cloud API
# ao cadastrar a URL do webhook). Sem isso configurado, a verificação do
# webhook (GET) sempre falha - o ícone de contato funciona normalmente mesmo
# assim, só o recebimento automático de mensagens depende disso.
WHATSAPP_VERIFY_TOKEN = config("WHATSAPP_VERIFY_TOKEN", default="")

# App Secret do app da Meta, usado para validar a assinatura (X-Hub-Signature-256)
# das requisições do webhook. Sem isso configurado, a assinatura não é validada
# - recomendado preencher antes de expor o webhook publicamente em produção.
WHATSAPP_APP_SECRET = config("WHATSAPP_APP_SECRET", default="")

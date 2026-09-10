"""URLs raiz do projeto BolsaTrader."""

from django.contrib import admin
from django.urls import path, include

from core.views import offline_view, service_worker_view, whatsapp_webhook

urlpatterns = [
    path("admin/", admin.site.urls),
    path("contas/", include("accounts.urls")),
    # Infraestrutura do PWA: precisam ficar na raiz do site (fora dos apps),
    # pois o Service Worker só controla URLs dentro do diretório onde é servido.
    path("sw.js", service_worker_view, name="service_worker"),
    path("offline/", offline_view, name="offline"),
    # Webhook da Meta Cloud API (WhatsApp): chamado pelos servidores da Meta, sem login.
    path("webhook/whatsapp/", whatsapp_webhook, name="whatsapp_webhook"),
    path("", include("core.urls")),
]

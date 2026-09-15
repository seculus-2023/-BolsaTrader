from django.contrib import admin

from .models import PerfilUsuario


@admin.register(PerfilUsuario)
class PerfilUsuarioAdmin(admin.ModelAdmin):
    list_display = ("usuario", "numero_whatsapp", "atualizado_em")
    search_fields = ("usuario__username", "numero_whatsapp")

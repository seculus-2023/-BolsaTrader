from django.contrib import admin

from .models import ConviteCadastro, PerfilUsuario


@admin.register(PerfilUsuario)
class PerfilUsuarioAdmin(admin.ModelAdmin):
    list_display = ("usuario", "numero_whatsapp", "telegram_chat_id", "acesso_ate", "atualizado_em")
    search_fields = ("usuario__username", "numero_whatsapp")


@admin.register(ConviteCadastro)
class ConviteCadastroAdmin(admin.ModelAdmin):
    list_display = ("observacao", "criado_por", "criado_em", "expira_em", "acesso_ate", "usado_por", "cancelado")
    readonly_fields = ("token",)

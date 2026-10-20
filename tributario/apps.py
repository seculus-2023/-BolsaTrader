from django.apps import AppConfig


class TributarioConfig(AppConfig):
    """Módulo tributário: preço médio fiscal, apuração de IR, DARF e informes para a declaração."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "tributario"
    verbose_name = "Tributário"

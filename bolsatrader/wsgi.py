"""
WSGI config for bolsatrader project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "bolsatrader.settings")

application = get_wsgi_application()

# Liga o agendador embutido de cotações (ver core.services.
# iniciar_agendador_cotacoes_embutido) só aqui - este módulo só é importado
# quando um servidor WSGI de verdade (Waitress, gunicorn) sobe a aplicação,
# nunca durante "manage.py migrate/test/shell/runserver" etc., que não
# passam por wsgi.py. Assim o agendador nunca dispara chamadas de rede
# durante a suíte de testes nem duplica trabalho em comandos avulsos.
from core.services import iniciar_agendador_cotacoes_embutido  # noqa: E402

iniciar_agendador_cotacoes_embutido()

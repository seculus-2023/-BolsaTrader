import os
import sys

from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        """
        Liga o agendador embutido de cotações (ver core.services.
        iniciar_agendador_cotacoes_embutido) também quando o site roda via
        "manage.py runserver" - sem isso, ele só nascia em produção via
        Waitress/gunicorn (ligado em bolsatrader/wsgi.py), e quem usa só o
        runserver no dia a dia nunca tinha atualização automática de
        verdade, mesmo com COTACOES_INTERVALO_MINUTOS configurado.

        ready() roda pra QUALQUER comando de management (migrate, test,
        shell, makemigrations...), não só runserver - por isso o primeiro
        filtro é `"runserver" not in sys.argv`, senão o agendador ligaria
        durante a suíte de testes e faria chamadas de rede de verdade.

        Com o autoreloader do runserver ligado (padrão), o Django sobe um
        processo "pai" que só observa arquivos e um processo "filho" que
        atende de verdade, marcado com a variável de ambiente RUN_MAIN - só
        o filho deve iniciar o agendador, senão ele nasceria em dobro a
        cada reinício automático por alteração de arquivo. Com
        "--noreload", existe só um processo e RUN_MAIN nunca é definida,
        então esse caso entra pela outra condição.
        """
        if "runserver" not in sys.argv:
            return
        if "--noreload" not in sys.argv and os.environ.get("RUN_MAIN") != "true":
            return

        from .services import iniciar_agendador_cotacoes_embutido
        iniciar_agendador_cotacoes_embutido()

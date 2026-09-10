"""
Comando de management para buscar as manchetes atuais de todas as fontes de
notícias ativas (ver core.models.FonteNoticia) e gravar as que ainda não
existem.

Uso manual (roda uma vez e termina):
    python manage.py atualizar_noticias

Uso em loop contínuo (fica rodando e atualiza de N em N minutos, sem precisar
de cron/Task Scheduler externo). N vem de NOTICIAS_INTERVALO_MINUTOS no .env
por padrão (1440 min = 24h), ou pode ser informado na hora com --intervalo:
    python manage.py atualizar_noticias --loop
    python manage.py atualizar_noticias --loop --intervalo 720

Uso alternativo (agendado externamente, ex. uma vez por dia via cron no
servidor) - ver manual de instalação, exemplo:
    0 8 * * * /caminho/venv/bin/python /caminho/manage.py atualizar_noticias
"""

import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.models import FonteNoticia
from core.services import atualizar_noticias_fonte, NoticiaScrapingError


class Command(BaseCommand):
    help = "Busca as manchetes atuais de todas as fontes de notícias ativas e grava as que ainda não existem."

    def add_arguments(self, parser):
        parser.add_argument(
            "--loop",
            action="store_true",
            help="Fica rodando e atualizando as notícias repetidamente, em vez de rodar uma única vez.",
        )
        parser.add_argument(
            "--intervalo",
            type=int,
            default=None,
            help="Minutos entre cada atualização no modo --loop (padrão: NOTICIAS_INTERVALO_MINUTOS do .env).",
        )

    def handle(self, *args, **options):
        if not options["loop"]:
            self._atualizar_tudo()
            return

        intervalo = options["intervalo"]
        if intervalo is None:
            intervalo = settings.NOTICIAS_INTERVALO_MINUTOS
        if intervalo <= 0:
            raise CommandError("--intervalo precisa ser maior que zero.")

        self.stdout.write(f"Modo contínuo: atualizando a cada {intervalo} minuto(s). Ctrl+C para parar.")
        try:
            while True:
                self._atualizar_tudo()
                self.stdout.write(f"Próxima atualização em {intervalo} minuto(s)...\n")
                time.sleep(intervalo * 60)
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("Interrompido pelo usuário."))

    def _atualizar_tudo(self):
        fontes = FonteNoticia.objects.filter(ativa=True)
        total = fontes.count()
        novas_total, falhas = 0, 0

        self.stdout.write(f"Buscando manchetes de {total} fonte(s)...")

        for fonte in fontes:
            try:
                novas_ids = atualizar_noticias_fonte(fonte)
                novas_total += len(novas_ids)
                self.stdout.write(self.style.SUCCESS(f"  OK  {fonte.nome}: {len(novas_ids)} notícia(s) nova(s)"))
            except NoticiaScrapingError as exc:
                falhas += 1
                self.stdout.write(self.style.WARNING(f"  FALHA {fonte.nome}: {exc}"))

        self.stdout.write(
            self.style.SUCCESS(f"Concluído. {novas_total} notícia(s) nova(s) no total, {falhas} falha(s).")
        )

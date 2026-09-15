"""
Comando de management para atualizar o histórico de benchmarks (Ibovespa e
CDI) usado no comparativo de desempenho da carteira (ver
core.services.calcular_comparativo_benchmark) - roda separado de
"atualizar_cotacoes" porque os benchmarks não dependem de nenhum usuário ter
ativos cadastrados, e não precisam ser atualizados com a mesma frequência
(uma vez por dia, após o fechamento, já é suficiente).

Uso manual (roda uma vez e termina):
    python manage.py atualizar_benchmarks

Uso em loop contínuo, como o "atualizar_cotacoes --loop":
    python manage.py atualizar_benchmarks --loop
    python manage.py atualizar_benchmarks --loop --intervalo 1440

Uso alternativo (agendado externamente via cron, uma vez por dia):
    35 18 * * 1-5 /caminho/venv/bin/python /caminho/manage.py atualizar_benchmarks
"""

import time

from django.core.management.base import BaseCommand, CommandError

from core.services import atualizar_benchmarks

INTERVALO_PADRAO_MINUTOS = 1440  # 24h - benchmarks fecham uma vez por dia, não precisam de mais que isso


class Command(BaseCommand):
    help = "Atualiza o histórico de benchmarks (Ibovespa e CDI) usado no comparativo com a carteira."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dias", type=int, default=180,
            help="Quantos dias corridos de histórico buscar na API (padrão: 180).",
        )
        parser.add_argument(
            "--loop", action="store_true",
            help="Fica rodando e atualizando repetidamente, em vez de rodar uma única vez.",
        )
        parser.add_argument(
            "--intervalo", type=int, default=None,
            help=f"Minutos entre cada atualização no modo --loop (padrão: {INTERVALO_PADRAO_MINUTOS}).",
        )

    def handle(self, *args, **options):
        dias = options["dias"]

        if not options["loop"]:
            self._atualizar(dias)
            return

        intervalo = options["intervalo"] or INTERVALO_PADRAO_MINUTOS
        if intervalo <= 0:
            raise CommandError("--intervalo precisa ser maior que zero.")

        self.stdout.write(f"Modo contínuo: atualizando a cada {intervalo} minuto(s). Ctrl+C para parar.")
        try:
            while True:
                self._atualizar(dias)
                self.stdout.write(f"Próxima atualização em {intervalo} minuto(s)...\n")
                time.sleep(intervalo * 60)
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("Interrompido pelo usuário."))

    def _atualizar(self, dias):
        resultado = atualizar_benchmarks(dias=dias)
        self.stdout.write(
            self.style.SUCCESS(
                f"Ibovespa: {resultado['ibovespa']} cotação(ões) nova(s) | "
                f"CDI: {resultado['cdi']} taxa(s) nova(s)"
            )
        )
        for erro in resultado["erros"]:
            self.stdout.write(self.style.WARNING(f"  FALHA: {erro}"))

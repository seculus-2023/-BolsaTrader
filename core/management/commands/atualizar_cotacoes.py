"""
Comando de management para atualizar as cotações diárias de todos os ativos
que possuem alguma operação registrada no sistema, gerar os alertas de
lucro/perda/tendência correspondentes e rodar o "robô consultor" (sinais de
compra/venda a partir de RSI/MACD/tendência - ver
core.services.gerar_sinais_robo_para_usuario). É o mesmo comando que faz o
robô funcionar em segundo plano, já que o sinal técnico depende da cotação
recém-atualizada.

Uso manual (roda uma vez e termina):
    python manage.py atualizar_cotacoes

Uso em loop contínuo (fica rodando e atualiza de N em N minutos, sem precisar
de cron/Task Scheduler externo). N vem de COTACOES_INTERVALO_MINUTOS no .env
por padrão, ou pode ser informado na hora com --intervalo:
    python manage.py atualizar_cotacoes --loop
    python manage.py atualizar_cotacoes --loop --intervalo 15

Uso alternativo (agendado externamente, ex. uma vez por dia após o fechamento
do pregão via cron no servidor) - ver manual de instalação, exemplo:
    30 18 * * 1-5 /caminho/venv/bin/python /caminho/manage.py atualizar_cotacoes
"""

import time

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from core.models import Ativo
from core.services import (
    atualizar_cotacao_diaria,
    gerar_alertas_para_usuario,
    gerar_sinais_robo_para_usuario,
    BrapiError,
)


class Command(BaseCommand):
    help = (
        "Atualiza as cotações diárias dos ativos em carteira, gera alertas de lucro/perda e tendência, "
        "e roda o robô consultor de sinais de compra/venda."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--loop",
            action="store_true",
            help="Fica rodando e atualizando as cotações repetidamente, em vez de rodar uma única vez.",
        )
        parser.add_argument(
            "--intervalo",
            type=int,
            default=None,
            help="Minutos entre cada atualização no modo --loop (padrão: COTACOES_INTERVALO_MINUTOS do .env).",
        )

    def handle(self, *args, **options):
        if not options["loop"]:
            self._atualizar_tudo()
            return

        intervalo = options["intervalo"]
        if intervalo is None:
            intervalo = settings.COTACOES_INTERVALO_MINUTOS
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
        ativos = Ativo.objects.filter(operacoes__isnull=False).distinct()
        total = ativos.count()
        atualizados = 0
        falhas = 0

        self.stdout.write(f"Atualizando cotações de {total} ativo(s)...")

        for ativo in ativos:
            try:
                cotacao = atualizar_cotacao_diaria(ativo)
                atualizados += 1
                self.stdout.write(
                    self.style.SUCCESS(f"  OK  {ativo.ticker}: R$ {cotacao.preco_fechamento}")
                )
            except BrapiError as exc:
                falhas += 1
                self.stdout.write(self.style.WARNING(f"  FALHA {ativo.ticker}: {exc}"))

        self.stdout.write(f"Cotações atualizadas: {atualizados} | Falhas: {falhas}")

        self.stdout.write("Gerando alertas de lucro/perda/tendência e sinais do robô consultor...")
        total_alertas = 0
        total_sinais_robo = 0
        for usuario in get_user_model().objects.filter(operacoes__isnull=False).distinct():
            novos = gerar_alertas_para_usuario(usuario)
            total_alertas += len(novos)
            novos_sinais = gerar_sinais_robo_para_usuario(usuario)
            total_sinais_robo += len(novos_sinais)

        self.stdout.write(
            self.style.SUCCESS(
                f"Concluído. {total_alertas} alerta(s) e {total_sinais_robo} sinal(is) do robô gerado(s)."
            )
        )

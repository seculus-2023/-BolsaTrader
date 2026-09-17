"""
Comando de management para atualizar as cotações diárias de todos os ativos
que possuem alguma operação registrada no sistema, gerar os alertas de
lucro/perda/tendência correspondentes e rodar o "robô consultor" (sinais de
compra/venda a partir de RSI/MACD/tendência - ver
core.services.gerar_sinais_robo_para_usuario). É o mesmo ciclo que roda
sozinho em segundo plano dentro do próprio processo do servidor web (ver
core.services.iniciar_agendador_cotacoes_embutido, ligado em
bolsatrader/wsgi.py) - normalmente não é preciso rodar este comando à parte,
a menos que o agendador embutido esteja desligado (AGENDADOR_COTACOES_
EMBUTIDO=False no .env, usado em deploys com mais de um processo worker).

Uso manual (roda uma vez e termina):
    python manage.py atualizar_cotacoes

Uso em loop contínuo (alternativa ao agendador embutido, útil quando ele está
desligado): fica rodando e atualiza de N em N minutos. N vem de
COTACOES_INTERVALO_MINUTOS no .env por padrão, ou pode ser informado na hora
com --intervalo:
    python manage.py atualizar_cotacoes --loop
    python manage.py atualizar_cotacoes --loop --intervalo 15

Uso alternativo (agendado externamente, ex. uma vez por dia após o fechamento
do pregão via cron no servidor) - ver manual de instalação, exemplo:
    30 18 * * 1-5 /caminho/venv/bin/python /caminho/manage.py atualizar_cotacoes
"""

import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.services import executar_ciclo_atualizacao_cotacoes


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
        self.stdout.write("Atualizando cotações, alertas e sinais do robô consultor...")
        resultado = executar_ciclo_atualizacao_cotacoes()
        self.stdout.write(
            f"Cotações atualizadas: {resultado['ativos_atualizados']}/{resultado['ativos_total']} "
            f"| Falhas: {resultado['ativos_falha']}"
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Concluído. {resultado['alertas_gerados']} alerta(s) e "
                f"{resultado['sinais_robo_gerados']} sinal(is) do robô gerado(s)."
            )
        )

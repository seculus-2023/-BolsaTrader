"""
Comando de management para preencher de uma vez o histórico de cotações
diárias (Cotacao) dos ativos que ainda têm poucos pregões registrados -
complementa o backfill automático que roda ao cadastrar um ativo novo (ver
core.views.operacao_nova), cobrindo ativos cadastrados antes dessa função
existir ou que, por algum motivo, ficaram com histórico curto.

Sem histórico suficiente, RSI (14 pregões) e MACD (26+9=35 pregões) ficam
"Aguardando histórico" - ver core.services.analisar_indicadores_tecnicos.

Uso:
    python manage.py backfill_cotacoes
    python manage.py backfill_cotacoes --dias 365 --minimo 40
    python manage.py backfill_cotacoes --todos   # ver --todos abaixo

--todos processa TODOS os ativos, mesmo os que já têm histórico suficiente -
útil só uma vez para completar o campo volume (ver Scanner Técnico,
core.services.analisar_volume_precos) em cotações antigas gravadas antes
desse campo existir; backfill_historico_cotacoes não sobrescreve preço, só
completa o volume que estiver faltando.
"""

from django.core.management.base import BaseCommand
from django.db.models import Count

from core.models import Ativo
from core.services import backfill_historico_cotacoes, BrapiError

# 26 (EMA longa do MACD) + 9 (período do sinal) = 35 pregões é o mínimo que
# analisar_indicadores_tecnicos precisa para sair de "Aguardando histórico".
MINIMO_PADRAO_PREGOES = 35


class Command(BaseCommand):
    help = "Preenche o histórico de cotações (Cotacao) dos ativos com poucos pregões registrados."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dias", type=int, default=180,
            help="Quantos dias corridos de histórico buscar na API (padrão: 180).",
        )
        parser.add_argument(
            "--minimo", type=int, default=MINIMO_PADRAO_PREGOES,
            help=f"Só processa ativos com menos que esta quantidade de cotações já salvas "
            f"(padrão: {MINIMO_PADRAO_PREGOES}, o mínimo que o MACD exige).",
        )
        parser.add_argument(
            "--todos", action="store_true",
            help="Processa todos os ativos, ignorando --minimo - use uma vez para completar o "
            "campo volume em cotações antigas (ver Scanner Técnico).",
        )

    def handle(self, *args, **options):
        dias = options["dias"]
        minimo = options["minimo"]
        todos = options["todos"]

        ativos = Ativo.objects.annotate(total_cotacoes=Count("cotacoes")).order_by("ticker")
        if not todos:
            ativos = ativos.filter(total_cotacoes__lt=minimo)
        total = ativos.count()
        if todos:
            self.stdout.write(f"{total} ativo(s) no total (--todos, ignorando --minimo)...")
        else:
            self.stdout.write(f"{total} ativo(s) com menos de {minimo} cotação(ões) registrada(s)...")

        atualizados, falhas, total_gravadas = 0, 0, 0
        for ativo in ativos:
            try:
                gravadas = backfill_historico_cotacoes(ativo, dias=dias)
                atualizados += 1
                total_gravadas += gravadas
                self.stdout.write(self.style.SUCCESS(f"  OK  {ativo.ticker}: {gravadas} cotação(ões) nova(s)"))
            except BrapiError as exc:
                falhas += 1
                self.stdout.write(self.style.WARNING(f"  FALHA {ativo.ticker}: {exc}"))

        self.stdout.write(
            self.style.SUCCESS(
                f"Concluído. {atualizados} ativo(s) processado(s), {total_gravadas} cotação(ões) "
                f"gravada(s) no total, {falhas} falha(s)."
            )
        )

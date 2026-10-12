"""
Backup do banco PostgreSQL com pg_dump, compactado (.sql.gz).

Uso:
    python manage.py backup_banco
    python manage.py backup_banco --pasta D:\\backups --manter 30

Gera BACKUP_PASTA/bolsatrader_AAAAMMDD_HHMMSS.sql.gz e apaga os backups mais
antigos dessa pasta, mantendo os BACKUP_MANTER mais recentes (.env). Precisa
do pg_dump instalado (vem com o PostgreSQL) - se ele não estiver no PATH,
informe o caminho em PG_DUMP no .env.

No Windows, agende com o Agendador de Tarefas (uma vez por dia); no Docker,
o serviço "backup" do docker-compose.yml já faz isso sozinho.

Para restaurar (num banco vazio):
    gunzip -c bolsatrader_AAAAMMDD_HHMMSS.sql.gz | psql -U postgres -d bolsatrader
"""

import gzip
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

PREFIXO = "bolsatrader_"
EXTENSAO = ".sql.gz"


class Command(BaseCommand):
    help = "Gera um backup compactado do banco PostgreSQL (pg_dump) e apaga os backups antigos."

    def add_arguments(self, parser):
        parser.add_argument("--pasta", default=None, help="Pasta dos backups (padrão: BACKUP_PASTA do .env).")
        parser.add_argument(
            "--manter", type=int, default=None,
            help="Quantos backups mais recentes manter (padrão: BACKUP_MANTER do .env).",
        )

    def handle(self, *args, **opcoes):
        pasta = Path(opcoes["pasta"] or settings.BACKUP_PASTA)
        manter = opcoes["manter"] if opcoes["manter"] is not None else settings.BACKUP_MANTER
        if manter < 1:
            raise CommandError("--manter precisa ser pelo menos 1.")
        pasta.mkdir(parents=True, exist_ok=True)

        banco = settings.DATABASES["default"]
        comando = [
            settings.PG_DUMP,
            "--host", str(banco["HOST"]),
            "--port", str(banco["PORT"]),
            "--username", banco["USER"],
            "--no-owner",
            "--no-privileges",
            banco["NAME"],
        ]
        ambiente = {**os.environ, "PGPASSWORD": banco["PASSWORD"]}

        destino = pasta / f"{PREFIXO}{timezone.localtime():%Y%m%d_%H%M%S}{EXTENSAO}"
        parcial = destino.with_name(destino.name + ".parcial")
        # stderr num arquivo temporário (não num pipe): se o pg_dump escrever
        # muitos avisos enquanto o stdout é lido, um pipe cheio travaria tudo.
        with tempfile.TemporaryFile() as saida_erros:
            try:
                processo = subprocess.Popen(comando, stdout=subprocess.PIPE, stderr=saida_erros, env=ambiente)
            except FileNotFoundError:
                raise CommandError(
                    f"pg_dump não encontrado ({settings.PG_DUMP}). Instale o cliente do PostgreSQL ou "
                    "informe o caminho completo em PG_DUMP no .env."
                )
            with gzip.open(parcial, "wb") as arquivo:
                shutil.copyfileobj(processo.stdout, arquivo)
            codigo = processo.wait()
            saida_erros.seek(0)
            erros = saida_erros.read().decode(errors="replace").strip()

        if codigo != 0:
            parcial.unlink(missing_ok=True)
            raise CommandError(f"pg_dump falhou: {erros or 'erro desconhecido'}")
        parcial.replace(destino)

        tamanho_kb = destino.stat().st_size / 1024
        self.stdout.write(self.style.SUCCESS(f"Backup gerado: {destino} ({tamanho_kb:.0f} KB)"))

        antigos = sorted(pasta.glob(f"{PREFIXO}*{EXTENSAO}"), reverse=True)[manter:]
        for arquivo in antigos:
            arquivo.unlink()
        if antigos:
            self.stdout.write(f"{len(antigos)} backup(s) antigo(s) apagado(s) - mantidos os {manter} mais recentes.")

"""
Restaura um backup gerado pelo backup_banco (.sql.gz) no banco PostgreSQL.

Uso:
    python manage.py restaurar_banco                      (o mais recente)
    python manage.py restaurar_banco Backup\\bolsatrader_AAAAMMDD_HHMMSS.sql.gz
    python manage.py restaurar_banco --sim                (sem pedir confirmação)

APAGA todos os dados atuais (DROP SCHEMA public) e carrega os do backup com
o psql, tudo numa transação só: se algo der errado, o banco fica como estava.
Precisa do psql instalado (vem com o PostgreSQL) - se ele não estiver no PATH,
informe o caminho em PG_PSQL no .env. Pare o servidor antes de restaurar,
senão as conexões abertas seguram o DROP SCHEMA.
"""

import gzip
import os
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from .backup_banco import EXTENSAO, PREFIXO

LIMPAR_SCHEMA = b"DROP SCHEMA IF EXISTS public CASCADE;\nCREATE SCHEMA public;\n"


class Command(BaseCommand):
    help = "Restaura um backup .sql.gz do banco PostgreSQL (apaga os dados atuais)."

    def add_arguments(self, parser):
        parser.add_argument("arquivo", nargs="?", default=None, help="Backup a restaurar (padrão: o mais recente).")
        parser.add_argument("--pasta", default=None, help="Pasta dos backups (padrão: BACKUP_PASTA do .env).")
        parser.add_argument("--sim", action="store_true", help="Não pede confirmação.")

    def handle(self, *args, **opcoes):
        arquivo = self._escolher_arquivo(opcoes["arquivo"], Path(opcoes["pasta"] or settings.BACKUP_PASTA))
        banco = settings.DATABASES["default"]

        self.stdout.write(f"Backup: {arquivo}")
        self.stdout.write(self.style.WARNING(
            f"Isto APAGA todos os dados atuais do banco \"{banco['NAME']}\" em {banco['HOST']}:{banco['PORT']}."
        ))
        if not opcoes["sim"] and input('Digite SIM para continuar: ').strip().upper() != "SIM":
            raise CommandError("Restauração cancelada.")

        comando = [
            settings.PG_PSQL,
            "--host", str(banco["HOST"]),
            "--port", str(banco["PORT"]),
            "--username", banco["USER"],
            "--dbname", banco["NAME"],
            "--quiet",
            "--no-psqlrc",
            "--set", "ON_ERROR_STOP=1",
            "--single-transaction",
            "--file", "-",
        ]
        # client_min_messages=warning: esconde a lista de "removendo em cascata" do DROP
        ambiente = {**os.environ, "PGPASSWORD": banco["PASSWORD"], "PGOPTIONS": "-c client_min_messages=warning"}
        try:
            # stderr vai direto para o console (sem pipe), então não há risco
            # de travar enquanto o stdin é alimentado.
            processo = subprocess.Popen(comando, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, env=ambiente)
        except FileNotFoundError:
            raise CommandError(
                f"psql não encontrado ({settings.PG_PSQL}). Instale o cliente do PostgreSQL ou "
                "informe o caminho completo em PG_PSQL no .env."
            )
        try:
            processo.stdin.write(LIMPAR_SCHEMA)
            with gzip.open(arquivo, "rb") as dump:
                shutil.copyfileobj(dump, processo.stdin)
            processo.stdin.close()
        except (BrokenPipeError, OSError):
            pass  # o psql parou no meio; o código de saída abaixo explica
        if processo.wait() != 0:
            raise CommandError("psql falhou (mensagem acima) - nada foi alterado no banco.")

        self.stdout.write(self.style.SUCCESS(f"Banco restaurado a partir de {arquivo.name}."))

    def _escolher_arquivo(self, informado, pasta):
        if informado:
            arquivo = Path(informado)
            if not arquivo.is_file():
                raise CommandError(f"Arquivo não encontrado: {arquivo}")
            return arquivo
        backups = sorted(pasta.glob(f"{PREFIXO}*{EXTENSAO}"), reverse=True)
        if not backups:
            raise CommandError(f"Nenhum backup em {pasta}.")
        return backups[0]

#!/bin/sh
# Ponto de entrada do container da aplicação (Dockerfile).
# Espera o banco de dados responder, aplica as migrações, coleta os
# arquivos estáticos e sobe o servidor de produção (Gunicorn).
set -e

echo "Aguardando o banco de dados (${DB_HOST:-db}:${DB_PORT:-5432}) ficar disponível..."
python - <<'PYEOF'
import os
import socket
import time

host = os.environ.get("DB_HOST", "db")
port = int(os.environ.get("DB_PORT", "5432"))

for _ in range(60):
    try:
        with socket.create_connection((host, port), timeout=2):
            break
    except OSError:
        time.sleep(1)
else:
    raise SystemExit(f"Banco de dados {host}:{port} não respondeu a tempo.")
PYEOF

echo "Aplicando migrações..."
python manage.py migrate --noinput

echo "Coletando arquivos estáticos..."
python manage.py collectstatic --noinput

echo "Iniciando o BolsaTrader..."
exec gunicorn bolsatrader.wsgi:application --bind 0.0.0.0:8000 --workers 3

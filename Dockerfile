# BolsaTrader - imagem da aplicação Django
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Usuário sem privilégios: se alguém explorar uma falha na aplicação, não
# ganha root dentro do container.
RUN useradd --create-home --uid 1000 bolsatrader

# Código-fonte da aplicação
COPY --chown=bolsatrader:bolsatrader . .
RUN chmod +x entrypoint.sh \
    && mkdir -p staticfiles logs backups \
    && chown bolsatrader:bolsatrader staticfiles logs backups

USER bolsatrader

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]

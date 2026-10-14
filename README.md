# BolsaTrader

Sistema em Python/Django + PostgreSQL para acompanhamento de compra e venda de ações da B3 e de criptomoedas, com login, cálculo de lucro/perda, cotações automáticas, conta corrente, análise técnica, robô consultor, notícias do mercado e avisos automáticos (Telegram).

É também um **PWA (Progressive Web App)**: instalável no computador, Android e iOS, com ícone
próprio, tela cheia e uma página offline (`/offline/`) exibida quando não há conexão. O manifesto
fica em `static/manifest.webmanifest`, o Service Worker é servido em `/sw.js`
(código-fonte em `static/js/service-worker-source.js`) e o registro/botão de instalação está em
`static/js/pwa.js`.

## Início rápido (Windows ou Linux, sem Docker)

```bash
python -m venv .venv
.venv\Scripts\activate          # Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env          # Linux: cp .env.example .env - depois ajuste os valores
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Acesse http://127.0.0.1:8000/

Todas as configurações ficam no `.env`, explicadas uma a uma no [.env.example](.env.example).

## Usuários e permissões

- **Cadastro**: por padrão só administradores (staff) cadastram usuários, pelo menu "Cadastrar
  Usuário". Para abrir o cadastro ao público na tela de login, use `CADASTRO_PUBLICO=True`.
- **Só staff**: gerenciar fontes de notícias e ver as mensagens do WhatsApp.
- **Só superusuário**: "Limpar dados do sistema" em Minha Conta (escolhe no combo a conta a apagar,
  ou "Todos os usuários" para apagar os dados de todos).
- **Senha**: "Esqueci minha senha" no login envia um link por e-mail (configure `EMAIL_*` no
  `.env`; sem isso o e-mail só aparece no console do servidor). "Alterar senha" fica em Minha Conta.
- **Login**: após 5 senhas erradas seguidas, bloqueado por 15 min (`LOGIN_MAX_TENTATIVAS`,
  `LOGIN_BLOQUEIO_MINUTOS`).
- **Credenciais da corretora e token do Telegram** ficam cifrados no banco. Defina uma
  `CHAVE_CRIPTOGRAFIA_CAMPOS` própria e não a troque depois (ver `.env.example`).

## Apoio à decisão de compra

- **Qualidade das Empresas** (menu): nota de 0 a 10 pelos balanços (lucro consistente, ROE, margem,
  crescimento, dívida, P/L, dividendos). Os dados vêm dos módulos que o plano Startup da brapi libera e
  ficam salvos por 7 dias.
- **Calculadora de risco** (tela de compra): stop por ATR/suporte, quantidade para não perder mais que
  `RISCO_POR_OPERACAO_PCT` do patrimônio no stop, alvo de 2x o risco e alerta de concentração por setor
  (`LIMITE_SETOR_PCT`).
- **Backtesting do Robô**: compara os sinais do robô com comprar em qualquer dia (referência), já
  descontando custos. Quanto mais histórico, melhor: `BRAPI_HISTORICO_MAXIMO` (gratuito 3mo, Startup 1y)
  e `python manage.py backfill_cotacoes --dias 365 --todos`.

## Atualização automática

O próprio servidor atualiza as cotações a cada `COTACOES_INTERVALO_MINUTOS` (agendador
embutido). Também dá para rodar à parte:

```bash
python manage.py atualizar_cotacoes            # uma vez
python manage.py atualizar_cotacoes --loop     # repetindo (ou atualizar_cotacoes_loop.bat)
python manage.py atualizar_noticias --loop     # manchetes das fontes de notícias
```

## Backup do banco

```bash
python manage.py backup_banco                  # ou backup_banco.bat no Windows
```

Gera `backups/bolsatrader_AAAAMMDD_HHMMSS.sql.gz` e mantém os 14 mais recentes (`BACKUP_PASTA`,
`BACKUP_MANTER`). Precisa do `pg_dump` (vem com o PostgreSQL); se não estiver no PATH, informe o
caminho em `PG_DUMP`. No Windows, agende o `backup_banco.bat` no Agendador de Tarefas
(diariamente). **Guarde uma cópia fora do servidor** (nuvem, HD externo).

Para restaurar num banco vazio:

```bash
gunzip -c backups/bolsatrader_AAAAMMDD_HHMMSS.sql.gz | psql -U postgres -d bolsatrader
```

## Docker (produção)

```bash
cp .env.example .env     # ajuste SECRET_KEY, DEBUG=False, ALLOWED_HOSTS, senhas...
docker compose up -d --build
```

| Serviço    | O que faz |
|------------|-----------|
| `db`       | PostgreSQL 16 |
| `web`      | o site (Gunicorn, 3 workers, sem root), na porta 8000, com healthcheck em `/saude/` |
| `cotacoes` | atualização automática de cotações/alertas/robô (processo único) |
| `noticias` | atualização automática das notícias |
| `backup`   | backup diário do banco em `./backups` |

Logs: `docker compose logs -f web` (ou `cotacoes`, `noticias`, `backup`).

## Logs e monitoramento

- Logs no console e em `logs/bolsatrader.log` (com rotação), com os erros do agendador, das APIs
  externas e os bloqueios de login. Nível em `LOG_NIVEL`.
- `GET /saude/` responde `{"status": "ok"}` (200) quando o site e o banco estão de pé, ou 503.
  Serve para monitores externos como o UptimeRobot.

## Testes

```bash
python manage.py test
```

Os testes também rodam sozinhos no GitHub a cada push/pull request
([.github/workflows/testes.yml](.github/workflows/testes.yml)), resultado na aba "Actions".

## Documentação

Na pasta [docs/](docs/):

- `Manual_Operacional_BolsaTrader.docx`: instalação passo a passo e manual de uso
- `BolsaTrader_Manual_Operacional.pptx`: apresentação do manual
- `Manual_Comercial_BolsaTrader.docx`: apresentação comercial
- `Analise_Precificacao_BolsaTrader.docx`: análise de precificação

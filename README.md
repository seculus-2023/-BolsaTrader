# BolsaTrader

Sistema em Python/Django + PostgreSQL para acompanhamento de compra e venda de ações da B3, com login, cálculo de lucro/perda, cotações diárias, análise simples de tendência (alta/baixa) e avisos automáticos.

É também um **PWA (Progressive Web App)**: instalável no computador, Android e iOS, com ícone
próprio, tela cheia e uma página offline (`/offline/`) exibida quando não há conexão. O manifesto
fica em `static/manifest.webmanifest`, o Service Worker é servido em `/sw.js`
(código-fonte em `static/js/service-worker-source.js`) e o registro/botão de instalação está em
`static/js/pwa.js`. Detalhes de instalação como app no manual, seção 6.

## Início rápido

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # ajuste as variáveis (banco de dados, etc.)
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Acesse http://127.0.0.1:8000/

Para atualizar as cotações diárias manualmente:

```bash
python manage.py atualizar_cotacoes
```

Para buscar as manchetes das fontes de notícias configuradas em "Notícias do Mercado" manualmente:

```bash
python manage.py atualizar_noticias
```

Para rodar os testes automatizados:

```bash
python manage.py test
```

**Consulte o manual completo (instalação passo a passo e manual de uso) no arquivo
`BolsaTrader_Manual_Instalacao_e_Uso.docx`.**

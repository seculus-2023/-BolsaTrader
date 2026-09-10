@echo off
setlocal EnableDelayedExpansion
title BolsaTrader - Instalador via Docker
cd /d "%~dp0"

echo ============================================================
echo   BolsaTrader - Instalador via Docker (Windows)
echo ============================================================
echo.

if not exist "docker-compose.yml" (
    echo [ERRO] docker-compose.yml nao encontrado nesta pasta.
    echo        Coloque este instalador na pasta raiz do projeto BolsaTrader.
    echo.
    pause
    exit /b 1
)

REM --- 1) Docker instalado? ------------------------------------------------
where docker >nul 2>&1
if errorlevel 1 (
    echo [ERRO] Docker nao foi encontrado neste computador.
    echo        Baixe e instale o Docker Desktop em:
    echo        https://www.docker.com/products/docker-desktop
    echo        Depois de instalar, reinicie o computador e rode este arquivo de novo.
    echo.
    pause
    exit /b 1
)

REM --- 2) Docker Desktop esta rodando? -------------------------------------
docker info >nul 2>&1
if errorlevel 1 (
    echo [ERRO] O Docker Desktop parece estar fechado ou ainda iniciando.
    echo        Abra o Docker Desktop, espere o icone da baleia ficar estavel
    echo        na bandeja do Windows e rode este instalador de novo.
    echo.
    pause
    exit /b 1
)
echo [OK] Docker Desktop esta rodando.

REM --- 3) "docker compose" (plugin) disponivel? ----------------------------
docker compose version >nul 2>&1
if errorlevel 1 (
    echo [ERRO] "docker compose" nao esta disponivel nesta instalacao do Docker.
    echo        Atualize o Docker Desktop para uma versao mais recente.
    echo.
    pause
    exit /b 1
)
echo [OK] Docker Compose disponivel.
echo.

REM --- 4) Cria o .env a partir do .env.example, se ainda nao existir -------
if not exist ".env" (
    if not exist ".env.example" (
        echo [ERRO] Arquivo .env.example nao encontrado. Rode este instalador na pasta do projeto.
        pause
        exit /b 1
    )

    echo Criando arquivo .env a partir do .env.example...
    copy /y ".env.example" ".env" >nul

    echo Gerando uma chave secreta (SECRET_KEY) aleatoria para este servidor...
    powershell -NoProfile -Command "$k = -join ((48..57)+(65..90)+(97..122) | Get-Random -Count 50 | ForEach-Object {[char]$_}); (Get-Content '.env') -replace '^SECRET_KEY=.*', ('SECRET_KEY=' + $k) | Set-Content '.env'"

    echo Ajustando o servidor de banco de dados para o container "db"...
    powershell -NoProfile -Command "(Get-Content '.env') -replace '^DB_HOST=.*', 'DB_HOST=db' | Set-Content '.env'"

    echo.
    echo [ATENCAO] Um arquivo .env foi criado com valores padrao ^(inclusive uma
    echo           senha de banco de dados generica^). Antes de usar em producao de
    echo           verdade, abra o arquivo .env nesta pasta e revise, em especial:
    echo             - DB_PASSWORD          ^(senha do banco de dados^)
    echo             - ALLOWED_HOSTS        ^(se for acessar por outro endereco alem de localhost^)
    echo             - DEBUG=False          ^(recomendado fora do seu computador^)
    echo           As demais variaveis ^(BRAPI_TOKEN, WHATSAPP_*, metas padrao^) sao opcionais.
    echo.
    pause
) else (
    echo [OK] Arquivo .env ja existe - mantendo as configuracoes atuais.
    echo.
)

REM --- 5) Sobe os containers (build da imagem + banco + app) ---------------
echo.
echo Construindo as imagens e iniciando os containers...
echo (a primeira vez pode demorar varios minutos, baixando as dependencias)
echo.
docker compose up -d --build
if errorlevel 1 (
    echo.
    echo [ERRO] Falha ao subir os containers. Veja as mensagens acima.
    echo        Para investigar mais: docker compose logs
    echo.
    pause
    exit /b 1
)

REM --- 6) Espera a aplicacao responder (migracoes rodam sozinhas no start) -
echo.
echo Aguardando o BolsaTrader iniciar (aplicando migracoes automaticamente)...
set /a TENTATIVAS=0

:esperar
set /a TENTATIVAS+=1
powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'http://localhost:8000/contas/login/' -UseBasicParsing -TimeoutSec 3 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 goto :app_pronta
if !TENTATIVAS! GEQ 40 goto :app_demorou
timeout /t 3 /nobreak >nul
goto :esperar

:app_pronta
echo.
echo ============================================================
echo   BolsaTrader esta rodando em: http://localhost:8000
echo ============================================================
echo.
start "" "http://localhost:8000"
goto :pos_instalacao

:app_demorou
echo.
echo [AVISO] A aplicacao ainda nao respondeu apos cerca de 2 minutos.
echo         Isso as vezes acontece na primeira execucao, com o banco de
echo         dados ainda inicializando. Verifique com: docker compose logs -f web
echo.

:pos_instalacao
echo.
set /p CRIAR_ADMIN="Deseja criar um usuario administrador agora (acesso a /admin)? (S/N): "
if /i "!CRIAR_ADMIN!"=="S" (
    docker compose exec web python manage.py createsuperuser
)

echo.
echo ------------------------------------------------------------
echo Comandos uteis (rode a partir desta pasta):
echo   docker compose logs -f web         - acompanhar os logs da aplicacao
echo   docker compose down                - parar os containers
echo   docker compose up -d               - subir de novo, sem reconstruir
echo   docker compose up -d --build       - reconstruir depois de alterar o codigo
echo   docker compose exec web python manage.py createsuperuser
echo                                       - criar outro usuario administrador
echo ------------------------------------------------------------
echo.
pause

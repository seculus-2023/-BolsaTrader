@echo off
title BolsaTrader - Restaurar backup do banco de dados
rem roda a partir da raiz do projeto (este arquivo fica na pasta backup)
cd /d "%~dp0.."

rem Restaura um backup gerado pelo backup_banco.bat (nesta mesma pasta).
rem ATENCAO: APAGA todos os dados atuais do banco e coloca os do backup.
rem Pare o servidor (runserver) antes, senao a restauracao fica esperando.
rem Se o psql nao estiver no PATH, informe o caminho em PG_PSQL no .env
rem (ex: PG_PSQL=C:\Program Files\PostgreSQL\17\bin\psql.exe).

if not exist "%~dp0bolsatrader_*.sql.gz" (
    echo Nenhum backup encontrado na pasta backup.
    pause
    exit /b 1
)

echo Backups disponiveis (mais recente primeiro):
echo.
dir /b /o-n "%~dp0bolsatrader_*.sql.gz"
echo.
set "ARQUIVO="
set /p "ARQUIVO=Nome do arquivo a restaurar (Enter = o mais recente): "

if defined ARQUIVO (
    ".venv\Scripts\python.exe" manage.py restaurar_banco "%~dp0%ARQUIVO%" --pasta "%~dp0."
) else (
    ".venv\Scripts\python.exe" manage.py restaurar_banco --pasta "%~dp0."
)
if errorlevel 1 (
    echo.
    echo FALHA na restauracao - veja a mensagem acima.
    pause
    exit /b 1
)
pause

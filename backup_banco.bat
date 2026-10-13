@echo off
title BolsaTrader - Backup do banco de dados
cd /d "%~dp0"

rem Gera um backup compactado do banco na pasta "Backup" do projeto e apaga
rem os antigos, mantendo os BACKUP_MANTER (.env, padrao 14) mais recentes.
rem Para restaurar, use restore.bat.
rem Para rodar todo dia sozinho: Agendador de Tarefas do Windows > Criar
rem Tarefa Basica > Diariamente > Iniciar um programa > este arquivo.
rem Se o pg_dump nao estiver no PATH, informe o caminho em PG_DUMP no .env
rem (ex: PG_DUMP=C:\Program Files\PostgreSQL\17\bin\pg_dump.exe).

".venv\Scripts\python.exe" manage.py backup_banco --pasta "%~dp0Backup"
if errorlevel 1 (
    echo.
    echo FALHA no backup - veja a mensagem acima.
    exit /b 1
)

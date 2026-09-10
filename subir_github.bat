@echo off
title BolsaTrader - Enviar para o GitHub
cd /d C:\PROJETOS_PYTHON\bolsatrader

echo ========================================
echo   BOLSA TRADER - ENVIAR PARA O GITHUB
echo ========================================
echo.

git status --short
echo.

set /p MENSAGEM="Mensagem do commit (descreva o que mudou): "
if "%MENSAGEM%"=="" (
    echo ERRO: a mensagem do commit nao pode ser vazia.
    pause
    exit /b 1
)

git add .
git commit -m "%MENSAGEM%"
if errorlevel 1 (
    echo.
    echo Nada para enviar - nenhuma alteracao detectada, ou falha ao criar o commit.
    pause
    exit /b 0
)

echo.
echo Enviando para o GitHub...
git push
if errorlevel 1 (
    echo.
    echo ERRO: falha ao enviar para o GitHub. Verifique sua conexao/login e tente de novo.
    pause
    exit /b 1
)

echo.
echo Codigo enviado com sucesso!
pause

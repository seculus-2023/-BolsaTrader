@echo off
title BolsaTrader - Enviar para o GitHub
cd /d C:\PROJETOS_PYTHON\bolsatrader

echo ========================================
echo   BOLSA TRADER - ENVIAR PARA O GITHUB
echo ========================================
echo.

git status --short
echo.

for /f "tokens=1-4 delims=/ " %%a in ('date /t') do set DATA_HOJE=%%a_%%b_%%c
for /f "tokens=1-2 delims=: " %%a in ('time /t') do set HORA_AGORA=%%a%%b
set MENSAGEM=%DATA_HOJE%_%HORA_AGORA%
echo Mensagem do commit (automatica): %MENSAGEM%
echo.

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

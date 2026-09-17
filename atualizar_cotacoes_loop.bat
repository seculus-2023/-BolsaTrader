@echo off
title BolsaTrader - Atualizacao automatica de cotacoes
cd /d "%~dp0"

echo ========================================
echo   BOLSATRADER - ATUALIZAR COTACOES (LOOP)
echo ========================================
echo.
echo Este script mantem as cotacoes, alertas e o robo consultor atualizando
echo sozinhos, de tempos em tempos (intervalo configurado em
echo COTACOES_INTERVALO_MINUTOS no arquivo .env).
echo.
echo NAO FECHE esta janela - fechar interrompe as atualizacoes automaticas.
echo Para parar de proposito, feche esta janela ou pressione Ctrl+C.
echo.

:loop
".venv\Scripts\python.exe" manage.py atualizar_cotacoes --loop
echo.
echo O processo parou inesperadamente. Reiniciando em 30 segundos...
echo (pressione Ctrl+C agora se quiser parar de vez)
timeout /t 30 /nobreak >nul
goto loop

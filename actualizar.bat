@echo off
cd /d "%~dp0"
title Actualizar Mi Cuarto
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0actualizar.ps1"
echo.
echo Si Mi Cuarto estaba abierto, cierralo y abre iniciar.bat de nuevo.
pause

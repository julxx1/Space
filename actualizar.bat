@echo off
cd /d "%~dp0"
title Actualizar Idearium
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0actualizar.ps1"
echo.
echo Si Idearium estaba abierto, cierralo y abre iniciar.bat de nuevo.
pause

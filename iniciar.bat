@echo off
cd /d "%~dp0"
title Mi Cuarto
set PY=
where py >nul 2>nul
if %errorlevel%==0 set PY=py
if "%PY%"=="" where python >nul 2>nul
if "%PY%"=="" if %errorlevel%==0 set PY=python
if "%PY%"=="" goto nopy
%PY% -c "import onnxruntime, numpy, PIL" >nul 2>nul
if %errorlevel%==0 goto run
echo.
echo Preparando Mi Cuarto por primera vez.
echo Instalando los componentes para aislar productos y comparar precios.
echo Esto tarda unos minutos y solo pasa una vez. No cierres esta ventana.
echo.
%PY% -m pip install --disable-pip-version-check onnxruntime numpy pillow
:run
echo.
%PY% server.py
goto fin
:nopy
echo Python no esta instalado.
echo Descargalo en https://www.python.org/downloads/ y marca "Add Python to PATH".
echo Luego vuelve a abrir este archivo.
:fin
pause

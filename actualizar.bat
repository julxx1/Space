@echo off
cd /d "%~dp0"
title Actualizar Mi Cuarto
where git >nul 2>nul
if errorlevel 1 goto nogit
if not exist ".git" goto nogitrepo
echo.
echo Buscando la ultima version de Mi Cuarto...
echo (Tus ideas, fotos y ajustes no se tocan.)
echo.
git pull --ff-only
if errorlevel 1 goto fallo
echo.
echo Listo. Ya tienes la ultima version.
echo Si Mi Cuarto estaba abierto, cierralo y abre iniciar.bat de nuevo.
goto fin
:nogit
echo Git no esta instalado. Descargalo en https://git-scm.com/download/win
echo Instalalo con las opciones por defecto y vuelve a abrir este archivo.
goto fin
:nogitrepo
echo Esta carpeta no viene de GitHub. Mira las instrucciones en LEEME.txt (seccion ACTUALIZAR).
goto fin
:fallo
echo.
echo No se pudo actualizar. Revisa tu internet o inicia sesion en GitHub cuando te lo pida.
:fin
pause

@echo off
title 3DS Hack Manager - ISA
cd /d "%~dp0"

where python >nul 2>&1
if %errorlevel% == 0 (
    python -m custominstall.gui
    goto end
)

where python3 >nul 2>&1
if %errorlevel% == 0 (
    python3 -m custominstall.gui
    goto end
)

where py >nul 2>&1
if %errorlevel% == 0 (
    py -3 -m custominstall.gui
    goto end
)

echo.
echo Python introuvable. Assure-toi que Python est installe et dans le PATH.
echo Telecharge Python sur https://www.python.org/downloads/

:end
pause

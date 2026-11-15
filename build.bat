@echo off
setlocal EnableExtensions
rem always run from the folder this file is in, even if it was started from somewhere else
cd /d "%~dp0"
title Any Link Downloader - build
echo.

rem look for a python 3.10+, launcher first, newest version first, then plain python
set "PY="
call :trypy "py -3"
if not defined PY for %%V in (3.14 3.13 3.12 3.11 3.10) do if not defined PY call :trypy "py -%%V"
if not defined PY call :trypy "python"
if not defined PY goto :installpy

:havepy
rem --clean and the other flags just get passed on to the build script
%PY% tools\build.py %*
if errorlevel 1 goto :fail
echo.
pause
exit /b 0

:trypy
rem only sets PY if that python is 3.10 or newer
%~1 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
exit /b 0

:installpy
echo  No suitable Python (3.10 or newer) found - installing one with winget (free, about a minute) ...
winget install -e --id Python.Python.3.13 --silent --accept-package-agreements --accept-source-agreements
rem a fresh winget install isnt on PATH in this window yet, so also look in the default folder
call :trypy "py -3"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if defined PY goto :havepy
echo.
echo  Could not set up Python automatically.
echo  Install any Python 3.10+ from https://www.python.org/downloads/ (tick "Add python.exe to PATH"),
echo  then run build.bat again.
pause
exit /b 1

:fail
echo.
echo  !! The build stopped because of an error (see the messages above).
pause
exit /b 1

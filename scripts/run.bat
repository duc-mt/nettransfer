@echo off
rem ==============================================================================
rem Script Name:   run.bat
rem Description:   Universal launcher for NetTransfer on Windows (CMD/PowerShell).
rem Author:        Mai Tan Duc <ducmai.network@gmail.com>
rem Created:       2026-10-11
rem Version:       1.0.0
rem License:       MIT
rem ==============================================================================
rem Usage:         run.bat [options]
rem ==============================================================================

setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
set "ROOT_DIR=%SCRIPT_DIR%..\"
set "VENV_DIR=%ROOT_DIR%.venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"

REM Check and bootstrap virtual environment if missing
if not exist "%VENV_PYTHON%" (
    echo Virtual environment not found in .venv. Initializing...

    where python >nul 2>nul
    if !ERRORLEVEL! neq 0 (
        echo Error: Python 3 is not installed or not found in PATH.
        exit /b 1
    )

    python -m venv "%VENV_DIR%"
    if !ERRORLEVEL! neq 0 (
        echo Error: Failed to create virtual environment.
        exit /b 1
    )

    if exist "%ROOT_DIR%requirements.txt" (
        echo Installing dependencies from requirements.txt...
        "%VENV_DIR%\Scripts\pip.exe" install --quiet -r "%ROOT_DIR%requirements.txt"
    )
)

REM Execute run.py with virtual environment python forwarding all arguments
"%VENV_PYTHON%" "%ROOT_DIR%run.py" %*

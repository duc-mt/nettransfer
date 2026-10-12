#!/bin/sh
# ==============================================================================
# Script Name:   run.sh
# Description:   Universal launcher for NetTransfer (Linux/macOS/WSL).
# Author:        Mai Tan Duc <ducmai.network@gmail.com>
# Created:       2026-10-11
# Version:       1.0.0
# License:       MIT
# ==============================================================================
# Usage:         ./run.sh [options]
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Check for elevated / root privileges on Unix-like systems and elevate via sudo
if [ "$(uname -s)" != "MINGW"* ] && [ "$(uname -s)" != "CYGWIN"* ]; then
    if [ "${EUID:-$(id -u)}" -ne 0 ]; then
        echo "[!] NetTransfer requires root privileges (sudo) to bind TFTP port 69."
        echo "    Re-running with sudo (use 'sudo -E ./run.sh' if GUI window fails to open)..."
        exec sudo "$0" "$@"
    fi
fi

VENV_DIR="$SCRIPT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"

# Check and bootstrap virtual environment if missing
if [ ! -f "$VENV_PYTHON" ]; then
    echo "Virtual environment not found in .venv. Initializing..."

    PYTHON_CMD=""
    if command -v python3 >/dev/null 2>&1; then
        PYTHON_CMD="python3"
    elif command -v python >/dev/null 2>&1; then
        PYTHON_CMD="python"
    else
        echo "Error: Python 3 is not installed or not in PATH."
        exit 1
    fi

    "$PYTHON_CMD" -m venv "$VENV_DIR"

    if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
        echo "Installing dependencies from requirements.txt..."
        "$VENV_DIR/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"
    fi
fi

# Execute run.py with virtual environment python forwarding all arguments
exec "$VENV_PYTHON" "$SCRIPT_DIR/run.py" "$@"

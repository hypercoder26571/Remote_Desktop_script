#!/usr/bin/env bash
# HyprRemote Launcher Script

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Sprawdzenie obecności pythona
if ! command -v python3 &>/dev/null; then
    echo "Błąd: python3 nie jest zainstalowany."
    exit 1
fi

# Uruchomienie programu
exec python3 "$SCRIPT_DIR/remote_desktop.py" "$@"

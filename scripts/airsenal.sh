#!/usr/bin/env bash
# Run AIrsenal (github.com/alan-turing-institute/AIrsenal) as a second opinion
# beside this repo's model. This launcher never applies transfers or lineups to
# the real FPL team: make any transfers yourself in the FPL app.
#
#   scripts/airsenal.sh login     save your FPL login in AIrsenal (asked once)
#   scripts/airsenal.sh [flags]   update data, predict, and suggest transfers
#   scripts/airsenal.sh update    update AIrsenal itself
#
# Flags are passed to `airsenal run`, e.g. --free-hit-gameweek 0 to test the
# Free Hit in every gameweek. Each run is copied to reports/airsenal-latest.log.
set -euo pipefail

AIRSENAL_DIR="${AIRSENAL_DIR:-$HOME/Code/AIrsenal}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$ROOT/reports/airsenal-latest.log"

if [[ ! -d "$AIRSENAL_DIR" ]]; then
    echo "AIrsenal not found at $AIRSENAL_DIR. Install it outside iCloud with:" >&2
    echo "  git clone https://github.com/alan-turing-institute/AIrsenal.git ~/Code/AIrsenal && (cd ~/Code/AIrsenal && uv sync)" >&2
    exit 1
fi

airsenal() { (cd "$AIRSENAL_DIR" && uv run airsenal "$@"); }

# Pass the value through the environment, not argv, so it never shows up in
# shell history or the process list. AIrsenal stores each setting as a file.
save_setting() {
    (cd "$AIRSENAL_DIR" && AIRSENAL_SETTING_VALUE="$2" uv run python -c \
        'import os, sys; from airsenal.core.env import airsenal_home, save_env
save_env(sys.argv[1], os.environ["AIRSENAL_SETTING_VALUE"])
os.chmod(airsenal_home() / sys.argv[1], 0o600)' "$1")
}

for arg in "$@"; do
    if [[ "$arg" == apply || "$arg" == --apply* ]]; then
        echo "Refusing '$arg': this launcher never changes your real FPL team." >&2
        exit 2
    fi
done

case "${1:-}" in
    login)
        read -rp "FPL login email: " email
        read -rsp "FPL password (hidden): " password
        echo
        save_setting FPL_LOGIN "$email"
        save_setting FPL_PASSWORD "$password"
        unset password
        airsenal env get
        ;;
    update)
        git -C "$AIRSENAL_DIR" pull --ff-only
        (cd "$AIRSENAL_DIR" && uv sync)
        ;;
    *)
        airsenal run "$@" 2>&1 | tee "$LOG"
        ;;
esac

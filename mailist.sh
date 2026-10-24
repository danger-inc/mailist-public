#!/usr/bin/env bash
[ "${1:-}" = --quit ] || { [ -x "$HOME/.local/bin/linguist" ] && "$HOME/.local/bin/linguist" --daemon >/dev/null 2>&1 & }   # linguist (Übersetzer) läuft immer mit
# Starter für mailist. Läuft mailist schon (auch unsichtbar im Hintergrund), geht der Aufruf direkt per D-Bus
# hin – ohne Python-/GTK-Start, das Fenster ist sofort da. Sonst wird mailist gestartet.
#   mailist  ·  mailist --toggle (Super+G)  ·  mailist mailto:…  ·  mailist --daemon  ·  mailist --quit
APP=de.invincible.Mailist
OBJ=/de/invincible/Mailist
MAIN="$HOME/.local/share/mailist/mailist.py"
LOG="${XDG_RUNTIME_DIR:-/tmp}/mailist.log"

running() { gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
    --method org.freedesktop.DBus.NameHasOwner "$APP" 2>/dev/null | grep -q true; }
action() { gdbus call --session --timeout 3 --dest "$APP" --object-path "$OBJ" \
    --method org.gtk.Actions.Activate "$1" "${2:-[]}" '{}' >/dev/null 2>&1; }
start() { setsid -f python3 "$MAIN" "$@" >>"$LOG" 2>&1 </dev/null; }
quote() { local s=${1//\\/\\\\}; s=${s//\'/\\\'}; printf "'%s'" "$s"; }

# Absicherung wie bei scourge: antwortet die Hintergrund-Instanz nicht (hängt), wird sie neu gestartet.
restart() {
    local pid
    pid=$(gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
        --method org.freedesktop.DBus.GetConnectionUnixProcessID "$APP" 2>/dev/null | grep -o '[0-9]\+' | tail -1)
    if [ -n "$pid" ]; then
        kill "$pid" 2>/dev/null
        for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
        kill -9 "$pid" 2>/dev/null
    fi
    start "$@"
}

case "${1:-}" in
    --daemon) running || start --daemon; exit 0 ;;
    --quit)   running && action quit; exit 0 ;;
    --check)  exec python3 "$MAIN" --check ;;
    --toggle) running && { action toggle || restart --toggle; exit 0; }; start --toggle; exit 0 ;;
    --compose|mailto:*)
        uri=""; [[ ${1:-} == mailto:* ]] && uri=$1; [[ ${2:-} == mailto:* ]] && uri=$2
        running && { action compose "[<$(quote "$uri")>]" || restart --compose $uri; exit 0; }
        start --compose $uri; exit 0 ;;
    "") running && { action show || restart; exit 0; }; start; exit 0 ;;
    *)  exec python3 "$MAIN" "$@" ;;
esac

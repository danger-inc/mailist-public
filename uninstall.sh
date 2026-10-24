#!/usr/bin/env bash
# mailist entfernen. In Gmail selbst bleibt alles; gelöscht werden Programm, Zwischenspeicher und Anmeldung.
set -euo pipefail
RICE="$HOME/.local/share/rice-apps"
"$HOME/.local/bin/mailist" --quit 2>/dev/null || true
pkill -u "$(id -u)" -f "share/mailist/mailist.py" 2>/dev/null || true
rm -rf "$HOME/.local/share/mailist" "$HOME/.cache/mailist"
rm -f "$HOME/.local/bin/mailist" "$HOME/.local/share/applications/mailist.desktop" \
      "$HOME/.local/share/icons/hicolor/scalable/apps/mailist.svg"
if [ -d "$HOME/.config/mailist" ]; then
    read -rp "Auch Einstellungen und Google-Anmeldung (~/.config/mailist) löschen? [j/N] " a
    [[ "${a,,}" =~ ^j ]] && rm -rf "$HOME/.config/mailist"
fi
# gemeinsame Bausteine nur entfernen, wenn keine andere Suite-App sie noch braucht
if [ ! -d "$RICE/calendar" ] && [ ! -d "$RICE/music" ] && [ ! -d "$HOME/.local/share/wallist" ] && [ ! -d "$HOME/.local/share/sharist" ] && [ ! -d "$HOME/.local/share/linguist" ]; then
    rm -rf "$RICE/fonts" "$RICE/tui.py" "$RICE/glass.py" "$RICE/changelog.py" "$RICE/swipe.py" "$RICE/__pycache__"
    rmdir "$RICE" 2>/dev/null || true
fi
echo "mailist entfernt. Deine Mails liegen weiter in Gmail."

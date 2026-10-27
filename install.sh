#!/usr/bin/env bash
# mailist installieren (ins eigene Benutzerverzeichnis, root nur für Systempakete).
#   ./install.sh            normal (fragt nach Systempaketen)
#   ./install.sh --update   ohne Rückfragen (von update.sh)
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SRC/pkg.sh"
DIR="$HOME/.local/share/mailist"
RICE="$HOME/.local/share/rice-apps"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
UPDATE=0; [[ "${1:-}" == "--update" ]] && UPDATE=1

say "== mailist installieren =="
basics_missing() {
    python3 -c 'import gi, sqlite3; gi.require_version("Gtk", "4.0")' 2>/dev/null && return 1
    return 0
}
if { (( UPDATE )) && basics_missing && say "Es fehlen Systempakete – werden nachinstalliert …"; } \
   || ask "Systempakete installieren (GTK4, PyGObject, Schlüsselbund, gpg)?"; then
    pkg_install \
        "gtk4 python python-gobject python-cairo adwaita-icon-theme xdg-utils libnotify libsecret gnupg" \
        "python3 python3-gi python3-gi-cairo gir1.2-gtk-4.0 adwaita-icon-theme xdg-utils libglib2.0-bin libnotify-bin gir1.2-secret-1 gnupg" \
        "python3 python3-gobject python3-cairo gtk4 adwaita-icon-theme xdg-utils glib2 libnotify libsecret gnupg2" \
        "python3 python3-gobject python3-gobject-cairo python3-gobject-Gdk typelib-1_0-Gtk-4_0 adwaita-icon-theme xdg-utils glib2-tools libnotify-tools typelib-1_0-Secret-1 gpg2" \
        || warn "Paketinstallation nicht vollständig – weiter."
fi
echo

say "Kopiere mailist nach $DIR …"
mkdir -p "$DIR/logo" "$RICE/fonts" "$BIN" "$APPS" "$ICONS"
cp -f "$SRC"/app/mailist/*.py "$SRC"/app/mailist/VERSION "$SRC"/app/mailist/CHANGELOG.md "$DIR/"
install -m 755 "$SRC/mailist.sh" "$DIR/mailist.sh"
cp -f "$SRC/mailist.svg" "$DIR/logo/mailist.svg"
cp -f "$SRC/app/tui.py" "$SRC/app/glass.py" "$SRC/app/istheme.py" "$SRC/app/ist-swipe.py" "$SRC/app/changelog.py" "$SRC/app/swipe.py" "$RICE/"   # gemeinsame Bausteine
cp -f "$SRC"/app/fonts/* "$RICE/fonts/"                           # Symbolschrift (Icons ohne Nerd Font)
ln -sf "$DIR/mailist.sh" "$BIN/mailist"
sed "s|@EXEC@|$BIN/mailist|g" "$SRC/mailist.desktop.in" > "$APPS/mailist.desktop"
cp -f "$SRC/mailist.svg" "$ICONS/mailist.svg"
refresh_desktop

if (( ! UPDATE )); then
    echo
    if ask "mailist für mailto:-Links (E-Mail-Adressen auf Webseiten) verwenden?" n; then
        xdg-mime default mailist.desktop x-scheme-handler/mailto && echo "  mailto: → mailist"
    fi
fi
# läuft mailist schon im Hintergrund? → neu starten, damit der neue Code gilt
if gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
     --method org.freedesktop.DBus.NameHasOwner de.invincible.Mailist 2>/dev/null | grep -q true; then
    "$BIN/mailist" --quit; sleep 1; "$BIN/mailist" --daemon
fi

echo
say "Prüfe …"
check_gtk || warn "Ohne passendes GTK startet mailist nicht – siehe README."
python3 "$DIR/mailist.py" --check || true
echo
path_hint
say "Fertig!  Starten mit:  mailist   (oder über das Anwendungsmenü)"
echo "Beim ersten Start führt mailist durch die Google-Anmeldung (Seite „konto“)."
echo "Tipp: Tastenkürzel auf  mailist --toggle  legen, Autostart:  mailist --daemon  (meldet neue Mails)."
echo "Aktualisieren: ./update.sh   Entfernen: ./uninstall.sh"

#!/usr/bin/env python3
# mailist – schlankes Gmail-Programm im Stil der Suite (filist, datist, notist, scourge).
# Aufbau wie ein Explorer: links Ordner, Mitte Liste, rechts Vorschau. Läuft im Hintergrund weiter
# (gleicht ab, meldet neue Mails), das Fenster ist dann sofort da.
#   mailist                  Fenster zeigen          mailist --toggle    zeigen/verstecken (Super+G)
#   mailist --daemon         nur Hintergrund         mailist --quit      wirklich beenden
#   mailist mailto:…         neue Mail               mailist --compose   neue Mail
#   mailist --check          Abhängigkeiten prüfen
import json, os, sys, time

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(1, os.path.expanduser("~/.local/share/rice-apps"))   # tui.py/glass.py/swipe.py der Suite

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk

APP_ID = os.environ.get("MAILIST_APP_ID", "de.invincible.Mailist")
CONFIG = os.path.join(GLib.get_user_config_dir(), "mailist")
PREFS = os.path.join(CONFIG, "prefs.json")
try:
    VERSION = open(os.path.join(HERE, "VERSION")).read().strip()
except OSError:
    VERSION = "?"

ML_CSS = """
window.ml label { font-size: 13px; }
.ml-nav { padding: 0 14px 6px 14px; }
button.ml-navbtn { font-size: 16px; padding: 0 6px; min-width: 22px; }
button.ml-navbtn:disabled { color: alpha(@foreground, 0.25); background: none; }
.ml-crumbs { border: 1px solid alpha(@primary, 0.35); border-radius: 4px; padding: 0 8px; min-height: 24px; }
window.ml .ml-crumb { color: alpha(@foreground, 0.75); }
window.ml .ml-crumb.last { color: @primary; }
window.ml .ml-sep { color: alpha(@foreground, 0.3); }
button.ml-compose { background: @primary; color: @on_primary; border: none; box-shadow: none; border-radius: 4px;
    padding: 1px 10px; min-height: 0; }
button.ml-compose:hover { background: alpha(@primary, 0.85); }
window.ml button.ml-compose label { color: @on_primary; }
.ml-body { margin: 0 14px; }
.ml-body > separator, .ml-split > separator { background: none; min-width: 8px; }
.ml-split > separator:hover { background-color: alpha(@primary, 0.25); border-radius: 4px; }
.ml-panebody { padding: 2px 4px 4px 4px; }
/* Seitenleiste */
window.ml .ml-place-head { color: @secondary; font-size: 11px; margin: 8px 6px 2px 6px; }
button.ml-place { background: none; border: none; box-shadow: none; border-radius: 4px; padding: 2px 6px; min-height: 0;
    color: alpha(@foreground, 0.85); }
button.ml-place:hover { background-color: alpha(@primary, 0.12); color: @foreground; }
button.ml-place.active { background-color: alpha(@primary, 0.22); color: @foreground; }
button.ml-place.drop { background-color: alpha(@primary, 0.3); }
window.ml .pglyph { color: @primary; font-size: 15px; min-width: 18px; }
window.ml .ml-count { color: @on_primary; background-color: @primary; border-radius: 8px; padding: 0 6px;
    font-size: 11px; }
window.ml .ml-count.soft { color: alpha(@foreground, 0.6); background: none; }
window.ml .ml-sub { margin-left: 14px; }
window.ml .ml-sidefoot { color: alpha(@foreground, 0.45); font-size: 11px; margin: 10px 6px 2px 6px; }
/* Liste */
columnview.ml-list { background: none; }
columnview.ml-list > header { background: none; border-bottom: 1px dashed alpha(@primary, 0.25); }
columnview.ml-list > header > button { background: none; border: none; box-shadow: none; padding: 2px 6px;
    color: @secondary; font-size: 12px; font-weight: bold; }
columnview.ml-list > header > button:hover { color: @foreground; }
columnview.ml-list > header > button sort-indicator { color: @primary; min-width: 12px; }
columnview.ml-list > listview { background: none; }
columnview.ml-list > listview > row { border-radius: 4px; padding: 0; margin: 0 2px; min-height: 26px; outline: none;
    color: @foreground; }
columnview.ml-list > listview > row:hover { background-color: alpha(@primary, 0.08); }
columnview.ml-list > listview > row:selected { background-color: alpha(@primary, 0.24); color: @foreground; }
columnview.ml-list > listview > row:selected:focus-within { background-color: alpha(@primary, 0.3); }
columnview.ml-list > listview > row:active { background-color: alpha(@primary, 0.24); }
columnview.ml-list > listview > row > cell { padding: 2px 6px; }
window.ml .ml-from { color: alpha(@foreground, 0.8); font-weight: normal; }
window.ml .ml-subj { color: alpha(@foreground, 0.8); font-weight: normal; }
window.ml .unread .ml-from, window.ml .unread .ml-subj { color: @foreground; font-weight: bold; }
window.ml .ml-date, window.ml .ml-size { color: @secondary; font-size: 12px; font-weight: normal; }
window.ml .unread .ml-date { color: @primary; font-weight: bold; }
window.ml .ml-dot { color: @primary; font-size: 10px; }
window.ml .ml-star { color: @tertiary; font-size: 13px; }
window.ml .ml-flag { color: alpha(@foreground, 0.5); font-size: 13px; }
window.ml .ml-flag.mittel { color: @tertiary; }
window.ml .ml-flag.hoch { color: @background; background-color: @tertiary; border-radius: 3px; padding: 0 2px; }
button.ml-rowact { background: none; border: none; box-shadow: none; padding: 0 4px; min-height: 0; min-width: 0;
    color: alpha(@foreground, 0.0); border-radius: 4px; }
columnview.ml-list > listview > row:hover button.ml-rowact { color: alpha(@foreground, 0.55); }
columnview.ml-list > listview > row:hover button.ml-rowact:hover { color: @background; background-color: @tertiary; }
window.ml .ml-listfoot { color: alpha(@foreground, 0.5); font-size: 11px; padding: 4px 6px 0 6px; }
button.ml-link { background: none; border: none; box-shadow: none; padding: 0 4px; min-height: 0; color: @primary;
    border-radius: 4px; }
button.ml-link:hover { background-color: alpha(@primary, 0.16); }
window.ml .ml-empty { color: @secondary; font-size: 14px; }
window.ml .ml-bigglyph { font-size: 54px; color: alpha(@primary, 0.6); }
/* Vorschau */
window.ml .ml-subject { font-size: 17px; color: @foreground; }
window.ml .ml-addr { color: @secondary; font-weight: normal; }
window.ml .ml-meta { color: @secondary; font-size: 12px; font-weight: normal; }
window.ml .ml-avatar { background-color: alpha(@primary, 0.22); color: @primary; border-radius: 6px; min-width: 34px;
    min-height: 34px; font-size: 15px; }
.ml-tools { padding: 4px 0; border-bottom: 1px dashed alpha(@primary, 0.22); margin-bottom: 4px; }
button.ml-tool { background: none; border: none; box-shadow: none; padding: 1px 6px; min-height: 0; min-width: 0;
    color: @primary; border-radius: 4px; font-size: 15px; }
button.ml-tool:hover { background-color: alpha(@primary, 0.18); }
button.ml-tool.danger { color: @tertiary; }
button.ml-tool.danger:hover { background-color: @tertiary; color: @background; }
button.ml-tool.on { color: @tertiary; }
window.ml button.ml-tool label { font-size: 15px; }
window.ml button.ml-tool .ml-toolname { font-size: 12px; }
.ml-guard { border-radius: 6px; padding: 4px 8px; border: 1px solid alpha(@primary, 0.25); }
.ml-guard.mittel { border-color: alpha(@tertiary, 0.6); }
.ml-guard.hoch { border-color: @tertiary; background-color: alpha(@tertiary, 0.12); }
window.ml .ml-guard .ml-gl { color: @primary; }
window.ml .ml-guard.mittel .ml-gl, window.ml .ml-guard.hoch .ml-gl { color: @tertiary; }
window.ml .ml-reason { font-size: 12px; font-weight: normal; color: alpha(@foreground, 0.85); }
window.ml .ml-reason.hoch { color: @tertiary; font-weight: bold; }
.ml-osint { border-radius: 6px; padding: 3px 6px; border: 1px dashed alpha(@primary, 0.4); }
window.ml .ml-osint-title { color: @secondary; font-size: 11px; }
button.ml-chip { background: none; box-shadow: none; border: 1px solid alpha(@primary, 0.4); border-radius: 4px;
    padding: 1px 8px; min-height: 0; color: @foreground; }
button.ml-chip:hover { background-color: @primary; border-color: @primary; color: @on_primary; }
window.ml button.ml-chip:hover label { color: @on_primary; }
window.ml button.ml-chip .ml-meta { font-size: 11px; }
button.ml-chip.warn { border-color: @tertiary; }
textview.ml-text, textview.ml-text text { background: none; color: @foreground; }
textview.ml-text { font-size: 13px; }
window.ml textview.ml-text { font-weight: normal; }
.ml-bulk { padding: 20px 6px; }
window.ml .ml-bulkcount { font-size: 26px; color: @primary; }
button.ml-bigbtn { background: none; border: 1px solid alpha(@primary, 0.4); box-shadow: none; border-radius: 6px;
    padding: 8px 12px; min-height: 0; color: @foreground; }
button.ml-bigbtn:hover { background-color: @primary; border-color: @primary; }
window.ml button.ml-bigbtn:hover label { color: @on_primary; }
window.ml button.ml-bigbtn .ml-bigglyph2 { font-size: 22px; color: @primary; }
button.ml-bigbtn.danger { border-color: alpha(@tertiary, 0.5); }
window.ml button.ml-bigbtn.danger .ml-bigglyph2 { color: @tertiary; }
button.ml-bigbtn.danger:hover { background-color: @tertiary; border-color: @tertiary; }
window.ml button.ml-bigbtn.danger:hover label { color: @background; }
button.ml-bigbtn.armed { background-color: @tertiary; border-color: @tertiary; }
window.ml button.ml-bigbtn.armed label { color: @background; }
/* Aufräumen */
.ml-srow { padding: 3px 4px; border-radius: 4px; }
.ml-srow:hover { background-color: alpha(@primary, 0.07); }
.ml-srow + .ml-srow { border-top: 1px dashed alpha(@primary, 0.14); }
window.ml .ml-sname { color: @foreground; }
window.ml .ml-snum { color: @primary; min-width: 52px; }
window.ml .ml-bar { background-color: alpha(@primary, 0.55); border-radius: 2px; min-height: 4px; }
button.ml-del { background: none; border: 1px solid alpha(@tertiary, 0.5); box-shadow: none; border-radius: 4px;
    padding: 0 8px; min-height: 0; color: @tertiary; }
button.ml-del:hover, button.ml-del.armed { background-color: @tertiary; color: @background; border-color: @tertiary; }
window.ml button.ml-del:hover label, window.ml button.ml-del.armed label { color: @background; }
/* Konto */
window.ml .ml-step { color: @primary; font-size: 15px; }
window.ml .ml-step.done { color: alpha(@foreground, 0.5); }
window.ml .ml-help { font-weight: normal; color: alpha(@foreground, 0.85); }
/* Menü */
popover.ml-menu > contents { background-color: alpha(@background, 0.95); border: 1px solid @primary; border-radius: 6px;
    padding: 4px; box-shadow: 0 8px 22px rgba(0,0,0,0.3); }
popover.ml-menu button.opt { background: none; border: none; box-shadow: none; padding: 3px 10px; border-radius: 4px;
    color: @foreground; min-height: 0; }
popover.ml-menu button.opt:hover { background-color: @primary; color: @on_primary; }
window.ml popover.ml-menu button.opt:hover label, popover.ml-menu button.opt:hover label { color: @on_primary; }
popover.ml-menu button.opt.danger label { color: @tertiary; }
popover.ml-menu button.opt.danger:hover { background-color: @tertiary; }
popover.ml-menu button.opt.danger:hover label { color: @background; }
popover.ml-menu separator { background-color: alpha(@primary, 0.25); margin: 3px 4px; min-height: 1px; }
.ml-quick { padding: 2px 2px 4px 2px; }
popover.ml-menu button.ml-qbtn { background: none; border: 1px solid alpha(@primary, 0.35); box-shadow: none;
    border-radius: 6px; padding: 7px 0; min-height: 0; }
popover.ml-menu button.ml-qbtn label { font-size: 20px; color: @primary; }
popover.ml-menu button.ml-qbtn:hover { background-color: @primary; border-color: @primary; }
popover.ml-menu button.ml-qbtn:hover label { color: @on_primary; }
popover.ml-menu button.ml-qbtn.danger { border-color: alpha(@tertiary, 0.45); }
popover.ml-menu button.ml-qbtn.danger label { color: @tertiary; }
popover.ml-menu button.ml-qbtn.danger:hover { background-color: @tertiary; border-color: @tertiary; }
popover.ml-menu button.ml-qbtn.danger:hover label { color: @background; }
button.ml-tabx { background: none; border: none; box-shadow: none; padding: 0 3px; min-height: 0; min-width: 0;
    color: alpha(@foreground, 0.4); font-size: 11px; border-radius: 4px; }
button.ml-tabx:hover { background-color: @tertiary; color: @background; }
.statusbar button.ml-undo { padding: 0 8px; }
/* Schreiben */
window.ml-compose textview.ml-edit, window.ml-compose textview.ml-edit text { background: none; color: @foreground; }
window.ml-compose textview.ml-edit { font-weight: normal; font-size: 13px; }
window.ml-compose .ml-field { color: @secondary; min-width: 58px; }
.ml-attrow { padding: 2px 0; }
window.ml-dialog { background-color: @background; }
window.ml-dialog.glass { background-color: alpha(@background, 0.75); }
"""


def load_prefs():
    try:
        with open(PREFS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


class Mailist(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.prefs = load_prefs()
        self.win = None
        self.daemon = False
        self.intent = None           # ("compose", mailto) | ("toggle",) beim ersten Start
        self._held = False
        self.store = None
        self.syncer = None
        self._timer = None
        self.notified = set()
        self.state = "idle"          # idle | busy | offline | expired | noauth

    # ---------------- Einstellungen
    def save_prefs(self):
        try:
            os.makedirs(CONFIG, exist_ok=True)
            tmp = PREFS + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.prefs, f, indent=1)
            os.replace(tmp, PREFS)
        except OSError:
            pass

    def pref(self, key, default=None):
        return self.prefs.get(key, default)

    def set_pref(self, key, value):
        self.prefs[key] = value
        self.save_prefs()

    # ---------------- Start
    def do_startup(self):
        Gtk.Application.do_startup(self)
        import tui, swipe, store
        tui.CSS = tui.CSS + ML_CSS + swipe.CSS
        tui.Theme.get()
        self.store = store.Store()
        self.syncer = store.Syncer(self.store, self._event, limit=int(self.pref("sync_limit", 2000)))
        for name, ptype, cb in (("show", None, lambda *_: self.show()),
                                ("toggle", None, lambda *_: self.toggle()),
                                ("quit", None, lambda *_: self.really_quit()),
                                ("sync", None, lambda *_: self.sync_now()),
                                ("compose", "s", lambda a, p: self.compose(p.get_string())),
                                ("open-mail", "s", lambda a, p: self.open_mail(p.get_string()))):
            act = Gio.SimpleAction.new(name, GLib.VariantType.new(ptype) if ptype else None)
            act.connect("activate", cb)
            self.add_action(act)
        self.set_accels_for_action("app.compose('')", ["<Control>n"])
        self._schedule()
        try:
            Gio.NetworkMonitor.get_default().connect("network-changed", self._net_changed)
        except Exception:
            pass
        GLib.timeout_add(1500 if self.daemon else 300, lambda: (self.sync_now(), False)[1])

    def _schedule(self):
        if self._timer:
            GLib.source_remove(self._timer)
        mins = max(1, int(self.pref("interval_min", 2)))
        self._timer = GLib.timeout_add_seconds(mins * 60, lambda: (self.sync_now(quiet=True), True)[1])

    def _net_changed(self, mon, available):
        if available:
            GLib.timeout_add_seconds(3, lambda: (self.sync_now(quiet=True), False)[1])

    def do_activate(self):
        self.show()

    def do_open(self, files, n, hint):
        for f in files:
            uri = f.get_uri()
            if uri.startswith("mailto:"):
                self.compose(uri)
            elif f.get_path() and f.get_path().lower().endswith(".eml"):
                self.show()
                self.win.open_eml(f.get_path())

    def keep_running(self):
        if not self._held:
            self._held = True
            self.hold()

    def build(self):
        if self.win is None:
            import window
            self.win = window.MailWindow(self)
            self.keep_running()
        return self.win

    def show(self):
        win = self.build()
        getattr(__import__("glass"), "bring_here", lambda w: None)(win)
        win.present()

    def toggle(self):
        win = self.build()
        if win.get_visible() and win.is_active():
            win.set_visible(False)
        else:
            self.show()

    def really_quit(self):
        if self.win:
            self.win.save_state()
        self.quit()

    def compose(self, mailto=""):
        import compose
        self.build()
        compose.ComposeWindow(self, mailto=mailto or None).present()

    def open_mail(self, mid):
        self.show()
        self.win.reveal(mid)

    # ---------------- Abgleich
    def sync_now(self, quiet=False):
        import gmail
        if not gmail.logged_in():
            self._set_state("noauth")
            return
        if gmail.expired():
            self._set_state("expired")
            return
        self.syncer.request("sync")

    def _event(self, kind, data):
        GLib.idle_add(lambda: (self._on_event(kind, data), False)[1])

    def _set_state(self, st, msg=None):
        self.state = st
        if self.win:
            self.win.sync_state(st, msg)

    def _on_event(self, kind, data):
        w = self.win
        if kind == "busy":
            self._set_state("busy", data)
        elif kind == "progress":
            self._set_state("busy", f"lade {data[0]}/{data[1]}")
            if w and data[0] % 240 == 0 or (w and data[0] == data[1]):
                w.refresh(soft=True)
        elif kind == "synced":
            self._set_state("idle")
            if w:
                w.refresh(soft=True)
            self.notify_new(data or [])
        elif kind == "idle":
            if self.state == "busy":
                self._set_state("idle")
            if w and data in ("more", "flush"):
                w.refresh(soft=True)
        elif kind == "offline":
            self._set_state("offline", str(data))
        elif kind == "expired":
            self._set_state("expired")
        elif kind == "auth":
            self._set_state("noauth", data)
            if w:
                w.say(f"anmeldung: {data}", error=True)
        elif kind == "api":
            self._set_state("idle")
            if w:
                w.api_error(data)
        elif kind == "errors":
            if w:
                w.say(" · ".join(data[:2]), error=True)
        elif kind == "error":
            self._set_state("idle")
            if w:
                w.say(f"abgleich: {data}", error=True)
        elif kind == "sent":
            if w:
                w.say(f"gesendet: {data or '(ohne betreff)'}")
        elif kind == "more":
            if w:
                w.more_done(*data)
        elif kind == "search":
            if w:
                w.search_done(*data)

    def notify_new(self, ids):
        """Neue Mails melden (nicht, wenn das Fenster gerade vorn ist; nur Posteingang ohne Werbung)."""
        if not ids or not self.pref("notify", True):
            return
        if self.win and self.win.is_active():
            return
        rows = [r for r in (self.store.message(i) for i in ids if i not in self.notified) if r]
        self.notified.update(ids)
        if not rows:
            return
        if len(rows) > 3:
            n = Gio.Notification.new(f"{len(rows)} neue Mails")
            n.set_body(", ".join(dict.fromkeys(r["from_name"] or r["from_addr"] for r in rows[:6])))
            n.set_default_action("app.show")
            self.send_notification("mailist-new", n)
            return
        for r in rows:
            n = Gio.Notification.new(r["from_name"] or r["from_addr"] or "neue Mail")
            n.set_body(r["subject"] or "(ohne betreff)")
            n.set_default_action_and_target("app.open-mail", GLib.Variant("s", r["id"]))
            n.set_icon(Gio.ThemedIcon.new("mailist"))
            self.send_notification("mailist-" + r["id"], n)


def check():
    ok = True
    for mod in ("gi", "sqlite3", "email"):
        try:
            __import__(mod)
            print(f"  {mod}: ok")
        except ImportError:
            print(f"  {mod}: FEHLT")
            ok = False
    try:
        gi.require_version("Gtk", "4.0")
        print(f"  GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}: ok")
    except ValueError:
        print("  GTK 4: FEHLT")
        ok = False
    import gmail
    print("  OAuth-Client:", {"eigen": "ok", "datist": "ok (von datist)"}.get(gmail.client_source(), "fehlt – in der App einrichten"))
    print("  Anmeldung:", "ok" if gmail.logged_in() and not gmail.expired() else "nein")
    return ok


def main():
    args = sys.argv[1:]
    if "--check" in args:
        sys.exit(0 if check() else 1)
    app = Mailist()
    if "--quit" in args:
        app.register(None)
        if app.get_is_remote():
            app.activate_action("quit", None)
        return 0
    if "--daemon" in args:
        app.daemon = True
        app.register(None)
        if app.get_is_remote():
            return 0
        app.keep_running()
        GLib.idle_add(lambda: (app.build(), False)[1])     # Fenster vorbauen → erstes Zeigen sofort
        return app.run([sys.argv[0]])
    mailto = next((a for a in args if a.startswith("mailto:")), None)
    if "--compose" in args or mailto:
        app.register(None)
        if app.get_is_remote():
            app.activate_action("compose", GLib.Variant("s", mailto or ""))
            return 0
        GLib.idle_add(lambda: (app.compose(mailto or ""), False)[1])
        app.keep_running()
        return app.run([sys.argv[0]])
    if "--toggle" in args:
        app.register(None)
        if app.get_is_remote():
            app.activate_action("toggle", None)
            return 0
    files = [a for a in args if not a.startswith("-")]
    return app.run([sys.argv[0]] + files)


if __name__ == "__main__":
    sys.exit(main() or 0)

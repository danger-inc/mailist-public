# Verbindung zwischen linguist und den anderen Apps der Suite (notist, mailist, …). Kleine Kopie in jeder App,
# ohne Abhängigkeit: Ist linguist nicht installiert oder der Hover-Übersetzer in dessen Einstellungen aus
# (Standard), ist hover_enabled() falsch und der „übersetzer“-Knopf der App tut wie immer sein Ding.
#   hover_enabled()      Einstellung „Hover-Übersetzer“ (linguist → einstellungen) ist an
#   hover_active()       … und gerade eingeschaltet (der Knopf in den Apps zeigt/schaltet diesen Zustand)
#   set_hover_active(b)  Zustand ändern (state.json; linguist und alle Apps merken es sofort)
#   popup(text)          linguist unten rechts aufpoppen lassen, gleich mit Text
#   watch(callback)      callback(), sobald sich Einstellung oder Zustand ändern; Rückgabe festhalten
import json, os, shutil, subprocess

CFG_DIR = os.path.expanduser("~/.config/linguist")
CFG_FILE = os.path.join(CFG_DIR, "config.json")      # nur linguist schreibt hier (Einstellungen)
STATE_FILE = os.path.join(CFG_DIR, "state.json")     # Apps und linguist: {"hover_on": true}


def _load(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def command():
    """Pfad des linguist-Starters oder None."""
    p = shutil.which("linguist") or os.path.expanduser("~/.local/bin/linguist")
    return p if os.access(p, os.X_OK) else None


def hover_enabled():
    return bool(_load(CFG_FILE).get("hover")) and command() is not None


def hover_active():
    return hover_enabled() and bool(_load(STATE_FILE).get("hover_on", True))


def launch(*args):
    cmd = command()
    if not cmd:
        return False
    try:
        subprocess.Popen([cmd, *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False


def set_hover_active(on):
    state = _load(STATE_FILE)
    state["hover_on"] = bool(on)
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except OSError:
        return
    if on and hover_enabled():
        launch("--daemon")            # läuft schon → nichts passiert


def popup(text=None):
    text = (text or "").strip()
    return launch("--popup", text[:500]) if text else launch("--popup")


def watch(callback):
    """Ruft callback() (im GTK-Hauptthread), wenn config.json/state.json sich ändern. Monitor festhalten!"""
    try:
        from gi.repository import Gio, GLib
        if not os.path.isdir(CFG_DIR):
            return None
        mon = Gio.File.new_for_path(CFG_DIR).monitor_directory(Gio.FileMonitorFlags.NONE, None)
        pending = [0]

        def fire():
            pending[0] = 0
            callback()
            return False

        def changed(_m, f, _o, _ev):
            if f.get_basename() in ("config.json", "state.json") and not pending[0]:
                pending[0] = GLib.timeout_add(120, fire)
        mon.connect("changed", changed)
        return mon
    except Exception:  # noqa: BLE001
        return None

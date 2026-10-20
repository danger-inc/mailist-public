# Seiten und Bausteine: aufräumen (wer schickt am meisten – alles von einem Absender löschen), konto
# (Einrichtung/Anmeldung), Rechtsklick-Menü, große Knöpfe, Rückfrage-Dialog, Textfenster.
import os, threading, time

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk

import glass
import gmail
import tui

CONSOLE = "https://console.cloud.google.com"


# ====================================================================== Bausteine
class Menu:
    """Rechtsklick-/Mehr-Menü im Suite-Stil: große Knöpfe oben (quick), darunter Einträge."""

    def __init__(self, widget, x=None, y=None):
        self.pop = Gtk.Popover(has_arrow=False)
        self.pop.add_css_class("ml-menu")
        self.pop.set_parent(widget)
        if x is not None:
            r = Gdk.Rectangle()
            r.x, r.y, r.width, r.height = int(x), int(y), 1, 1
            self.pop.set_pointing_to(r)
        self.box = tui.vbox(spacing=1)
        self.pop.set_child(self.box)
        self.pop.connect("closed", lambda p: GLib.idle_add(lambda: (p.unparent(), False)[1]))

    def quick(self, items):
        row = Gtk.Box(spacing=4, homogeneous=True)
        row.add_css_class("ml-quick")
        for glyph, tip, cb, danger in items:
            b = Gtk.Button(label=glyph, tooltip_text=tip)
            b.add_css_class("ml-qbtn")
            if danger:
                b.add_css_class("danger")
            b.connect("clicked", lambda *_, cb=cb: self._run(cb))
            row.append(b)
        self.box.append(row)

    def item(self, text, cb, danger=False):
        b = Gtk.Button()
        b.add_css_class("opt")
        if danger:
            b.add_css_class("danger")
        b.set_child(tui.label(text))
        b.connect("clicked", lambda *_: self._run(cb))
        self.box.append(b)

    def sep(self):
        self.box.append(Gtk.Separator())

    def _run(self, cb):
        self.pop.popdown()
        GLib.idle_add(lambda: (cb(), False)[1])

    def popup(self):
        self.pop.popup()


class BigButton(Gtk.Button):
    """Großer Knopf mit Symbol; confirm=True: erster Klick „sicher?“, zweiter führt aus (3 s Zeit)."""

    def __init__(self, glyph, text, cb, danger=False, confirm=False):
        super().__init__(focus_on_click=False)
        self.add_css_class("ml-bigbtn")
        if danger:
            self.add_css_class("danger")
        self.text = text
        self.lbl = tui.label(text, xalign=0.5)
        self.set_child(tui.vbox(tui.label(glyph, "ml-bigglyph2", xalign=0.5), self.lbl, spacing=4))
        self.cb, self.confirm, self.armed = cb, confirm, None
        self.connect("clicked", self._clicked)

    def _clicked(self, *_):
        if self.confirm and not self.armed:
            self.add_css_class("armed")
            self.lbl.set_label("sicher? nochmal klicken")
            self.armed = GLib.timeout_add(3000, self._disarm)
            return
        self._disarm()
        self.cb()

    def _disarm(self):
        if self.armed:
            GLib.source_remove(self.armed) if self.armed is not True else None
        self.armed = None
        self.remove_css_class("armed")
        self.lbl.set_label(self.text)
        return False


def dialog_window(parent, title, width=460):
    w = Gtk.Window(transient_for=parent, modal=True, title=title, default_width=width, resizable=False)
    w.set_decorated(False)
    for c in ("tui", "ml", "ml-dialog"):
        w.add_css_class(c)
    if glass.is_on():
        w.add_css_class("glass")
    return w


def confirm(parent, title, text, ok, cb, danger=True):
    w = dialog_window(parent, title)
    panel = tui.Panel(title)
    panel.set_margin_top(10)
    panel.set_margin_bottom(12)
    panel.set_margin_start(12)
    panel.set_margin_end(12)
    panel.body.append(tui.label(text, "ml-help", wrap=True))
    yes = tui.Button(ok, lambda: (w.close(), cb()), danger=danger)
    no = tui.Button("abbrechen", w.close)
    row = tui.hbox(tui.label("", hexpand=True), no, yes, spacing=8)
    row.set_margin_top(10)
    panel.body.append(row)
    w.set_child(panel)
    keys = Gtk.EventControllerKey()
    keys.connect("key-pressed", lambda _c, kv, *_: (w.close(), True)[1] if kv == Gdk.KEY_Escape else
                 ((w.close(), cb(), True)[2] if kv in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and w.get_focus() is yes
                  else False))
    w.add_controller(keys)
    w.present()
    yes.grab_focus()
    return w


def text_window(parent, title, text):
    w = Gtk.Window(transient_for=parent, title=f"mailist · {title}", default_width=820, default_height=600)
    for c in ("pb-window", "tui", "ml"):
        w.add_css_class(c)
    tv = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR, left_margin=6, top_margin=6)
    tv.add_css_class("ml-text")
    tv.get_buffer().set_text(text)
    panel = tui.Panel(title, Gtk.ScrolledWindow(child=tv, vexpand=True))
    panel.body.set_vexpand(True)
    for m in ("top", "bottom", "start", "end"):
        getattr(panel, f"set_margin_{m}")(12)
    copy = tui.Button("kopieren", lambda: (Gdk.Display.get_default().get_clipboard().set(text), w.close()))
    panel.body.append(tui.hbox(tui.label("", hexpand=True), copy, tui.Button("schließen", w.close)))
    w.set_child(panel)
    keys = Gtk.EventControllerKey()
    keys.connect("key-pressed", lambda _c, kv, *_: (w.close(), True)[1] if kv == Gdk.KEY_Escape else False)
    w.add_controller(keys)
    w.present()


# ====================================================================== Aufräumen
class Cleanup(Gtk.Box):
    """Wie die Ordnergrößen im Explorer: wer belegt das Postfach? Pro Absender Anzahl, Größe, zuletzt –
    und ein Knopf, der alles von ihm in den Papierkorb legt (Strg+Z holt es zurück)."""
    SCOPES = [("ALLMAIL", "alle mails"), ("INBOX", "posteingang"), ("CATEGORY_PROMOTIONS", "werbung"),
              ("CATEGORY_SOCIAL", "soziale netze"), ("CATEGORY_UPDATES", "benachrichtigungen"), ("SPAM", "spam")]

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, vexpand=True, hexpand=True)
        self.win = win
        self.scope = "ALLMAIL"
        self.order = "count"
        self.only_news = False
        head = tui.hbox(spacing=8)
        head.append(tui.label("bereich", "dim"))
        self.scope_sel = tui.Select(self._scope)
        self.scope_sel.set_options(self.SCOPES, self.scope)
        head.append(self.scope_sel)
        head.append(tui.label("sortieren", "dim"))
        self.order_sel = tui.Select(self._order)
        self.order_sel.set_options([("count", "anzahl"), ("size", "größe"), ("date", "neueste"), ("unread", "ungelesen")],
                                   self.order)
        head.append(self.order_sel)
        self.news_chk = tui.Check("nur newsletter", False, self._news)
        head.append(self.news_chk)
        head.append(tui.label("", hexpand=True))
        self.summary = tui.label("", "ml-meta")
        head.append(self.summary)
        self.list = tui.vbox(spacing=0)
        sc = Gtk.ScrolledWindow(child=self.list, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        note = tui.label("Ein Klick auf „löschen“ legt alle Mails dieses Absenders (im gewählten Bereich) in den "
                         "Papierkorb – Strg+Z macht es rückgängig. Gezählt wird, was mailist zwischengespeichert hat.",
                         "ml-meta", wrap=True)
        panel = tui.Panel("aufräumen", tui.vbox(head, note, sc, spacing=8))
        panel.body.set_vexpand(True)
        panel.set_vexpand(True)
        self.append(panel)

    def _scope(self, v):
        self.scope = v
        self.scope_sel.set_options(self.SCOPES, v)
        self.refresh()

    def _order(self, v):
        self.order = v
        self.order_sel.set_options([("count", "anzahl"), ("size", "größe"), ("date", "neueste"), ("unread", "ungelesen")], v)
        self.refresh()

    def _news(self, v):
        self.only_news = v
        self.refresh()

    def refresh(self):
        from window import human_size, num, short_date
        st = self.win.st
        rows = st.senders(self.scope)
        if self.only_news:
            rows = [r for r in rows if r[6]]
        key = {"count": lambda r: (-r[2], -r[4]), "size": lambda r: -(r[3] or 0), "date": lambda r: -(r[4] or 0),
               "unread": lambda r: (-(r[5] or 0), -r[2])}[self.order]
        rows = sorted(rows, key=key)[:250]
        tui.clear(self.list)
        if not rows:
            self.list.append(tui.label("nichts zum aufräumen", "ml-empty"))
            self.summary.set_label("")
            return
        total = sum(r[2] for r in rows)
        self.summary.set_label(f"{num(len(rows))} absender · {num(total)} mails")
        top = max(r[2] for r in rows) if self.order != "size" else max((r[3] or 0) for r in rows)
        for addr, name, n, size, last, unread, news in rows:
            line = tui.hbox(spacing=10)
            line.add_css_class("ml-srow")
            line.append(tui.label(num(n), "ml-snum", xalign=1))
            who = tui.vbox(tui.label((name or addr) + ("  \U000F046B" if news else ""), "ml-sname", ellipsize=True),
                           tui.label(addr if name else "", "ml-meta", ellipsize=True), spacing=0, hexpand=True)
            bar = Gtk.Box(valign=Gtk.Align.CENTER)
            bar.add_css_class("ml-bar")
            val = n if self.order != "size" else (size or 0)
            bar.set_size_request(max(2, int(80 * val / (top or 1))), 4)
            line.append(who)
            line.append(tui.vbox(bar, tui.label(f"{human_size(size)} · {short_date(last)}"
                                                + (f" · {unread} neu" if unread else ""), "ml-meta"),
                                 spacing=2, valign=Gtk.Align.CENTER))
            show = Gtk.Button(label="zeigen", focus_on_click=False, valign=Gtk.Align.CENTER)
            show.add_css_class("ml-link")
            show.connect("clicked", lambda *_, a=addr, nm=name: self.win.go(("sender", a, nm or a)))
            line.append(show)
            if news:
                un = Gtk.Button(label="abbestellen", focus_on_click=False, valign=Gtk.Align.CENTER,
                                tooltip_text="Abmeldeseite des Absenders öffnen")
                un.add_css_class("ml-link")
                un.connect("clicked", lambda *_, a=addr: self._unsub(a))
                line.append(un)
            d = Gtk.Button(label=f"\U000F0A7A löschen", focus_on_click=False, valign=Gtk.Align.CENTER,
                           tooltip_text=f"alle {n} in den papierkorb")
            d.add_css_class("ml-del")
            d.connect("clicked", lambda b, a=addr, cnt=n: self._delete(b, a, cnt))
            line.append(d)
            self.list.append(line)

    def _delete(self, btn, addr, n):
        if not getattr(btn, "armed", False):
            btn.armed = True
            btn.add_css_class("armed")
            btn.set_label(f"sicher? {n}")
            GLib.timeout_add(3000, lambda: (btn.remove_css_class("armed"), btn.set_label("\U000F0A7A löschen"),
                                            setattr(btn, "armed", False), False)[3])
            return
        ids = self.win.st.ids_from(addr, self.scope if self.scope != "SPAM" else "SPAM")
        self.win.trash(ids)

    def _unsub(self, addr):
        import json
        st = self.win.st
        r = st.db.execute("SELECT hdr FROM messages WHERE from_addr=? AND hdr LIKE '%list-unsubscribe%' "
                          "ORDER BY date DESC LIMIT 1", (addr,)).fetchone()
        if r:
            hdr = json.loads(r[0]).get("list-unsubscribe", "")
            self.win.reader.unsubscribe(hdr)


# ====================================================================== Konto
class Account(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, vexpand=True, hexpand=True)
        self.win = win
        self.api_disabled = False
        self.login = None
        self.inner = tui.vbox(spacing=10)
        sc = Gtk.ScrolledWindow(child=self.inner, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        panel = tui.Panel("konto", sc)
        panel.body.set_vexpand(True)
        panel.set_vexpand(True)
        self.append(panel)

    def step(self, n, title, done, *body):
        box = tui.vbox(spacing=4)
        box.append(tui.label(f"{'󰄬' if done else n}  {title}", "ml-step" + (" done" if done else "")))
        inner = tui.vbox(*body, spacing=6)
        inner.set_margin_start(26)
        box.append(inner)
        self.inner.append(box)

    def refresh(self):
        from window import human_size, num
        tui.clear(self.inner)
        src = gmail.client_source()
        logged = gmail.logged_in()
        expired = gmail.expired()
        st = self.win.st
        if logged and not expired and not self.api_disabled:
            since = gmail.signed_in_at()
            when = time.strftime("%d.%m.%Y %H:%M", time.localtime(since)) if since else "?"
            left = ""
            if since:
                days = 7 - (time.time() - since) / 86400
                left = (f" · google-testmodus: noch ca. {max(0, days):.0f} tage gültig, dann einmal neu anmelden"
                        if days < 8 else "")
            self.inner.append(tui.label(f"\U000F0565  mit gmail verbunden (seit {when}){left}", "ml-step"))
            synced = st.get("synced")
            n = st.count_local("ALLMAIL") + st.count_local("TRASH") + st.count_local("SPAM")
            self.inner.append(tui.label(
                f"{num(n)} mails zwischengespeichert ({human_size(st.size_on_disk())}) · letzter abgleich "
                f"{time.strftime('%H:%M', time.localtime(int(synced))) if synced else '–'} · "
                f"warteschlange {len(st.pending())}", "ml-help", wrap=True))
            row = tui.hbox(tui.Button("jetzt abgleichen", lambda: self.win.app.sync_now()),
                           tui.Button("zwischenspeicher neu füllen", self._refill),
                           tui.Button("abmelden", self._logout, danger=True), spacing=8)
            self.inner.append(row)
            self.inner.append(tui.label(
                "Abmelden widerruft den Zugriff bei Google und löscht alle lokal gespeicherten Mails von diesem "
                "Rechner. In Gmail selbst bleibt alles, wie es ist.", "ml-meta", wrap=True))
            self._tips()
            return

        self.inner.append(tui.label(
            "mailist spricht direkt mit Gmail – über deinen eigenen Google-Zugang (wie datist). Einmal einrichten:",
            "ml-help", wrap=True))
        # 1) Client
        if src:
            body = [tui.label("von datist übernommen – derselbe Google-Cloud-Zugang" if src == "datist"
                              else "eingerichtet", "ml-meta"),
                    tui.hbox(tui.Button("andere client-datei laden", self._pick_client), spacing=8)]
        else:
            body = [tui.label(
                f"In der Google Cloud Console ein Projekt anlegen → „Google Auth Platform“ → Client erstellen, "
                f"Typ „Desktop-App“ → JSON herunterladen und hier laden. Unter „Zielgruppe“ dich selbst als "
                f"Testnutzer eintragen.", "ml-help", wrap=True),
                tui.hbox(tui.Button("client-datei laden", self._pick_client),
                         tui.Button("console öffnen", lambda: self._open(f"{CONSOLE}/auth/clients")), spacing=8)]
        self.step("1", "google-zugang (oauth-client)", bool(src), *body)
        # 2) Gmail-API
        pid = gmail.project_id()
        url = f"{CONSOLE}/apis/library/gmail.googleapis.com" + (f"?project={pid}" if pid else "")
        self.step("2", "gmail-api im projekt aktivieren", False,
                  tui.label("Einmal „Aktivieren“ klicken – sonst lehnt Google jeden Zugriff ab." +
                            (" (Google meldet gerade: noch nicht aktiviert.)" if self.api_disabled else ""),
                            "ml-help", wrap=True),
                  tui.hbox(tui.Button("gmail-api öffnen", lambda: self._open(url)),
                           tui.Button("erledigt – erneut versuchen", self._retry), spacing=8))
        # 3) Anmelden
        msg = "Die Anmeldung öffnet sich in deinem Browser. Google warnt, dass die App nicht überprüft ist – das ist " \
              "dein eigener Zugang: „Weiter“ und den Haken bei Gmail setzen."
        if expired:
            msg = "Google hat die Anmeldung beendet (im Testmodus nach 7 Tagen). Einmal neu anmelden – deine Mails " \
                  "bleiben im Zwischenspeicher."
        self.status = tui.label("", "ml-meta", wrap=True)
        btn = tui.Button("mit google anmelden" if not expired else "neu anmelden", self._login)
        btn.add_css_class("on")
        self.step("3", "anmelden", logged and not expired, tui.label(msg, "ml-help", wrap=True),
                  tui.hbox(btn, tui.Button("abbrechen", self._cancel), spacing=8), self.status)
        n = st.count_local("ALLMAIL")
        if n:
            self.inner.append(tui.label(f"Offline weiterhin lesbar: {num(n)} zwischengespeicherte Mails.", "ml-meta"))

    def _tips(self):
        tips = ("Entf = Papierkorb, Shift+Entf = endgültig, Strg+Z = rückgängig",
                "„aufräumen“ links: alle Mails eines Absenders mit einem Klick löschen",
                "Mails per Ziehen auf einen Ordner links verschieben (Papierkorb, Spam, Label)",
                "Suche: tippen filtert sofort lokal, Enter sucht in ganz Gmail (auch from: has:attachment …)",
                "Super+G zeigt/versteckt mailist; es gleicht im Hintergrund ab und meldet neue Mails")
        box = tui.vbox(spacing=2)
        box.set_margin_top(10)
        box.append(tui.label("tipps", "ml-place-head"))
        for t in tips:
            box.append(tui.label("· " + t, "ml-meta", wrap=True))
        self.inner.append(box)

    def _open(self, url):
        Gtk.UriLauncher(uri=url).launch(self.win, None, None)

    def _pick_client(self):
        dlg = Gtk.FileDialog(title="client_secret_….json wählen")
        f = Gtk.FileFilter()
        f.add_pattern("*.json")
        f.set_name("OAuth-Client (JSON)")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        dlg.set_filters(store)

        def done(d, res):
            try:
                path = d.open_finish(res).get_path()
                gmail.install_client(path)
                self.win.say("oauth-client übernommen")
            except GLib.Error:
                return
            except (OSError, ValueError) as e:
                self.win.say(str(e), error=True)
            self.refresh()
        dlg.open(self.win, None, done)

    def _retry(self):
        self.api_disabled = False
        self.win.app.sync_now()
        self.refresh()

    def _login(self):
        if self.login:
            return
        self.status.set_label("browser geöffnet – warte auf die anmeldung …")
        self.login = gmail.Login(lambda url: GLib.idle_add(
            lambda: (Gtk.UriLauncher(uri=url).launch(self.win, None, None), False)[1]))

        def work():
            try:
                self.login.run()
                res = None
            except Exception as e:
                res = e
            GLib.idle_add(lambda: (self._login_done(res), False)[1])
        threading.Thread(target=work, daemon=True).start()

    def _cancel(self):
        if self.login:
            self.login.cancelled = True

    def _login_done(self, err):
        self.login = None
        if err:
            self.status.set_label(f"✗ {err}")
            return
        self.win.say("angemeldet – hole deine mails …")
        old = self.win.st.get("me")
        self.win.st.put("history", None if not old else self.win.st.get("history"))
        self.win.app.sync_now()
        self.refresh()
        GLib.timeout_add(400, lambda: (self.win.go(("folder", "INBOX")), False)[1])

    def _refill(self):
        self.win.st.put("history", None)
        self.win.app.sync_now()
        self.win.say("zwischenspeicher wird neu gefüllt …")

    def _logout(self):
        def go():
            gmail.logout()
            self.win.st.reset()
            self.win.refresh()
            self.refresh()
            self.win.say("abgemeldet, lokale mails gelöscht")
        confirm(self.win, "abmelden?", "Der Zugriff von mailist auf Gmail wird widerrufen und alle lokal gespeicherten "
                "Mails werden von diesem Rechner gelöscht. In Gmail bleibt alles erhalten.", "abmelden", go)

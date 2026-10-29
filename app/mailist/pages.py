# Seiten und Bausteine: aufräumen (wer schickt am meisten – alles von einem Absender löschen), konto
# (Einrichtung/Anmeldung), Rechtsklick-Menü, große Knöpfe, Rückfrage-Dialog, Textfenster.
import os, threading, time

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk

import glass
import gmail
import pgp
import tui
import vault
from accounts import mask

CONSOLE = "https://console.cloud.google.com"


# ====================================================================== Bausteine

_active_menu = None


def close_on_outside(pop, owner):
    """Menü ohne GTK-Grab: Klick irgendwo anders, Scrollen, Esc oder Fensterwechsel schließen es – der Klick bzw. das
    Scrollen kommt trotzdem an (ein Grab-Popup hing sich bei einem zweiten Menü auf und fror die Eingabe ein)."""
    root = owner.get_root()
    if root is None:
        return
    t0 = GLib.get_monotonic_time()

    def close(*_):
        if GLib.get_monotonic_time() - t0 > 150_000:        # das Ereignis, das das Menü öffnete, ignorieren
            pop.popdown()
        return False
    click = Gtk.GestureClick(button=0)
    click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    click.connect("pressed", close)
    scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
    scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    scroll.connect("scroll", close)
    key = Gtk.EventControllerKey()
    key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    key.connect("key-pressed", lambda c, kv, *_: (pop.popdown(), True)[1] if kv == 0xFF1B else False)
    for c in (click, scroll, key):
        root.add_controller(c)
    hid = root.connect("notify::is-active", lambda w, p: None if w.is_active() else pop.popdown())

    def done(*_):
        root.disconnect(hid)

        def drop():                                          # nie mitten im Ereignis entfernen (Absturz)
            for c in (click, scroll, key):
                root.remove_controller(c)
            return False
        GLib.idle_add(drop)
    pop.connect("closed", done)


class Menu:
    """Rechtsklick-/Mehr-Menü im Suite-Stil: große Knöpfe oben (quick), darunter Einträge."""

    def __init__(self, widget, x=None, y=None):
        global _active_menu
        if _active_menu is not None:
            try:
                _active_menu.popdown()
            except Exception:
                pass
        self.widget = widget
        self.pop = Gtk.Popover(has_arrow=False, autohide=False)
        _active_menu = self.pop
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
        close_on_outside(self.pop, self.widget)


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
    """Seite „konten“: alle Postfächer (wechseln, hinzufügen, entfernen) und darunter das gerade gezeigte im
    Detail (Gmail-Einrichtung bzw. IMAP-Zugang)."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, vexpand=True, hexpand=True)
        self.win = win
        self.api_disabled = False
        self.login = None
        self.inner = tui.vbox(spacing=10)
        sc = Gtk.ScrolledWindow(child=self.inner, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        panel = tui.Panel("konten", sc)
        panel.body.set_vexpand(True)
        panel.set_vexpand(True)
        self.append(panel)

    @property
    def acct(self):
        return self.win.app.acct

    def step(self, n, title, done, *body):
        box = tui.vbox(spacing=4)
        box.append(tui.label(f"{'󰄬' if done else n}  {title}", "ml-step" + (" done" if done else "")))
        inner = tui.vbox(*body, spacing=6)
        inner.set_margin_start(26)
        box.append(inner)
        self.inner.append(box)

    # ---------------- Übersicht
    def _overview(self):
        app = self.win.app
        box = tui.vbox(spacing=4)
        box.append(tui.label("deine postfächer", "ml-place-head"))
        for a in app.accounts.values():
            cur = a is self.acct
            glyph = "\U000F02AB" if a.kind == "gmail" else "\U000F01EE"
            ok = a.backend.logged_in() and not a.backend.expired()
            state = "verbunden" if ok else ("anmeldung abgelaufen" if a.backend.expired() else "nicht angemeldet")
            if a.kind == "imap" and not ok:
                state = "passwort fehlt"
            row = tui.hbox(tui.label(glyph, "accent"),
                           tui.label(a.label, "ml-acctname" + (" cur" if cur else "")),
                           tui.label(a.address if a.address != a.label else "", "ml-meta",
                                     hexpand=True, ),
                           tui.label(state, "ml-meta" if ok else "ml-warn"), spacing=8)
            if not cur:
                row.append(tui.Button("zeigen", lambda aid=a.id: app.switch_account(aid)))
            else:
                row.append(tui.label("· gerade offen", "ml-meta"))
            row.append(tui.Button("entfernen", lambda acc=a: self._remove(acc), danger=True))
            box.append(row)
        add = tui.hbox(spacing=8)
        add.append(tui.Button("+ gmail-konto", self._add_gmail))
        add.append(tui.Button("+ anderes postfach (imap)", lambda: ImapDialog(self.win)))
        add.set_margin_top(4)
        box.append(add)
        box.append(tui.label("Wechseln: links oben in der Ordnerleiste oder Strg+Shift+A. Jedes Postfach hat einen "
                             "eigenen Zwischenspeicher; alle werden im Hintergrund abgeglichen und melden neue Mails.",
                             "ml-meta", wrap=True))
        self.inner.append(box)
        sep = Gtk.Separator()
        sep.add_css_class("ml-fmtsep")
        self.inner.append(sep)
        self.inner.append(tui.label(f"{self.acct.label} – {'gmail' if self.acct.kind == 'gmail' else 'imap/smtp'}",
                                    "ml-place-head"))

    def _remove(self, acc):
        what = "Der Zugriff auf Gmail wird widerrufen" if acc.kind == "gmail" else \
            "Das gespeicherte Passwort wird aus dem Schlüsselbund gelöscht"
        confirm(self.win, "postfach entfernen?",
                f"„{acc.label}“ wird aus mailist entfernt. {what} und die lokal gespeicherten Mails dieses "
                f"Postfachs werden von diesem Rechner gelöscht. Auf dem Server bleibt alles, wie es ist.",
                "entfernen", lambda: (self.win.app.remove_account(acc.id), self.refresh(),
                                      self.win.say(f"„{acc.label}“ entfernt")))

    def _add_gmail(self):
        n = sum(1 for a in self.win.app.accounts.values() if a.kind == "gmail")
        main_empty = "main" in self.win.app.accounts and not self.win.app.accounts["main"].backend.logged_in()
        if main_empty:
            self.win.app.switch_account("main")
        else:
            self.win.app.add_account({"kind": "gmail", "name": f"gmail {n + 1}" if n else "gmail"})
        self.win.go(("account",))
        self.refresh()

    # ---------------- Seite
    def refresh(self):
        tui.clear(self.inner)
        self._overview()
        if self.acct.kind == "imap":
            self._imap()
        else:
            with self.acct.backend.bound():
                self._gmail()

    def _imap(self):
        from window import human_size, num
        acc, st, c = self.acct, self.acct.store, self.acct.conf
        ok = acc.backend.logged_in()
        if ok:
            synced = st.get("synced")
            self.inner.append(tui.label(
                f"\U000F0565  {c.get('address')} · {c.get('imap_host')} ({c.get('imap_tls', 'ssl')}) · "
                f"senden über {c.get('smtp_host')}:{c.get('smtp_port')}", "ml-step", wrap=True))
            n = st.count_local("ALLMAIL") + st.count_local("TRASH") + st.count_local("SPAM")
            kr = "im schlüsselbund" if vault.in_keyring(acc.id) else "nur bis zum beenden (schlüsselbund aus)"
            self.inner.append(tui.label(
                f"{num(n)} mails zwischengespeichert ({human_size(st.size_on_disk())}) · letzter abgleich "
                f"{time.strftime('%H:%M', time.localtime(int(synced))) if synced else '–'} · "
                f"warteschlange {len(st.pending())} · passwort {kr}", "ml-help", wrap=True))
            row = tui.hbox(tui.Button("jetzt abgleichen", lambda: acc.syncer.request("sync")),
                           tui.Button("zugang bearbeiten", lambda: ImapDialog(self.win, edit=acc)),
                           tui.Button("zwischenspeicher neu füllen", self._refill_imap), spacing=8)
            self.inner.append(row)
            self._tips()
            return
        self.inner.append(tui.label(
            "Das Passwort für dieses Postfach ist nicht da (der Schlüsselbund ist aus, gesperrt, oder es wurde "
            "nie gespeichert). Einmal eingeben:", "ml-help", wrap=True))
        pw = tui.Entry("passwort", password=True)
        msg = tui.label("", "ml-meta", wrap=True)

        def go():
            p = pw.get_text()
            if not p:
                return
            msg.set_label("prüfe …")

            def work():
                import imapmail
                err = imapmail.ImapBackend.test(c, p)
                GLib.idle_add(lambda: (done(err, p), False)[1])
            threading.Thread(target=work, daemon=True).start()

        def done(err, p):
            if err:
                msg.set_label(f"✗ {err}")
                return
            vault.put(acc.id, p, f"mailist · {c.get('address')}")
            acc.state = "idle"
            self.win.app.sync_now()
            self.refresh()
            self.win.say("verbunden – hole deine mails …")
        pw.connect("activate", lambda *_: go())
        btn = tui.Button("verbinden", go)
        btn.add_css_class("on")
        self.inner.append(tui.hbox(pw, btn, tui.Button("zugang bearbeiten", lambda: ImapDialog(self.win, edit=acc)),
                                   spacing=8))
        self.inner.append(msg)
        GLib.idle_add(lambda: (pw.grab_focus(), False)[1])

    def _refill_imap(self):
        st = self.acct.store
        for (k,) in st.db.execute("SELECT k FROM kv WHERE k LIKE 'imap:f:%'").fetchall():
            st.put(k, None)
        self.acct.syncer.request("sync")
        self.win.say("zwischenspeicher wird neu gefüllt …")

    def _gmail(self):
        from window import human_size, num
        src = gmail.client_source()
        logged = gmail.logged_in()
        expired = gmail.expired()
        st = self.acct.store
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
            row = tui.hbox(tui.Button("jetzt abgleichen", lambda: self.acct.syncer.request("sync")),
                           tui.Button("zwischenspeicher neu füllen", self._refill),
                           tui.Button("abmelden", self._logout, danger=True), spacing=8)
            self.inner.append(row)
            self.inner.append(tui.label(
                "Abmelden widerruft den Zugriff bei Google und löscht alle lokal gespeicherten Mails dieses Kontos "
                "von diesem Rechner. In Gmail selbst bleibt alles, wie es ist.", "ml-meta", wrap=True))
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
        if self.acct.id != "main":
            msg += " Google fragt, welches Konto – nimm das, das du hier hinzufügen willst. Weitere Konten musst du " \
                   "evtl. auch als Testnutzer eintragen."
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
                "Mails per Ziehen auf einen Ordner links verschieben (Papierkorb, Spam, Label/Ordner)",
                "Suche: tippen filtert sofort lokal, Enter sucht im ganzen Postfach",
                "Super+G zeigt/versteckt mailist; es gleicht im Hintergrund ab und meldet neue Mails",
                "Strg+Shift+A wechselt zwischen deinen Postfächern")
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
        acc = self.acct
        with acc.backend.bound():
            self.login = gmail.Login(lambda url: GLib.idle_add(
                lambda: (Gtk.UriLauncher(uri=url).launch(self.win, None, None), False)[1]), choose=acc.id != "main")

        def work():
            try:
                self.login.run()
                res = None
            except Exception as e:
                res = e
            GLib.idle_add(lambda: (self._login_done(res, acc), False)[1])
        threading.Thread(target=work, daemon=True).start()

    def _cancel(self):
        if self.login:
            self.login.cancelled = True

    def _login_done(self, err, acc):
        self.login = None
        if err:
            self.status.set_label(f"✗ {err}")
            return
        self.win.say("angemeldet – hole deine mails …")
        st = acc.store
        old = st.get("me")
        st.put("history", None if not old else st.get("history"))
        if acc.id != "main" and acc.conf.get("name", "").startswith("gmail"):
            acc.conf["name"] = ""                     # Name kommt dann von der Adresse
            self.win.app.save_accounts()
        acc.syncer.request("sync")
        self.refresh()
        GLib.timeout_add(400, lambda: (self.win.go(("folder", "INBOX")), False)[1])

    def _refill(self):
        self.acct.store.put("history", None)
        self.acct.syncer.request("sync")
        self.win.say("zwischenspeicher wird neu gefüllt …")

    def _logout(self):
        acc = self.acct

        def go():
            acc.backend.logout()
            acc.store.reset()
            self.win.refresh()
            self.refresh()
            self.win.say("abgemeldet, lokale mails gelöscht")
        confirm(self.win, "abmelden?", "Der Zugriff von mailist auf Gmail wird widerrufen und alle lokal gespeicherten "
                "Mails dieses Kontos werden von diesem Rechner gelöscht. In Gmail bleibt alles erhalten.", "abmelden",
                go)


# ====================================================================== IMAP-Zugang
class ImapDialog:
    """Postfach per IMAP/SMTP hinzufügen oder bearbeiten. Bekannte Anbieter werden an der Adresse erkannt."""

    def __init__(self, win, preset=None, edit=None):
        import imapmail
        self.win, self.edit, self.preset = win, edit, preset
        self.touched = False
        base = dict(edit.conf) if edit else (dict(imapmail.PRESETS[preset]) if preset else {})
        uni = False
        title = "zugang bearbeiten" if edit else "postfach hinzufügen"
        w = self.w = dialog_window(win, title, width=520)
        panel = tui.Panel(title)
        for m in ("top", "bottom", "start", "end"):
            getattr(panel, f"set_margin_{m}")(12)
        body = panel.body
        body.set_spacing(6)
        self.addr = tui.Entry("mail-adresse", text=base.get("address", ""))
        self.pw = tui.Entry("passwort" + (" (leer = bisheriges behalten)" if edit else ""), password=True)
        self.name = tui.Entry("name in mailist (z. b. „uni“)", text=base.get("name", ""))
        self.sender = tui.Entry("absendername (optional, z. b. vor- und nachname)", text=base.get("sender_name", ""))

        def row(label, *ws):
            r = tui.hbox(tui.label(label, "ml-field"), *ws, spacing=8)
            for x in ws:
                x.set_hexpand(True)
            body.append(r)
            return r
        row("adresse", self.addr)
        row("passwort", self.pw)
        row("name", self.name)
        row("absender", self.sender)
        # Server (bei bekannten Anbietern eingeklappt)
        self.imap_host = tui.Entry("imap-server", text=base.get("imap_host", ""))
        self.imap_port = tui.Entry("993", text=str(base.get("imap_port", "") or ""))
        self.imap_tls = tui.Select(lambda v: self.imap_tls.set_options(self.imap_tls.options, v))
        self.imap_tls.set_options([("ssl", "ssl/tls"), ("starttls", "starttls"), ("none", "ohne")],
                                  base.get("imap_tls", "ssl"))
        self.smtp_host = tui.Entry("smtp-server", text=base.get("smtp_host", ""))
        self.smtp_port = tui.Entry("587", text=str(base.get("smtp_port", "") or ""))
        self.smtp_tls = tui.Select(lambda v: self.smtp_tls.set_options(self.smtp_tls.options, v))
        self.smtp_tls.set_options([("starttls", "starttls"), ("ssl", "ssl/tls"), ("none", "ohne")],
                                  base.get("smtp_tls", "starttls"))
        self.user = tui.Entry("benutzername (leer = adresse)", text=base.get("user", ""))
        self.save_sent = tui.Check("kopie gesendeter mails in „gesendet“ ablegen", base.get("save_sent", True),
                                   tooltip="Aus, wenn der Anbieter das selbst macht (sonst doppelt)")
        srv = tui.vbox(spacing=6)
        for label, *ws in (("imap", self.imap_host, self.imap_port, self.imap_tls),
                           ("smtp", self.smtp_host, self.smtp_port, self.smtp_tls), ("login", self.user)):
            r = tui.hbox(tui.label(label, "ml-field"), *ws, spacing=8)
            ws[0].set_hexpand(True)
            if len(ws) > 1:
                ws[1].set_width_chars(5)
            srv.append(r)
        srv.append(self.save_sent)
        self.exp = Gtk.Expander(label="server-einstellungen", child=srv,
                                expanded=bool(edit) or not (preset or base.get("imap_host")))
        body.append(self.exp)
        self.msg = tui.label("", "ml-meta", wrap=True)
        body.append(self.msg)
        btns = tui.hbox(spacing=8)
        btns.append(tui.label("", hexpand=True))
        btns.append(tui.Button("abbrechen", w.close))
        self.ok = tui.Button("speichern" if edit else "verbinden", self.go)
        self.ok.add_css_class("on")
        btns.append(self.ok)
        btns.set_margin_top(6)
        body.append(btns)
        w.set_child(panel)
        for e in (self.imap_host, self.smtp_host):
            e.connect("changed", lambda *_: setattr(self, "touched", True) if e.has_focus() else None)
        self.addr.connect("changed", self._guess)
        for e in (self.addr, self.pw, self.name, self.sender):
            e.connect("activate", lambda *_: self.go())
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _c, kv, *_: (w.close(), True)[1] if kv == Gdk.KEY_Escape else False)
        w.add_controller(keys)
        w.present()
        (self.pw if base.get("address") else self.addr).grab_focus()

    def _guess(self, *_):
        import imapmail
        if self.edit or self.touched or self.preset:
            return
        g = imapmail.guess(self.addr.get_text().strip())
        if not g:
            return
        self.imap_host.set_text(g["imap_host"])
        self.imap_port.set_text(str(g["imap_port"]))
        self.imap_tls.set_options(self.imap_tls.options, g["imap_tls"])
        self.smtp_tls.set_options(self.smtp_tls.options, g["smtp_tls"])
        self.smtp_host.set_text(g["smtp_host"])
        self.smtp_port.set_text(str(g["smtp_port"]))
        self.save_sent.set_active(g.get("save_sent", True))
        if not self.name.get_text():
            self.name.set_text(g["name"])
        self.msg.set_label(f"erkannt: {g['name']} – server eingetragen")

    def conf(self):
        def port(e, d):
            try:
                return int(e.get_text().strip() or d)
            except ValueError:
                return d
        addr = self.addr.get_text().strip()
        c = dict(self.edit.conf) if self.edit else {"kind": "imap"}
        c.update(address=addr, name=self.name.get_text().strip() or addr.split("@")[-1],
                 sender_name=self.sender.get_text().strip(), imap_host=self.imap_host.get_text().strip(),
                 imap_port=port(self.imap_port, 993), imap_tls=self.imap_tls.current or "ssl",
                 smtp_host=self.smtp_host.get_text().strip(), smtp_port=port(self.smtp_port, 587),
                 smtp_tls=self.smtp_tls.current or "starttls", user=self.user.get_text().strip(),
                 save_sent=self.save_sent.active)
        if self.preset:
            c["preset"] = self.preset
        return c

    def go(self):
        import imapmail
        c = self.conf()
        if "@" not in c["address"]:
            self.msg.set_label("✗ mail-adresse fehlt")
            return
        if not c["imap_host"] or not c["smtp_host"]:
            self.exp.set_expanded(True)
            self.msg.set_label("✗ server fehlen – bitte eintragen (steht meist in der hilfe deines anbieters)")
            return
        pw = self.pw.get_text() or (vault.get(self.edit.id) if self.edit else None)
        if not pw:
            self.msg.set_label("✗ passwort fehlt")
            return
        self.ok.set_sensitive(False)
        self.msg.set_label("verbinde … (imap und smtp werden geprüft)")

        def work():
            err = imapmail.ImapBackend.test(c, pw)
            GLib.idle_add(lambda: (self._done(err, c, pw), False)[1])
        threading.Thread(target=work, daemon=True).start()

    def _done(self, err, c, pw):
        self.ok.set_sensitive(True)
        if err:
            self.msg.set_label(f"✗ {err}")
            self.exp.set_expanded(True)
            return
        app = self.win.app
        if self.edit:
            self.edit.conf.clear()
            self.edit.conf.update(c)
            self.edit.backend.name = c.get("name") or c["imap_host"]
            self.edit.backend.drop_conns()
            vault.put(self.edit.id, pw, f"mailist · {c['address']}")
            app.save_accounts()
            self.edit.syncer.request("sync")
            self.win.say("zugang gespeichert")
        else:
            import accounts
            c["id"] = accounts.new_id()
            vault.put(c["id"], pw, f"mailist · {c['address']}")
            app.add_account(c)
            self.win.say(f"„{c['name']}“ verbunden – hole deine mails …")
            GLib.timeout_add(300, lambda: (self.win.go(("folder", "INBOX")), False)[1])
        self.w.close()
        self.win.account.refresh() if self.win.cur and self.win.cur.loc[0] == "account" else None


# ====================================================================== Einstellungen
class Settings(Gtk.Box):
    """Alle Schalter an einem Ort – vor allem die sicherheitsrelevanten."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, vexpand=True, hexpand=True)
        self.win = win
        self.inner = tui.vbox(spacing=4)
        sc = Gtk.ScrolledWindow(child=self.inner, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        panel = tui.Panel("einstellungen", sc)
        panel.body.set_vexpand(True)
        panel.set_vexpand(True)
        self.append(panel)
        self.key_box = None

    def section(self, title, text=None):
        h = tui.label(title, "ml-step")
        h.set_margin_top(12)
        self.inner.append(h)
        if text:
            self.inner.append(tui.label(text, "ml-meta", wrap=True))

    def check(self, key, text, default, help_=None, cb=None):
        app = self.win.app

        def changed(v):
            app.set_pref(key, v)
            c = getattr(self.win, "chk", {}).get(key)
            if c is not None:
                c.set_active(v)
            if cb:
                cb(v)
        c = tui.Check(text, app.pref(key, default), changed, tooltip=help_)
        row = tui.vbox(c, spacing=0)
        if help_:
            h = tui.label(help_, "ml-meta", wrap=True)
            h.set_margin_start(34)
            row.append(h)
        self.inner.append(row)
        return c

    def refresh(self):
        import imapmail
        w = self.win
        app = w.app
        tui.clear(self.inner)
        self.section("design")
        self.inner.append(tui.istheme.picker())
        self.section("allgemein")
        self.check("sidebar", "ordner-leiste zeigen (f9)", True, cb=lambda v: w.side_panel.set_visible(v))
        self.check("preview", "vorschau neben der liste (f3)", True, cb=lambda v: w.set_reading(False) if w.reading else w.apply_layout())
        self.check("col_size", "spalte „größe“", True, cb=lambda v: w.col_size.set_visible(v))
        self.check("notify", "neue mails melden (alle postfächer)", True)
        self.check("mark_read", "beim lesen als gelesen markieren", True)
        self.check("html", "html-mails formatiert zeigen", True, cb=lambda v: w.reader.rerender(),
                   help_="Aus: nur die Textfassung.")
        self.check("load_images", "externe bilder automatisch laden", False, cb=lambda v: w.reader.rerender(),
                   help_="Aus: Bilder aus dem Netz erst nach Klick auf „laden“ in der Mail. An: sofort – der "
                         "Absender sieht dann, dass und wann du die Mail geöffnet hast. Tracker-Pixel bleiben "
                         "immer blockiert.")

        self.section("sicherheit beim lesen")
        self.check("sec_check", "sicherheits-check jeder mail", True,
                   "SPF/DKIM/DMARC, gefälschte Absendernamen, andere Antwortadresse, getarnte Links, gefährliche "
                   "Anhänge – oben in der Vorschau.", cb=lambda v: w.reader.rerender())
        self.check("sec_link_confirm", "verdächtige links nur nach rückfrage öffnen", True)
        self.check("sec_all_links", "jeden link vor dem öffnen zeigen", False,
                   "Vor jedem Link (außer mailto:) erst das echte Ziel zeigen und bestätigen.")
        self.check("sec_risky_att", "gefährliche anhänge nur nach rückfrage öffnen", True,
                   ".exe, .js, Office mit Makros, .iso, .html … – typische Schadsoftware-Träger.")

        self.section("verbindungen & passwörter", "gilt für imap/smtp-postfächer (gmail nutzt google-anmeldung)")

        def keyring(v):
            app.apply_security()
            for a in app.accounts.values():
                if a.kind != "imap":
                    continue
                pw = vault.get(a.id)
                if v and pw:
                    vault.put(a.id, pw, f"mailist · {a.conf.get('address')}")
                elif not v:
                    vault.forget(a.id, keyring=True)
                    if pw:
                        vault._mem[a.id] = pw          # läuft bis zum Beenden weiter
            w.say("passwörter im schlüsselbund" if v else "passwörter aus dem schlüsselbund gelöscht – nach dem "
                  "nächsten start fragt mailist neu")
        self.check("sec_keyring", "passwörter im schlüsselbund speichern", True,
                   "An: im System-Schlüsselbund (verschlüsselt, mit deiner Anmeldung entsperrt). Aus: nur im "
                   "Arbeitsspeicher – nach jedem Start neu eingeben. Nie im Klartext in einer Datei."
                   + ("" if vault.available() else " (Schlüsselbund nicht verfügbar – libsecret fehlt)"), cb=keyring)
        self.check("sec_require_tls", "nur verschlüsselte verbindungen", True,
                   "Unverschlüsselte IMAP/SMTP-Verbindungen ablehnen. Nur für lokale Brücken (z. B. Proton Bridge "
                   "auf 127.0.0.1) ausschalten.", cb=lambda v: app.apply_security())
        self.check("sec_verify_cert", "server-zertifikate streng prüfen", True,
                   "Schützt davor, dass sich jemand als dein Mail-Server ausgibt. Nur für Server mit eigenem "
                   "Zertifikat (selbst signiert) ausschalten.", cb=lambda v: app.apply_security())

        self.section("verschlüsselung (openpgp)",
                     None if pgp.available() else "gpg ist nicht installiert – ohne gpg geht kein openpgp.")
        self.check("pgp_on", "openpgp verwenden", True, "Signieren/verschlüsseln beim Schreiben, prüfen/"
                   "entschlüsseln beim Lesen – über dein gpg (Passphrasen fragt gpg selbst).")
        self.check("pgp_sign", "neue mails standardmäßig signieren", False,
                   "Empfänger können prüfen, dass die Mail wirklich von dir ist und nicht verändert wurde.")
        self.check("pgp_encrypt", "automatisch verschlüsseln, wenn für alle empfänger ein schlüssel da ist", True,
                   "Im Schreibfenster lässt es sich für jede Mail ein-/ausschalten.")
        self.check("pgp_verify", "signaturen eingehender mails prüfen", True)
        self.check("pgp_decrypt", "verschlüsselte mails beim öffnen entschlüsseln", True,
                   "Aus: verschlüsselte Mails bleiben zu (z. B. wenn jemand mitschaut).")
        self.check("pgp_autocrypt_send", "eigenen öffentlichen schlüssel mitsenden (autocrypt)", False,
                   "Andere Programme (Thunderbird, Delta Chat …) können dir dann verschlüsselt antworten.")
        self.check("pgp_autocrypt_import", "schlüssel von absendern automatisch übernehmen (autocrypt)", False,
                   "Bequem, aber ungeprüft: ein Angreifer könnte so einen falschen Schlüssel unterschieben.")
        if pgp.available():
            self.key_box = tui.vbox(spacing=6)
            self.key_box.set_margin_top(8)
            self.inner.append(self.key_box)
            self._keys()

    def _keys(self):
        acc = self.win.app.acct
        addr = acc.address
        box = self.key_box
        tui.clear(box)
        box.append(tui.label(f"dein schlüssel für {mask(addr) if addr else '(adresse noch unbekannt)'}",
                             "ml-place-head"))
        msg = tui.label("", "ml-meta", wrap=True)

        def bg(fn, done):
            def work():
                try:
                    r = fn()
                except Exception as e:
                    r = e
                GLib.idle_add(lambda: (done(r), False)[1])
            threading.Thread(target=work, daemon=True).start()

        def show(k):
            if isinstance(k, Exception) or not k:
                row = tui.hbox(tui.label("noch keiner", "ml-meta"), spacing=8)
                if addr:
                    row.append(tui.Button("schlüssel erzeugen", gen))
                row.append(tui.Button("schlüssel importieren …", self._import))
                box.append(row)
            else:
                fp = k["fpr"]
                box.append(tui.label(f"\U000F0306  {k['uid'].replace(addr, mask(addr))}\n{' '.join(fp[i:i + 4] for i in range(0, 40, 4))}",
                                     "ml-help", wrap=True))
                box.append(tui.hbox(tui.Button("öffentlichen schlüssel kopieren", lambda: copy(fp)),
                                    tui.Button("als datei speichern …", lambda: self._save(fp)),
                                    tui.Button("schlüssel importieren …", self._import), spacing=8))
            box.append(msg)

        def gen():
            msg.set_label("erzeuge schlüssel … gpg fragt gleich nach einer passphrase (gut merken!)")
            name = acc.conf.get("sender_name") or ""
            bg(lambda: pgp.generate(name, addr), lambda r: (msg.set_label(
                f"✗ {r[1] if isinstance(r, tuple) else r}") if isinstance(r, Exception) or not r[0]
                else (self._keys(), self.win.say("schlüssel erzeugt"))))

        def copy(fp):
            bg(lambda: pgp.export(fp), lambda data: (
                Gdk.Display.get_default().get_clipboard().set(data.decode() if isinstance(data, bytes) else ""),
                self.win.say("öffentlicher schlüssel kopiert – kann man gefahrlos weitergeben")))
        if not addr:
            show(None)
            return
        bg(lambda: pgp.own_key(addr), show)

    def _save(self, fp):
        dlg = Gtk.FileDialog(title="öffentlichen schlüssel speichern",
                             initial_name=f"{self.win.app.acct.address or 'schluessel'}.asc")

        def done(d, res):
            try:
                path = d.save_finish(res).get_path()
            except GLib.Error:
                return
            with open(path, "wb") as f:
                f.write(pgp.export(fp))
            self.win.say(f"gespeichert: {os.path.basename(path)}")
        dlg.save(self.win, None, done)

    def _import(self):
        dlg = Gtk.FileDialog(title="openpgp-schlüssel importieren (.asc, .gpg)")

        def done(d, res):
            try:
                path = d.open_finish(res).get_path()
            except GLib.Error:
                return
            with open(path, "rb") as f:
                data = f.read()

            def work():
                n, text = pgp.import_keys(data)
                GLib.idle_add(lambda: (self.win.say(f"{n} schlüssel übernommen" if n else f"nichts übernommen: "
                                                    f"{text[-120:]}", error=not n), self._keys(), False)[2])
            threading.Thread(target=work, daemon=True).start()
        dlg.open(self.win, None, done)

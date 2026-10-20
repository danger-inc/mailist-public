# Neue Mail / Antworten / Weiterleiten – eigenes kleines Fenster im Suite-Stil.
# Senden geht über die Warteschlange: offline geschrieben = gesendet, sobald wieder Netz da ist.
import base64, email.message, email.policy, email.utils, mimetypes, os, re, urllib.parse

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk

import glass
import store as stmod
import tui

DRAFTS = os.path.join(stmod.CACHE, "entwurf.txt")


def parse_mailto(uri):
    u = urllib.parse.urlsplit(uri)
    q = {k.lower(): v for k, v in urllib.parse.parse_qsl(u.query)}
    to = urllib.parse.unquote(u.path)
    if q.get("to"):
        to = ", ".join(x for x in (to, q["to"]) if x)
    return {"to": to, "cc": q.get("cc", ""), "subject": q.get("subject", ""), "body": q.get("body", "")}


def quote(mail, row):
    from window import long_date
    who = row["from_name"] or row["from_addr"]
    segs, _ = mail.render(prefer_html=False) if mail.plain else mail.render()
    text = "".join(s[0] for s in segs).strip()
    lines = "\n".join("> " + l for l in text.split("\n")[:400])
    return f"\n\nAm {long_date(row['date'])} schrieb {who}:\n{lines}\n"


class ComposeWindow(Gtk.ApplicationWindow):
    def __init__(self, app, mode="new", row=None, mail=None, mailto=None):
        super().__init__(application=app, title="mailist · neue mail")
        self.app, self.st = app, app.store
        self.mode, self.row, self.mail = mode, row, mail
        self.files = []              # [(name, bytes, ctype)]
        self.thread = None
        self.headers = {}
        self.sent = False
        self.set_default_size(*getattr(glass, "fit_size", lambda w, h: (w, h))(820, 640))
        getattr(glass, "frame_window", lambda w: w.set_decorated(False))(self)
        for c in ("pb-window", "tui", "ml", "ml-compose"):
            self.add_css_class(c)
        if glass.is_on():
            self.add_css_class("glass")

        top = tui.hbox(spacing=8)
        top.add_css_class("topbar")
        top.append(tui.label("mailist", "brand"))
        self.title_lbl = tui.label("neue mail", "accent")
        top.append(self.title_lbl)
        top.append(tui.label("", hexpand=True))
        self.send_btn = tui.Button("\U000F048A  senden", self.send, tooltip="Senden (Strg+Enter)")
        self.send_btn.add_css_class("on")
        top.append(self.send_btn)
        close = tui.Button("✕", self.close, tooltip="Schließen (Esc)")
        close.add_css_class("close")
        top.append(close)

        form = tui.vbox(spacing=4)

        def field(name, entry):
            row_ = tui.hbox(tui.label(name, "ml-field"), entry, spacing=8)
            form.append(row_)
            return row_
        self.to = tui.Entry("empfänger, mit komma getrennt")
        self.cc = tui.Entry("kopie an")
        self.bcc = tui.Entry("blindkopie an")
        self.subject = tui.Entry("betreff")
        field("an", self.to)
        self.cc_row = field("cc", self.cc)
        self.bcc_row = field("bcc", self.bcc)
        field("betreff", self.subject)
        self.cc_row.set_visible(False)
        self.bcc_row.set_visible(False)
        more = tui.hbox(spacing=6)
        for text, cb in (("+ cc", lambda: self.cc_row.set_visible(True)), ("+ bcc", lambda: self.bcc_row.set_visible(True)),
                         ("\U000F03E2 anhang", self.pick_files)):
            b = Gtk.Button(label=text, focus_on_click=False)
            b.add_css_class("ml-link")
            b.connect("clicked", lambda *_, cb=cb: cb())
            more.append(b)
        more.append(tui.label("", hexpand=True))
        self.info = tui.label("", "ml-meta")
        more.append(self.info)
        form.append(more)
        self.att_box = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=5, row_spacing=4,
                                   column_spacing=4)
        self.att_box.add_css_class("ml-attrow")
        form.append(self.att_box)
        self.body = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, vexpand=True, left_margin=4, top_margin=6,
                                 bottom_margin=20, accepts_tab=False)
        self.body.add_css_class("ml-edit")
        sc = Gtk.ScrolledWindow(child=self.body, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        form.append(sc)
        panel = tui.Panel("mail", form)
        panel.body.set_vexpand(True)
        panel.set_vexpand(True)
        self.panel = panel
        status = tui.hbox(tui.label("strg+enter", "accent"), tui.label("senden", "dim"),
                          tui.label("esc", "accent"), tui.label("schließen", "dim"), spacing=6)
        status.add_css_class("statusbar")
        content = tui.vbox(Gtk.WindowHandle(child=top), panel, status, spacing=0)
        panel.set_margin_start(14)
        panel.set_margin_end(14)
        self.set_child(content)

        # Vorschläge für Adressen
        self.sugg = Gtk.Popover(has_arrow=False, autohide=False, can_focus=False)
        self.sugg.add_css_class("ml-menu")
        self.sugg_box = tui.vbox(spacing=1)
        self.sugg.set_child(self.sugg_box)
        self.sugg_for = None
        for e in (self.to, self.cc, self.bcc):
            e.connect("changed", self._suggest)
            fk = Gtk.EventControllerFocus()
            fk.connect("leave", lambda *_: GLib.timeout_add(150, lambda: (self.sugg.popdown(), False)[1]))
            e.add_controller(fk)
            k = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
            k.connect("key-pressed", self._entry_key, e)
            e.add_controller(k)

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._drop)
        self.add_controller(drop)
        self.connect("close-request", self._closing)
        self._prefill(mailto)

    # ---------------- Vorbelegen
    def _prefill(self, mailto):
        r, m, me = self.row, self.mail, self.st.me
        buf = self.body.get_buffer()
        if mailto:
            d = parse_mailto(mailto)
            self.to.set_text(d["to"])
            if d["cc"]:
                self.cc.set_text(d["cc"])
                self.cc_row.set_visible(True)
            self.subject.set_text(d["subject"])
            buf.set_text(d["body"])
        if not r or not m:
            self.subject.grab_focus() if self.to.get_text() else self.to.grab_focus()
            return
        subj = r["subject"] or ""
        mid = m.header("Message-ID")
        refs = (m.header("References") + " " + mid).strip()
        if self.mode in ("reply", "all"):
            reply_to = m.header("Reply-To") or m.header("From")
            if r["from_addr"] == me:                         # eigene Mail: an die ursprünglichen Empfänger
                reply_to = m.header("To")
            self.to.set_text(reply_to)
            if self.mode == "all":
                cc = [f"{n} <{a}>" if n else a for n, a in stmod.addr_list(m.header("To") + ", " + m.header("Cc"))
                      if a != me and a not in self.to.get_text().lower()]
                if cc:
                    self.cc.set_text(", ".join(dict.fromkeys(cc)))
                    self.cc_row.set_visible(True)
            self.subject.set_text(subj if re.match(r"^(re|aw):", subj, re.I) else "Re: " + subj)
            self.headers = {"In-Reply-To": mid, "References": refs}
            self.thread = r["thread"]
            buf.set_text(quote(m, r))
            self.title_lbl.set_label("antworten" if self.mode == "reply" else "allen antworten")
            self.body.grab_focus()
            buf.place_cursor(buf.get_start_iter())
        elif self.mode == "forward":
            self.subject.set_text(subj if re.match(r"^(fwd?|wg):", subj, re.I) else "Fwd: " + subj)
            from window import long_date
            head = (f"\n\n---------- Weitergeleitete Nachricht ----------\nVon: {m.header('From')}\n"
                    f"Datum: {long_date(r['date'])}\nBetreff: {subj}\nAn: {m.header('To')}\n\n")
            segs, _ = m.render(prefer_html=False) if m.plain else m.render()
            buf.set_text(head + "".join(s[0] for s in segs).strip() + "\n")
            for name, ctype, size, part in m.attachments:
                self.add_file(name, m.payload(part), ctype)
            self.title_lbl.set_label("weiterleiten")
            self.to.grab_focus()
            buf.place_cursor(buf.get_start_iter())
        elif self.mode == "draft":
            self.to.set_text(m.header("To"))
            if m.header("Cc"):
                self.cc.set_text(m.header("Cc"))
                self.cc_row.set_visible(True)
            self.subject.set_text(subj)
            segs, _ = m.render(prefer_html=False) if m.plain else m.render()
            buf.set_text("".join(s[0] for s in segs))
            for name, ctype, size, part in m.attachments:
                self.add_file(name, m.payload(part), ctype)
            self.title_lbl.set_label("entwurf")
        self.set_title(f"mailist · {self.title_lbl.get_label()}")

    # ---------------- Anhänge
    def pick_files(self):
        dlg = Gtk.FileDialog(title="Anhänge wählen")

        def done(d, res):
            try:
                files = d.open_multiple_finish(res)
            except GLib.Error:
                return
            for i in range(files.get_n_items()):
                self._add_path(files.get_item(i).get_path())
        dlg.open_multiple(self, None, done)

    def _drop(self, _t, value, x, y):
        for f in value.get_files():
            if f.get_path():
                self._add_path(f.get_path())
        return True

    def _add_path(self, path):
        if not path or not os.path.isfile(path):
            return
        if os.path.getsize(path) > 24 * 1024 * 1024:
            self.info.set_label(f"✗ {os.path.basename(path)} ist größer als 24 MB (gmail-grenze 25 MB)")
            return
        with open(path, "rb") as f:
            data = f.read()
        self.add_file(os.path.basename(path), data, mimetypes.guess_type(path)[0] or "application/octet-stream")

    def add_file(self, name, data, ctype):
        from window import human_size
        self.files.append((name, data, ctype))
        b = Gtk.Button(focus_on_click=False, tooltip_text="klick = entfernen")
        b.add_css_class("ml-chip")
        b.set_child(tui.hbox(tui.label("\U000F03E2", "accent"), tui.label(name[:36]), tui.label(human_size(len(data)),
                                                                                                 "ml-meta"),
                             tui.label("✕", "ml-meta"), spacing=6))
        entry = (name, data, ctype)
        b.connect("clicked", lambda *_: self._remove_file(b, entry))
        self.att_box.append(b)
        self._size_info()

    def _remove_file(self, btn, entry):
        if entry in self.files:
            self.files.remove(entry)
        child = btn.get_parent()
        self.att_box.remove(child)
        self._size_info()

    def _size_info(self):
        from window import human_size
        total = sum(len(d) for _, d, _ in self.files)
        self.info.set_label(f"{len(self.files)} anhänge · {human_size(total)}" if self.files else "")
        if total > 24 * 1024 * 1024:
            self.info.set_label(f"✗ zusammen {human_size(total)} – gmail nimmt höchstens 25 MB")

    # ---------------- Adressvorschläge
    def _suggest(self, entry):
        text = entry.get_text()
        part = text.rsplit(",", 1)[-1].strip()
        if len(part) < 2 or "@" in part and "." in part.split("@")[-1] and " " not in part:
            self.sugg.popdown()
            return
        hits = self.st.contacts(part)
        tui.clear(self.sugg_box)
        if not hits:
            self.sugg.popdown()
            return
        for name, addr in hits:
            b = Gtk.Button()
            b.add_css_class("opt")
            b.set_child(tui.hbox(tui.label(name or addr), tui.label(addr if name else "", "ml-meta"), spacing=8))
            b.connect("clicked", lambda *_, n=name, a=addr: self._take(entry, n, a))
            self.sugg_box.append(b)
        if self.sugg.get_parent() is not entry:
            if self.sugg.get_parent():
                self.sugg.unparent()
            self.sugg.set_parent(entry)
        self.sugg_for = entry
        self.sugg.popup()

    def _take(self, entry, name, addr):
        parts = [p.strip() for p in entry.get_text().split(",")[:-1] if p.strip()]
        parts.append(f"{name} <{addr}>" if name and "," not in name else addr)
        entry.set_text(", ".join(parts) + ", ")
        entry.set_position(-1)
        self.sugg.popdown()
        entry.grab_focus()

    def _entry_key(self, _c, kv, _code, state, entry):
        if not self.sugg.get_visible() or self.sugg_for is not entry:
            return False
        first = self.sugg_box.get_first_child()
        if kv in (Gdk.KEY_Tab, Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Down) and first:
            first.emit("clicked")
            return True
        if kv == Gdk.KEY_Escape:
            self.sugg.popdown()
            return True
        return False

    # ---------------- Senden
    def _recipients(self, text):
        return [a for _, a in email.utils.getaddresses([text]) if a]

    def build(self):
        msg = email.message.EmailMessage(policy=email.policy.SMTP)
        if self.st.me:
            msg["From"] = self.st.me
        for k, e in (("To", self.to), ("Cc", self.cc), ("Bcc", self.bcc)):
            v = e.get_text().strip().strip(",").strip()
            if v:
                msg[k] = v
        msg["Subject"] = self.subject.get_text().strip()
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid(domain=(self.st.me.split("@")[-1] if self.st.me else None))
        for k, v in self.headers.items():
            if v:
                msg[k] = v
        buf = self.body.get_buffer()
        msg.set_content(buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False))
        for name, data, ctype in self.files:
            maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
            msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=name)
        return msg.as_bytes()

    def send(self):
        to = self._recipients(self.to.get_text()) + self._recipients(self.cc.get_text()) + \
             self._recipients(self.bcc.get_text())
        bad = [a for a in to if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", a)]
        if not to:
            self.info.set_label("✗ kein empfänger")
            self.to.grab_focus()
            return
        if bad:
            self.info.set_label(f"✗ adresse sieht falsch aus: {bad[0]}")
            return
        if sum(len(d) for _, d, _ in self.files) > 24 * 1024 * 1024:
            self.info.set_label("✗ anhänge zu groß (höchstens 25 MB)")
            return
        raw = self.build()
        subj = self.subject.get_text().strip()
        self.st.queue({"op": "send", "ids": [], "raw": base64.b64encode(raw).decode(), "thread": self.thread,
                       "subject": subj})
        if self.mode == "draft" and self.row:
            self.st.queue({"op": "delete", "ids": [self.row["id"]]})
            self.st.drop([self.row["id"]])
        self.app.syncer.request("flush")
        self.app.syncer.request("sync")
        self.sent = True
        w = self.app.win
        if w:
            import gmail
            w.say(f"wird gesendet: {subj or '(ohne betreff)'}" if gmail.online() else
                  "offline – wird gesendet, sobald wieder netz da ist")
        self.close()

    # ---------------- Tasten & Schließen
    def _key(self, _c, kv, _code, state):
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl and kv in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.send()
            return True
        if kv == Gdk.KEY_Escape and not self.sugg.get_visible():
            self.close()
            return True
        return False

    def _has_text(self):
        buf = self.body.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        if self.mode in ("reply", "all", "forward"):
            text = text.split("\nAm ", 1)[0].split("---------- Weitergeleitete", 1)[0]
        return bool(text.strip()) or (bool(self.files) and self.mode not in ("forward", "draft"))

    def _closing(self, *_):
        if self.sent or not self._has_text() or getattr(self, "_really", False):
            if self.sugg.get_parent():
                self.sugg.unparent()
            return False
        import pages

        def discard():
            self._really = True
            self.close()
        pages.confirm(self, "mail verwerfen?", "Der Text ist noch nicht gesendet und geht verloren.", "verwerfen", discard)
        return True

# Vorschau rechts: Kopf (Absender, Empfänger, Datum), Knöpfe (antworten … löschen), Sicherheits-Hinweise,
# der Text (formatiert, Links anklickbar – verdächtige rot, echtes Ziel beim Drüberfahren unten in der Statuszeile),
# Anhänge. Bei mehreren ausgewählten Mails: große Knöpfe für alle auf einmal.
import concurrent.futures, email.utils, json, os, re, subprocess, tempfile

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango

import gmail
import guard
import mime
import pgp
import store as stmod
import tui

_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="mailist-body")
_img_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="mailist-img")
_img_cache = {}          # url → GdkPixbuf.Pixbuf (None = kein Bild/Tracker), begrenzt
IMG_LIMIT = 8_000_000
IMG_MAX = 60             # mehr Bilder pro Mail lädt mailist nicht


def fetch_image(url):
    """Externes Bild holen (läuft im Thread) → Pixbuf, "px" (1×1-Zählpixel) oder None (zu groß, kein Bild)."""
    if url in _img_cache:
        return _img_cache[url]
    import urllib.request
    pb = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) mailist",
                                                   "Accept": "image/*"})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = r.read(IMG_LIMIT + 1)
        if len(data) <= IMG_LIMIT:
            loader = GdkPixbuf.PixbufLoader()
            loader.write(data)
            loader.close()
            pb = loader.get_pixbuf()
            if pb is not None and (pb.get_width() <= 2 or pb.get_height() <= 2):
                pb = "px"                              # Zählpixel
    except Exception:
        pb = None
    if len(_img_cache) > 300:
        _img_cache.clear()
    _img_cache[url] = pb
    return pb
CACHE = stmod.CACHE


def fetch_raw(st, mid):
    raw = st.body(mid)
    if raw is not None:
        return raw
    raw = st.backend.raw(mid)
    st.save_body(mid, raw)
    r = st.message(mid)
    if r is not None and not r["snippet"]:            # IMAP liefert keine Vorschau → aus dem Text nachtragen
        try:
            m = mime.Mail(raw)
            text = m.plain or re.sub(r"<[^>]+>|&nbsp;", " ", m.html or "")
            snip = " ".join(text.split())[:180]
            if snip:
                with st.lock:
                    st.db.execute("UPDATE messages SET snippet=? WHERE id=?", (snip, mid))
                    st.db.commit()
        except Exception:
            pass
    return raw


def downloads():
    d = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or os.path.expanduser("~/Downloads")
    os.makedirs(d, exist_ok=True)
    return d


def unique_path(folder, name):
    base, ext = os.path.splitext(mime.safe_filename(name))
    p = os.path.join(folder, base + ext)
    n = 2
    while os.path.exists(p):
        p = os.path.join(folder, f"{base} ({n}){ext}")
        n += 1
    return p


def type_glyph(name, ctype):
    n = name.lower()
    if ctype.startswith("image/"):
        return "\U000F021F"
    if n.endswith(".pdf"):
        return "\U000F0226"
    if ctype.startswith("video/"):
        return "\U000F022B"
    if ctype.startswith("audio/"):
        return "\U000F0223"
    if n.endswith((".zip", ".rar", ".7z", ".tar", ".gz")):
        return "\U000F05C4"
    if n.endswith((".doc", ".docx", ".odt", ".txt", ".md")):
        return "\U000F022C"
    if n.endswith((".xls", ".xlsx", ".ods", ".csv")):
        return "\U000F021B"
    if n.endswith(".ics"):
        return "\U000F00F0"
    if n.endswith(mime.RISKY_EXT):
        return "\U000F0027"
    return "\U000F0214"


class Reader(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, vexpand=True, hexpand=True)
        self.win = win
        self.st = win.st
        self.mid = None
        self.row = None
        self.mail = None
        self.links = []          # (start, end, href)
        self.link_warn = {}
        self._hover = None
        self._load_token = 0
        self.stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE,
                               transition_duration=70)
        self.append(self.stack)

        self.empty_lbl = tui.label("", "ml-empty", xalign=0.5, wrap=True)
        self.stack.add_named(tui.vbox(tui.label("\U000F01EE", "ml-bigglyph", xalign=0.5), self.empty_lbl, spacing=8,
                                      valign=Gtk.Align.CENTER, vexpand=True), "empty")
        self.stack.add_named(self._build_mail(), "mail")
        self.bulk = tui.vbox(spacing=10)
        self.bulk.add_css_class("ml-bulk")
        self.stack.add_named(Gtk.ScrolledWindow(child=self.bulk, hscrollbar_policy=Gtk.PolicyType.NEVER), "bulk")
        tui.Theme.get().listeners.append(self.rerender)

    # ================================================================ Aufbau
    def _build_mail(self):
        box = tui.vbox(spacing=6)
        box.set_margin_end(6)
        self.subject = tui.label("", "ml-subject", wrap=True)
        self.subject.set_selectable(True)
        box.append(self.subject)
        self.avatar = tui.label("", "ml-avatar", xalign=0.5)
        self.avatar.set_valign(Gtk.Align.START)
        self.from_lbl = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=30, row_spacing=0,
                                    column_spacing=2, homogeneous=False)
        self.to_lbl = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=30, row_spacing=0,
                                  column_spacing=2, homogeneous=False)
        self.date_lbl = tui.label("", "ml-meta")
        self.date_lbl.set_valign(Gtk.Align.START)
        head = tui.hbox(self.avatar, tui.vbox(self.from_lbl, self.to_lbl, spacing=1, hexpand=True), self.date_lbl,
                        spacing=10)
        box.append(head)

        # Knöpfe
        self.tools = tui.hbox(spacing=2)
        self.tools.add_css_class("ml-tools")
        self.tool_names = []

        def tool(glyph, name, tip, cb, danger=False):
            b = Gtk.Button(tooltip_text=tip, focus_on_click=False)
            b.add_css_class("ml-tool")
            if danger:
                b.add_css_class("danger")
            n = tui.label(name, "ml-toolname")
            self.tool_names.append(n)
            b.set_child(tui.hbox(tui.label(glyph), n, spacing=4))
            b.connect("clicked", lambda *_: cb())
            self.tools.append(b)
            return b
        tool("\U000F045A", "antworten", "Antworten (r)", lambda: self.reply("reply"))
        tool("\U000F0F5D", "allen", "Allen antworten (a)", lambda: self.reply("all"))
        tool("\U000F0F5C", "weiter", "Weiterleiten (f)", lambda: self.reply("forward"))
        self.tools.append(tui.label("", hexpand=True))
        tool("\U000F003C", "", "Archivieren (e) – raus aus dem Posteingang", lambda: self.win.archive([self.mid]))
        self.read_btn = tool("\U000F06EE", "", "Ungelesen/gelesen (u)", lambda: self.win.toggle_read())
        self.star_btn = tool("\U000F04D2", "", "Markieren (s)", lambda: self.win.star([self.mid]))
        tool("\U000F0A7A", "", "Löschen (Entf) – Strg+Z holt sie zurück", lambda: self.win.trash([self.mid]),
             danger=True)
        more = Gtk.Button(label="\U000F01D8", tooltip_text="Mehr", focus_on_click=False)
        more.add_css_class("ml-tool")
        more.connect("clicked", lambda b: self.more_menu(b))
        self.tools.append(more)
        box.append(self.tools)

        # Sicherheit
        self.guard_box = tui.vbox(spacing=2)
        self.guard_box.add_css_class("ml-guard")
        self.guard_head = Gtk.Button(focus_on_click=False)
        self.guard_head.add_css_class("flat")
        self.guard_head.connect("clicked", lambda *_: self.guard_rev.set_reveal_child(not self.guard_rev.get_reveal_child()))
        self.guard_rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN, transition_duration=120)
        self.guard_list = tui.vbox(spacing=2)
        self.guard_list.set_margin_start(24)
        self.guard_rev.set_child(self.guard_list)
        self.guard_box.append(self.guard_head)
        self.guard_box.append(self.guard_rev)
        box.append(self.guard_box)

        # OSINT-Leiste (nur volle Ausgabe)
        self.osint = None

        # Verlauf (andere Mails derselben Unterhaltung)
        self.thread_box = tui.vbox(spacing=0)
        box.append(self.thread_box)

        # Text
        self.text = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                 vexpand=True, left_margin=2, right_margin=6, top_margin=6, bottom_margin=24,
                                 pixels_below_lines=1)
        self.text.add_css_class("ml-text")
        self.buf = self.text.get_buffer()
        self._make_tags()
        click = Gtk.GestureClick(button=1)
        click.connect("released", self._text_click)
        self.text.add_controller(click)
        rclick = Gtk.GestureClick(button=3)
        rclick.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)   # vor GTKs eigenem (grauem) Menü
        rclick.connect("pressed", self.text_menu)
        self.text.add_controller(rclick)
        sclick = Gtk.GestureClick(button=3)
        sclick.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        sclick.connect("pressed", lambda g, n, x, y: (
            g.set_state(Gtk.EventSequenceState.CLAIMED),
            self._label_menu(self.subject, x, y)))
        self.subject.add_controller(sclick)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._text_motion)
        motion.connect("leave", lambda *_: self._set_hover(None))
        self.text.add_controller(motion)
        self.spinner = tui.label("", "ml-meta")
        box.append(self.spinner)
        # Externe Bilder: Hinweis + „laden“ (automatisch nur, wenn in den Einstellungen an)
        self.img_lbl = tui.label("", "ml-meta", hexpand=True, xalign=0, ellipsize=True)
        self.img_btn = Gtk.Button(label="laden", focus_on_click=False,
                                  tooltip_text="Externe Bilder dieser Mail laden – der Absender sieht dann, dass und "
                                               "wann du sie geöffnet hast. Tracker-Pixel bleiben blockiert.")
        self.img_btn.add_css_class("ml-link")
        self.img_btn.connect("clicked", lambda *_: self.load_images())
        self.img_bar = tui.hbox(self.img_lbl, self.img_btn, spacing=8)
        self.img_bar.set_visible(False)
        box.append(self.img_bar)
        self._img_urls, self._img_pos, self._img_token = [], [], None
        box.append(self.text)

        self.att_box = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=6, row_spacing=4,
                                   column_spacing=4, homogeneous=False)
        box.append(self.att_box)
        vp = Gtk.Viewport(child=box, scroll_to_focus=False)     # sonst springt die Vorschau, sobald der Text den Fokus bekommt
        self.scroll = Gtk.ScrolledWindow(child=vp, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        return self.scroll

    def _make_tags(self):
        c = tui.colors()
        prim, sec, ter, fg = c.get("primary"), c.get("secondary"), c.get("tertiary"), c.get("foreground")
        tt = self.buf.get_tag_table()
        for name in ("bold", "italic", "h1", "h2", "quote", "mono", "link", "linkwarn", "small", "strike", "bullet",
                     "img", "dimq"):
            t = tt.lookup(name)
            if t:
                tt.remove(t)
        self.buf.create_tag("bold", weight=Pango.Weight.BOLD)
        self.buf.create_tag("italic", style=Pango.Style.ITALIC)
        self.buf.create_tag("h1", weight=Pango.Weight.BOLD, scale=1.3, pixels_above_lines=4, pixels_below_lines=4)
        self.buf.create_tag("h2", weight=Pango.Weight.BOLD, scale=1.12, pixels_above_lines=2)
        self.buf.create_tag("quote", left_margin=16, foreground_rgba=_alpha(fg, 0.6))
        self.buf.create_tag("mono", family="JetBrainsMono Nerd Font, monospace")
        self.buf.create_tag("link", foreground_rgba=prim, underline=Pango.Underline.SINGLE)
        self.buf.create_tag("linkwarn", foreground_rgba=ter, underline=Pango.Underline.ERROR,
                            background_rgba=_alpha(ter, 0.12))
        self.buf.create_tag("small", scale=0.86)
        self.buf.create_tag("strike", strikethrough=True)
        self.buf.create_tag("bullet", foreground_rgba=prim, weight=Pango.Weight.BOLD)
        self.buf.create_tag("img", foreground_rgba=sec, style=Pango.Style.ITALIC, scale=0.9)
        if not tt.lookup("rimg"):
            self.buf.create_tag("rimg")
            self.buf.create_tag("ialt")
            self.buf.create_tag("hidden", invisible=True)

    def rerender(self):
        self._make_tags()
        if self.mail is not None:
            self._render_body()

    # ================================================================ Anzeigen
    def update_title(self):
        """Panel-Titel: „vorschau“ neben der Liste, der Betreff, wenn die Mail groß offen ist."""
        if self.row is None or self.stack.get_visible_child_name() != "mail":
            return
        w = self.win
        w.read_panel.set_title((self.row["subject"] or "mail")[:40] if w.reading else "vorschau")

    def _reset_images(self):
        self._img_token = None
        self._img_urls, self._img_pos = [], []
        self.img_bar.set_visible(False)

    def show_empty(self, where=""):
        self._reset_images()
        self.mid = None
        self.row = None
        self.mail = None
        self.empty_lbl.set_label(f"keine mail ausgewählt\n{where}" if where else "keine mail ausgewählt")
        self.stack.set_visible_child_name("empty")
        self.win.read_panel.set_title("vorschau")

    def show(self, mid):
        row = self.st.message(mid)
        if row is None:
            return
        same = mid == self.mid
        if not same:
            self._reset_images()
        self.mid, self.row = mid, row
        self.stack.set_visible_child_name("mail")
        self._header(row)
        if same and self.mail is not None:
            return
        self.mail = None
        self.buf.set_text("")
        self.links = []
        tui.clear(self.att_box)
        self._thread(row)
        self._guard(*guard.quick(row))
        if self.osint:
            self.osint.set_mail(row, None)
        self.scroll.get_vadjustment().set_value(0)
        self._load_token += 1
        token = self._load_token
        raw = self.st.body(mid)
        if raw is not None:
            self._loaded(token, raw)
        else:
            self.spinner.set_label("\U000F04E6 lade text …")
            self.spinner.set_visible(True)
            fut = _pool.submit(fetch_raw, self.st, mid)
            fut.add_done_callback(lambda f: GLib.idle_add(lambda: (self._fetched(token, f), False)[1]))
        self.win.schedule_mark_read(mid)
        self._prefetch_next()

    def show_raw(self, raw, name):
        """.eml-Datei (nicht aus Gmail) anzeigen."""
        m = mime.Mail(raw)
        fn, fa = stmod.split_addr(m.header("From"))
        d = m.date()
        row = {"id": None, "thread": None, "labels": " ", "from_name": fn, "from_addr": fa, "to_addr": m.header("To"),
               "subject": m.header("Subject") or name, "snippet": "", "date": int(d.timestamp() * 1000) if d else 0,
               "size": len(raw), "att": 0, "unread": 0, "starred": 0,
               "hdr": json.dumps({"authentication-results": m.header("Authentication-Results"),
                                  "reply-to": m.header("Reply-To")})}
        self.mid, self.row = None, row
        self.stack.set_visible_child_name("mail")
        self._header(row)
        tui.clear(self.thread_box)
        self._load_token += 1
        self._loaded(self._load_token, raw)

    def _fetched(self, token, fut):
        if token != self._load_token:
            return
        try:
            raw = fut.result()
        except gmail.ExpiredError:
            self.spinner.set_label("⚠ anmeldung abgelaufen – oben rechts neu anmelden")
            return
        except (gmail.AuthError, gmail.ApiError) as e:
            self.spinner.set_label(f"✗ text nicht geladen: {e}")
            return
        except Exception as e:
            self.spinner.set_label(f"\U000F0B5A offline – text noch nicht im zwischenspeicher ({type(e).__name__})")
            return
        self._loaded(token, raw)

    def _loaded(self, token, raw):
        if token != self._load_token:
            return
        self.spgp = None
        p = self.win.prefs
        if p.get("pgp_on", True) and pgp.available() and pgp.looks_pgp(raw):
            dec, ver = p.get("pgp_decrypt", True), p.get("pgp_verify", True)
            if dec or ver:
                self.spinner.set_label("\U000F033E entschlüssele / prüfe signatur …")
                self.spinner.set_visible(True)
                fut = _pool.submit(pgp.process, raw, dec, ver)
                fut.add_done_callback(lambda f: GLib.idle_add(lambda: (self._pgp_done(token, raw, f), False)[1]))
                return
        self._show_loaded(token, raw)

    def _pgp_done(self, token, raw, fut):
        if token != self._load_token:
            return
        try:
            raw2, st = fut.result()
        except Exception as e:
            raw2, st = raw, {"encrypted": False, "decrypted": False, "error": f"gpg: {e}", "sig": None}
        self.spgp = st
        self._show_loaded(token, raw2)

    def _pgp_why(self):
        """OpenPGP-Ergebnis als Hinweise für den Kasten oben."""
        st = getattr(self, "spgp", None)
        if not st:
            return []
        out = []
        if st.get("encrypted"):
            if st.get("decrypted"):
                out.append(("ok", "\U000F033E verschlüsselt (openpgp) – entschlüsselt"))
            elif st.get("error"):
                out.append(("mittel", f"\U000F033E verschlüsselt – {st['error']}"))
            else:
                out.append(("info", "\U000F033E verschlüsselt – entschlüsseln ist in den einstellungen aus"))
        sig = st.get("sig")
        if sig:
            who = sig.get("who") or sig.get("fpr", "")[-16:]
            txt = {"good": f"signatur gültig: {who}" + (" (schlüssel nicht beglaubigt)"
                                                          if sig.get("trust") in ("", "undefined", "never") else ""),
                   "bad": "signatur UNGÜLTIG – der inhalt wurde nach dem signieren verändert",
                   "expired": f"signatur von {who} – schlüssel abgelaufen",
                   "revoked": f"signatur von {who} – schlüssel wurde widerrufen",
                   "nokey": "signiert, aber der öffentliche schlüssel des absenders fehlt",
                   "unknown": "signatur ließ sich nicht prüfen"}[sig.get("state", "unknown")]
            lvl = {"good": "ok", "bad": "hoch", "revoked": "hoch", "expired": "mittel"}.get(sig.get("state"), "info")
            out.append((lvl, "\U000F0124 " + txt))
        return out

    def _show_loaded(self, token, raw):
        if token != self._load_token:
            return
        self.spinner.set_visible(False)
        try:
            self.mail = mime.Mail(raw)
        except Exception as e:
            self.spinner.set_label(f"✗ mail lässt sich nicht lesen: {e}")
            self.spinner.set_visible(True)
            return
        self._render_body()
        self._attachments()
        if self.osint:
            self.osint.set_mail(self.row, self.mail)
        p = self.win.prefs
        ac = self.mail.header("Autocrypt")
        if ac and self.row and p.get("pgp_on", True) and p.get("pgp_autocrypt_import", False) and pgp.available():
            _pool.submit(pgp.import_autocrypt, ac, self.row["from_addr"])

    def _render_body(self):
        lvl, why, self.link_warn, (segs, info) = guard.full(self.row, self.mail, self.st if self.mid else None) \
            if self.row else ("ok", [], {}, self.mail.render())
        if not self.win.prefs.get("sec_check", True):
            why, self.link_warn = [], {}               # Sicherheits-Check in den Einstellungen aus
        why = self._pgp_why() + list(why)
        if why:
            lvl = guard.level_of(why)
        if not self.win.prefs.get("html", True) and self.mail.plain:
            segs, links = mime.plain_segments(self.mail.plain)
            info = dict(info, links=links)
        self._guard(lvl, why)
        self.buf.set_text("")
        self.links = []
        self._img_token = None                 # noch laufende Bild-Downloads der vorigen Mail verwerfen
        self._img_urls, self._img_pos = list(info.get("images") or []), []
        it = self.buf.get_end_iter()
        for text, styles, href in segs:
            tags = [s for s in styles if s in ("bold", "italic", "h1", "h2", "quote", "mono", "small", "strike",
                                               "bullet", "img", "rimg", "ialt")]
            start = it.get_offset()
            if "rimg" in styles:
                self._img_pos.extend(range(start, start + len(text)))
            if href:
                tags.append("linkwarn" if href in self.link_warn else "link")
            self.buf.insert_with_tags_by_name(it, text, *tags) if tags else self.buf.insert(it, text)
            if href:
                self.links.append((start, it.get_offset(), href))
        if not segs:
            self.buf.insert(it, "(kein text)")
        self._img_trackers = info.get("trackers", 0)
        self.img_bar.set_visible(False)
        if info.get("remote_images"):
            if self._img_urls and self.win.prefs.get("load_images", False):
                self.load_images()
            else:
                n = info["remote_images"]
                self.img_lbl.set_label(f"\U000F0976 {n} externe {'bild' if n == 1 else 'bilder'} nicht geladen"
                                       + (f" · {info['trackers']} tracker blockiert" if info.get("trackers") else ""))
                self.img_btn.set_visible(bool(self._img_urls))
                self.img_bar.set_visible(True)

    # ================================================================ Externe Bilder
    def load_images(self):
        urls = self._img_urls[:IMG_MAX]
        if not urls or len(self._img_pos) < len(urls):
            return
        token = self._img_token = object()
        state = {"left": len(urls), "ok": 0, "px": 0}
        self.img_btn.set_visible(False)
        self.img_lbl.set_label(f"\U000F04E6 lade {len(urls)} {'bild' if len(urls) == 1 else 'bilder'} …")
        self.img_bar.set_visible(True)
        for i, url in enumerate(urls):
            fut = _img_pool.submit(fetch_image, url)
            fut.add_done_callback(lambda f, i=i: GLib.idle_add(self._image_done, token, i, f, state))

    def _image_done(self, token, i, fut, state):
        if token is not self._img_token:
            return False
        state["left"] -= 1
        try:
            pb = fut.result()
        except Exception:
            pb = None
        if pb == "px":
            state["px"] += 1
        elif pb is not None and self._place_image(self._img_pos[i], pb):
            state["ok"] += 1
        if state["left"] <= 0:
            tr = self._img_trackers + state["px"]
            if state["ok"] == len(self._img_urls[:IMG_MAX]) and not tr:
                self.img_bar.set_visible(False)
            else:
                miss = len(self._img_urls[:IMG_MAX]) - state["ok"] - state["px"]
                self.img_lbl.set_label("\U000F0976 " + " · ".join(
                    x for x in ((f"{miss} nicht ladbar" if miss else ""), (f"{tr} tracker blockiert" if tr else ""))
                    if x))
        return False

    def _place_image(self, off, pb):
        buf = self.buf
        it = buf.get_iter_at_offset(off)
        e = it.copy()
        e.forward_char()
        if buf.get_text(it, e, True) != "\u200b" or not it.has_tag(buf.get_tag_table().lookup("rimg")):
            return False
        maxw = max(160, min(720, self.text.get_width() - 24))
        w, h = pb.get_width(), pb.get_height()
        if w > maxw:
            pb = pb.scale_simple(maxw, max(1, int(h * maxw / w)), GdkPixbuf.InterpType.BILINEAR)
        tex = Gdk.Texture.new_for_pixbuf(pb)
        tags = [t for t in it.get_tags()]
        buf.delete(it, e)
        it = buf.get_iter_at_offset(off)
        buf.insert_paintable(it, tex)
        s, e = buf.get_iter_at_offset(off), buf.get_iter_at_offset(off + 1)
        for t in tags:
            buf.apply_tag(t, s, e)
        alt = buf.get_tag_table().lookup("ialt")
        if e.has_tag(alt):                     # „[alt-text]“ hinter dem Bild ausblenden
            end = e.copy()
            end.forward_to_tag_toggle(alt)
            buf.apply_tag_by_name("hidden", e, end)
        return True

    def _header(self, r):
        self.subject.set_label(r["subject"] or "(ohne betreff)")
        me = self.st.me
        name, addr = r["from_name"], r["from_addr"]
        if addr and addr == me:
            self._chips(self.from_lbl, [("", "ich", None)], bold=True)
            self.avatar.set_label("\U000F0004")
        else:
            self._chips(self.from_lbl, [("", name or addr or "?", addr or None)], bold=True)
            self.avatar.set_label((name or addr or "?")[:1].upper())
        to = [("ich" if a == me else (n or a), None if a == me else a) for n, a in stmod.addr_list(r["to_addr"])]
        try:
            cc = [("ich" if a == me else (n or a), None if a == me else a) for n, a in
                  stmod.addr_list(json.loads(r["hdr"] or "{}").get("cc", ""))]
        except ValueError:
            cc = []
        ents = []
        if to:
            ents.append(("an ", None, None))
            for k, (n, a) in enumerate(to[:6]):
                ents.append(("" if not k else ", ", n, a))
            if len(to) > 6:
                ents.append((f" +{len(to) - 6}", None, None))
        if cc:
            ents.append(("  ·  cc " if to else "cc ", None, None))
            for k, (n, a) in enumerate(cc[:4]):
                ents.append(("" if not k else ", ", n, a))
        self._chips(self.to_lbl, ents, meta=True)
        self.to_lbl.set_visible(bool(to or cc))
        from window import long_date
        self.date_lbl.set_label(long_date(r["date"]))
        self.read_btn.get_child().get_first_child().set_label("\U000F06EE" if not r["unread"] else "\U000F06EF")
        self.read_btn.set_tooltip_text("als ungelesen markieren (u)" if not r["unread"] else "als gelesen markieren (u)")
        self.star_btn.get_child().get_first_child().set_label("\U000F04CE" if r["starred"] else "\U000F04D2")
        (self.star_btn.add_css_class if r["starred"] else self.star_btn.remove_css_class)("on")
        self.update_title()
        for n in self.tool_names:
            n.set_visible(self.get_width() > 520 or self.get_width() == 0)

    def _chips(self, box, ents, bold=False, meta=False):
        """Kopfzeile: Adressen als Knöpfe – Klick kopiert, Rechtsklick öffnet das Menü. ents: (davor, text, adresse)."""
        while (c := box.get_first_child()):
            box.remove(c)
        for pre, text, addr in ents:
            if text is None:                              # reiner Zwischentext
                box.append(tui.label(pre, "ml-meta"))
                continue
            if pre:
                box.append(tui.label(pre, "ml-meta"))
            if not addr:
                box.append(tui.label(f"<b>{tui.esc(text)}</b>", markup=True) if bold else tui.label(text, "ml-meta"))
                continue
            b = Gtk.Button(focus_on_click=False, tooltip_text=f"{addr} – klicken zum Kopieren, Rechtsklick für mehr")
            b.add_css_class("ml-link")
            lab = tui.label(f"<b>{tui.esc(text)}</b>" if bold else tui.esc(text), markup=True, ellipsize=True)
            if meta:
                lab.add_css_class("ml-meta")
            if bold and text != addr:
                lab = tui.hbox(lab, tui.label(f"<span alpha='60%'>{tui.esc(addr)}</span>", markup=True, ellipsize=True),
                                spacing=6)
            b.set_child(lab)
            b.connect("clicked", lambda _b, a=addr: self.copy_text(a, "adresse kopiert: " + a))
            rc = Gtk.GestureClick(button=3)
            rc.connect("pressed", lambda g, n, x, y, b=b, a=addr, t=text: (
                g.set_state(Gtk.EventSequenceState.CLAIMED), self.addr_menu(b, a, t, x, y)))
            b.add_controller(rc)
            box.append(b)

    def copy_text(self, text, msg="kopiert"):
        self.get_clipboard().set(text)
        self.win.say(msg, seconds=3)

    def addr_menu(self, widget, addr, name, x, y):
        import pages
        m = pages.Menu(widget, x, y)
        m.item("adresse kopieren", lambda: self.copy_text(addr, "adresse kopiert: " + addr))
        if name and name != addr:
            m.item("name kopieren", lambda: self.copy_text(name, "name kopiert"))
        m.item("neue mail an …", lambda: self.win.app.compose("mailto:" + addr))
        m.sep()
        m.item("alle von dieser adresse zeigen", lambda: self.win.go(("sender", addr, name or addr)))
        m.popup()

    def _label_menu(self, lab, x, y):
        import pages
        m = pages.Menu(lab, x, y)
        ok, a, b = lab.get_selection_bounds()
        if ok:
            m.item("kopieren", lambda: self.copy_text(lab.get_text()[a:b], "kopiert"))
        m.item("ganzen betreff kopieren", lambda: self.copy_text(lab.get_text(), "betreff kopiert"))
        m.popup()

    def text_menu(self, g, n, x, y):
        import pages
        g.set_state(Gtk.EventSequenceState.CLAIMED)
        href = self._href_at(self._offset_at(x, y))
        m = pages.Menu(self.text, x, y)
        if href:
            m.item("link öffnen", lambda: self.open_link(href))
            m.item("link kopieren", lambda: self.copy_text(href.removeprefix("mailto:"), "link kopiert"))
            m.sep()
        if self.buf.get_has_selection():
            m.item("kopieren", lambda: self.text.emit("copy-clipboard"))
        m.item("alles auswählen", lambda: self.text.emit("select-all", True))
        m.item("ganzen text kopieren", lambda: self.copy_text(
            self.buf.get_text(self.buf.get_start_iter(), self.buf.get_end_iter(), False), "text kopiert"))
        if self.buf.get_has_selection():
            def sel():
                a, b = self.buf.get_selection_bounds()
                return self.buf.get_text(a, b, False).strip()
            m.sep()
            m.item("auswahl in allen mails suchen", lambda: (self.win.search.set_text(sel()),
                                                             self.win.search.emit("activate")))
            m.item("auswahl im web suchen", lambda: Gtk.UriLauncher(
                uri="https://duckduckgo.com/?q=" + __import__("urllib.parse").parse.quote(sel())).launch(
                self.win, None, None))
        m.popup()

    def refresh_row(self):
        if not self.mid:
            return
        r = self.st.message(self.mid)
        if r is None:
            return
        self.row = r
        self._header(r)

    def _guard(self, lvl, why):
        for c in ("mittel", "hoch"):
            self.guard_box.remove_css_class(c)
        if lvl in ("mittel", "hoch"):
            self.guard_box.add_css_class(lvl)
        tui.clear(self.guard_list)
        if not why:
            self.guard_box.set_visible(False)
            return
        self.guard_box.set_visible(True)
        top = max(why, key=lambda w: guard.ORDER[w[0]])
        glyph = {"ok": "\U000F0565", "info": "\U000F02FC", "mittel": "\U000F0026", "hoch": "\U000F0027"}[lvl]
        head = {"ok": "absender bestätigt", "info": "hinweis", "mittel": "vorsicht", "hoch": "achtung"}[lvl]
        extra = f" · {len(why)} hinweise ▾" if len(why) > 1 else ""
        self.guard_head.set_child(tui.hbox(tui.label(glyph, "ml-gl"),
                                           tui.label(f"{head}: {top[1]}" if lvl != "ok" else top[1],
                                                     "ml-reason", ellipsize=True, hexpand=True),
                                           tui.label(extra, "ml-meta"), spacing=8))
        for l, text in sorted(why, key=lambda w: -guard.ORDER[w[0]]):
            lab = tui.label(("● " if l in ("mittel", "hoch") else "· ") + text, "ml-reason", wrap=True)
            if l == "hoch":
                lab.add_css_class("hoch")
            self.guard_list.append(lab)
        self.guard_rev.set_reveal_child(lvl == "hoch")

    def _thread(self, r):
        tui.clear(self.thread_box)
        if not r["thread"]:
            return
        others = [m for m in self.st.thread(r["thread"]) if m["id"] != r["id"] and " TRASH " not in m["labels"]]
        if not others:
            return
        from window import short_date
        rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN, transition_duration=120)
        lst = tui.vbox(spacing=0)
        for m in others[-12:]:
            b = Gtk.Button(focus_on_click=False)
            b.add_css_class("ml-place")
            who = "ich" if m["from_addr"] == self.st.me else (m["from_name"] or m["from_addr"])
            b.set_child(tui.hbox(tui.label(who[:22], "ml-meta"), tui.label(m["snippet"] or "", ellipsize=True,
                                                                           hexpand=True),
                                 tui.label(short_date(m["date"]), "ml-meta"), spacing=8))
            b.connect("clicked", lambda *_, i=m["id"]: self.jump(i))
            lst.append(b)
        rev.set_child(lst)
        head = Gtk.Button(focus_on_click=False, halign=Gtk.Align.START)
        head.add_css_class("ml-link")
        head.set_label(f"\U000F0369 unterhaltung · {len(others) + 1} mails ▾")
        head.connect("clicked", lambda *_: rev.set_reveal_child(not rev.get_reveal_child()))
        self.thread_box.append(head)
        self.thread_box.append(rev)

    def jump(self, mid):
        ids = [self.win.sorted.get_item(i).id for i in range(self.win.sorted.get_n_items())]
        if mid in ids:
            self.win.select_ids({mid}, scroll=True)
        else:
            self.show(mid)

    def _attachments(self):
        tui.clear(self.att_box)
        for i, (name, ctype, size, part) in enumerate(self.mail.attachments):
            b = Gtk.Button(focus_on_click=False, tooltip_text=f"{name} · {ctype}")
            b.add_css_class("ml-chip")
            from window import human_size
            risky = name.lower().endswith(mime.RISKY_EXT)
            if risky:
                b.add_css_class("warn")
            b.set_child(tui.hbox(tui.label(type_glyph(name, ctype), "accent"),
                                 tui.label(name[:40], ellipsize=True),
                                 tui.label(human_size(size), "ml-meta"), spacing=6))
            b.connect("clicked", lambda btn, i=i: self.att_menu(btn, i))
            self.att_box.append(b)

    def _prefetch_next(self):
        """Die nächsten paar ungelesenen Mails schon im Hintergrund laden (Pfeil runter = sofort)."""
        if not self.st.backend.logged_in():
            return
        pos = self.win._first_selected_pos()
        if pos is None:
            return
        n = self.win.sorted.get_n_items()
        ids = [self.win.sorted.get_item(i).id for i in range(pos + 1, min(n, pos + 4))]
        for mid in ids:
            if self.st.body(mid) is None:
                _pool.submit(_quiet_fetch, self.st, mid)

    # ================================================================ Mehrfachauswahl
    def show_bulk(self, ids):
        import pages
        from window import human_size, num
        self._reset_images()
        self.mid = None
        self.mail = None
        tui.clear(self.bulk)
        rows = [r for r in (self.st.message(i) for i in ids[:5000]) if r]
        size = sum(r["size"] or 0 for r in rows)
        self.bulk.append(tui.label(f"{num(len(ids))} mails", "ml-bulkcount", xalign=0.5))
        senders = {r["from_addr"] for r in rows}
        sub = f"{human_size(size)} · {len(senders)} {'absender' if len(senders) != 1 else 'absender'}"
        self.bulk.append(tui.label(sub, "ml-meta", xalign=0.5))
        in_trash = self.win.cur.loc[0] == "folder" and self.win.cur.loc[1] == "TRASH"
        unread = any(r["unread"] for r in rows)
        grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=3, min_children_per_line=2,
                           row_spacing=8, column_spacing=8, homogeneous=True, halign=Gtk.Align.CENTER)

        def big(glyph, text, cb, danger=False, confirm=False):
            b = pages.BigButton(glyph, text, cb, danger=danger, confirm=confirm)
            grid.append(b)
        if in_trash:
            big("\U000F0450", "wiederherstellen", lambda: self.win.restore(ids))
            big("\U000F0A7A", f"{num(len(ids))} endgültig löschen", lambda: self.win.apply(
                {"op": "delete", "ids": ids}, f"{num(len(ids))} endgültig gelöscht", undoable=False),
                danger=True, confirm=True)
        else:
            big("\U000F0A7A", f"{num(len(ids))} löschen", lambda: self.win.trash(ids), danger=True)
            big("\U000F003C", "archivieren", lambda: self.win.archive(ids))
            big("\U000F06EE" if unread else "\U000F06EF", "gelesen" if unread else "ungelesen",
                lambda: self.win.mark(ids, read=unread))
            big("\U000F04CE", "markieren", lambda: self.win.star(ids, True))
            big("\U000F0027", "spam", lambda: self.win.spam(ids))
            big("\U000F05E8", "endgültig löschen", lambda: self.win.apply(
                {"op": "delete", "ids": ids}, f"{num(len(ids))} endgültig gelöscht", undoable=False),
                danger=True, confirm=True)
        self.bulk.append(grid)
        if len(senders) == 1 and rows:
            addr = rows[0]["from_addr"]
            allids = self.st.ids_from(addr)
            if len(allids) > len(ids):
                b = Gtk.Button(label=f"alle {num(len(allids))} mails von {addr} löschen", halign=Gtk.Align.CENTER)
                b.add_css_class("ml-del")
                b.connect("clicked", lambda *_: self.win.trash(allids))
                self.bulk.append(b)
        self.bulk.append(tui.label("strg+z macht löschen/archivieren rückgängig · strg+a wählt alles",
                                   "ml-meta", xalign=0.5))
        self.stack.set_visible_child_name("bulk")
        self.win.read_panel.set_title(f"{len(ids)} ausgewählt")

    # ================================================================ Text: Links
    def _offset_at(self, x, y):
        bx, by = self.text.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, int(x), int(y))
        ok, it = self.text.get_iter_at_location(bx, by)
        return it.get_offset() if ok else None

    def _href_at(self, off):
        if off is None:
            return None
        for s, e, href in self.links:
            if s <= off < e:
                return href
        return None

    def _text_motion(self, _c, x, y):
        self._set_hover(self._href_at(self._offset_at(x, y)))

    def _set_hover(self, href):
        if href == self._hover:
            return
        self._hover = href
        self.text.set_cursor_from_name("pointer" if href else "text")
        if href:
            warn = self.link_warn.get(href)
            self.win.say(("⚠ " + warn + " · " if warn else "\U000F0337 ") + href[:240], seconds=4)

    def _text_click(self, g, n, x, y):
        if n > 1 or self.buf.get_has_selection():
            return
        href = self._href_at(self._offset_at(x, y))
        if href:
            self.open_link(href)

    def open_link(self, href):
        import pages
        if href.lower().startswith("mailto:"):
            self.win.app.compose(href)
            return
        if not re.match(r"^https?://", href, re.I):
            self.win.say(f"link nicht geöffnet (unbekannte art): {href[:80]}", error=True)
            return
        warn = self.link_warn.get(href)
        p = self.win.prefs
        if warn and p.get("sec_link_confirm", True):
            pages.confirm(self.win, "verdächtiger link", f"{warn}\n\nziel: {href[:300]}", "trotzdem öffnen",
                          lambda: Gtk.UriLauncher(uri=href).launch(self.win, None, None))
            return
        if p.get("sec_all_links", False) and not href.startswith("mailto:"):
            pages.confirm(self.win, "link öffnen?", f"ziel: {href[:300]}", "öffnen",
                          lambda: Gtk.UriLauncher(uri=href).launch(self.win, None, None), danger=False)
            return
        Gtk.UriLauncher(uri=href).launch(self.win, None, None)

    # ================================================================ Anhänge
    def att_menu(self, btn, i):
        import pages
        name = self.mail.attachments[i][0]
        m = pages.Menu(btn)
        m.item("öffnen", lambda: self.open_att(i))
        m.item("in downloads speichern", lambda: self.save_att(i))
        m.item("speichern & im ordner zeigen", lambda: self.save_att(i, show=True))
        if len(self.mail.attachments) > 1:
            m.item(f"alle {len(self.mail.attachments)} speichern", self.save_all)
        ctype = self.mail.attachments[i][1]
        if pgp.available() and (ctype == "application/pgp-keys" or name.lower().endswith((".asc", ".pgp", ".gpg"))
                                and b"PUBLIC KEY BLOCK" in (self._att_bytes(i) or b"")[:200]):
            m.item("\U000F0306 openpgp-schlüssel übernehmen", lambda: self.import_key(i))
        m.popup()

    def import_key(self, i):
        data = self._att_bytes(i)

        def done(f):
            try:
                n, text = f.result()
            except Exception as e:
                n, text = 0, str(e)
            GLib.idle_add(lambda: (self.win.say(f"{n} schlüssel übernommen" if n else f"nichts übernommen: {text[-120:]}",
                                                error=not n), False)[1])
        _pool.submit(pgp.import_keys, data).add_done_callback(done)

    def _att_bytes(self, i):
        return self.mail.payload(self.mail.attachments[i][3])

    def save_att(self, i, show=False):
        name = self.mail.attachments[i][0]
        path = unique_path(downloads(), name)
        try:
            with open(path, "wb") as f:
                f.write(self._att_bytes(i))
        except OSError as e:
            self.win.say(f"speichern ging nicht: {e}", error=True)
            return None
        self.win.say(f"gespeichert: {path.replace(os.path.expanduser('~'), '~')}")
        if show:
            try:
                Gtk.FileLauncher(file=Gio.File.new_for_path(path)).open_containing_folder(self.win, None, None)
            except Exception:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        return path

    def save_all(self):
        for i in range(len(self.mail.attachments)):
            self.save_att(i)
        self.win.say(f"{len(self.mail.attachments)} anhänge in downloads gespeichert")

    def open_att(self, i):
        import pages
        name = self.mail.attachments[i][0]

        def go():
            folder = os.path.join(CACHE, "offen")
            os.makedirs(folder, exist_ok=True)
            path = unique_path(folder, name)
            with open(path, "wb") as f:
                f.write(self._att_bytes(i))
            Gtk.FileLauncher(file=Gio.File.new_for_path(path)).launch(self.win, None, None)
        if name.lower().endswith(mime.RISKY_EXT) and self.win.prefs.get("sec_risky_att", True):
            pages.confirm(self.win, "anhang öffnen?", f"„{name}“ ist ein Dateityp, mit dem oft Schadsoftware kommt. "
                          "Nur öffnen, wenn du ihn erwartet hast.", "trotzdem öffnen", go)
        else:
            go()

    # ================================================================ Weiteres
    def more_menu(self, btn):
        import pages
        if not self.row:
            return
        m = pages.Menu(btn)
        m.item("quelltext / kopfzeilen", self.show_source)
        m.item("als .eml speichern", self.save_eml)
        m.item("im browser öffnen (ohne externe inhalte)", self.open_browser)
        try:
            unsub = json.loads(self.row["hdr"] or "{}").get("list-unsubscribe")
        except ValueError:
            unsub = None
        if unsub:
            m.item("newsletter abbestellen", lambda: self.unsubscribe(unsub))
        if self.mid:
            m.sep()
            addr = self.row["from_addr"]
            if addr:
                n = len(self.st.ids_from(addr))
                m.item(f"alle von diesem absender zeigen ({n})",
                       lambda: self.win.go(("sender", addr, self.row["from_name"] or addr)))
                m.item(f"alle {n} von diesem absender löschen", lambda: self.win.trash(self.st.ids_from(addr)),
                       danger=True)
            m.item("als spam melden", lambda: self.win.spam([self.mid]))
            m.item("endgültig löschen", lambda: self.win.delete_forever([self.mid]), danger=True)
        m.popup()

    def show_source(self):
        import pages
        if not self.mail:
            return
        pages.text_window(self.win, "kopfzeilen", self.mail.headers_text())

    def save_eml(self):
        if not self.mail:
            return
        path = unique_path(downloads(), (self.row["subject"] or "mail")[:60] + ".eml")
        with open(path, "wb") as f:
            f.write(self.mail.raw)
        self.win.say(f"gespeichert: {path.replace(os.path.expanduser('~'), '~')}")

    def open_browser(self):
        if not self.mail:
            return
        fd, path = tempfile.mkstemp(prefix="mailist-", suffix=".html", dir=_tmpdir())
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(mime.html_page(self.mail))
        Gtk.FileLauncher(file=Gio.File.new_for_path(path)).launch(self.win, None, None)

    def unsubscribe(self, header):
        import pages
        urls = re.findall(r"<([^>]+)>", header) or [header.strip()]
        web = next((u for u in urls if u.lower().startswith("https://")), None)
        mail = next((u for u in urls if u.lower().startswith("mailto:")), None)
        if web:
            pages.confirm(self.win, "newsletter abbestellen", f"öffnet die abmeldeseite des absenders:\n"
                          f"{mime.host_of(web)}", "öffnen", lambda: Gtk.UriLauncher(uri=web).launch(self.win, None, None),
                          danger=False)
        elif mail:
            self.win.app.compose(mail)
        else:
            self.win.say("keine abmelde-adresse gefunden")

    def reply(self, mode):
        import compose
        if not self.row or not self.mail:
            if self.row and self.mid:
                self.win.say("text lädt noch …")
            return
        compose.ComposeWindow(self.win.app, mode=mode, row=self.row, mail=self.mail).present()

    def edit_draft(self):
        import compose
        if self.mail and self.row:
            compose.ComposeWindow(self.win.app, mode="draft", row=self.row, mail=self.mail).present()

    def page(self, d):
        adj = self.scroll.get_vadjustment()
        adj.set_value(adj.get_value() + d * adj.get_page_size() * 0.85)

    def focus_text(self):
        self.text.grab_focus()

    def focused(self):
        f = self.win.get_focus()
        while f is not None:
            if f is self:
                return True
            f = f.get_parent()
        return False


def _quiet_fetch(st, mid):
    try:
        fetch_raw(st, mid)
    except Exception:
        pass


def _tmpdir():
    d = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), "mailist")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _alpha(rgba, a):
    c = rgba.copy()
    c.alpha = a
    return c

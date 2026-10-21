# Formatierter Text im Schreibfenster: Leiste + Tastenkürzel über einem Gtk.TextView, Ausgabe als HTML
# (und Klartext für Programme ohne HTML). Formatierung hängt als TextTag am Text; Listen sind echte
# Zeilenanfänge („• “, „1. “), damit sie auch im Klartext stimmen.
import html, re

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk, Pango

import tui

G_BOLD, G_ITALIC, G_UNDER, G_STRIKE = "\U000F0264", "\U000F0277", "\U000F0287", "\U000F0280"
G_SIZE, G_FONT, G_COLOR, G_MARK = "\U000F027F", "\U000F06D6", "\U000F069E", "\U000F0E31"
G_BULLET, G_NUMBER, G_QUOTE, G_CENTER = "\U000F0279", "\U000F027B", "\U000F027E", "\U000F0260"
G_LINK, G_CLEAR = "\U000F0337", "\U000F0265"

INLINE = {          # name → (TextTag-Eigenschaften, HTML-Tag oder CSS)
    "b": ({"weight": Pango.Weight.BOLD}, "b"),
    "i": ({"style": Pango.Style.ITALIC}, "i"),
    "u": ({"underline": Pango.Underline.SINGLE}, "u"),
    "s": ({"strikethrough": True}, "s"),
}
SIZES = [("sz-s", "klein", 0.85, "small"), (None, "normal", 1.0, None), ("sz-l", "groß", 1.25, "large"),
         ("sz-h", "überschrift", 1.6, "x-large")]
FONTS = [(None, "standard", None, None), ("ff-serif", "mit serifen", "Serif", "Georgia, serif"),
         ("ff-mono", "schreibmaschine", "Monospace", "monospace")]
COLORS = [("fg-red", "rot", "#d93025"), ("fg-orange", "orange", "#e8710a"), ("fg-green", "grün", "#188038"),
          ("fg-blue", "blau", "#1a73e8"), ("fg-purple", "lila", "#9334e6"), ("fg-gray", "grau", "#80868b")]
MARK_BG, MARK_FG = "#fde293", "#202124"
GROUPS = {"size": [s[0] for s in SIZES if s[0]], "font": [f[0] for f in FONTS if f[0]],
          "color": [c[0] for c in COLORS]}
PARA = ("center", "quote")
BULLET = "• "
NUM_RE = re.compile(r"^(\d+)\. ")


def _line_iter(buf, n):
    r = buf.get_iter_at_line(n)
    return r[1] if isinstance(r, tuple) else r


class Formatter:
    """bar = die Leiste fürs Fenster; key(kv, state) für Tastenkürzel; used()/to_html()/to_plain() fürs Senden."""

    def __init__(self, view, parent):
        self.view, self.parent = view, parent
        self.buf = view.get_buffer()
        self.active = set()           # gilt für das, was als Nächstes getippt wird
        self.links = {}               # Tag → Adresse
        self.quiet = False
        self.para_pending = set()     # Absatz-Formate der leeren letzten Zeile (dort gibt es kein Zeichen dafür)
        self.btns = {}
        tt = self.buf.get_tag_table()
        self.tags = {}
        for name, (props, _h) in INLINE.items():
            self.tags[name] = self._tag(name, **props)
        for key, _n, scale, _c in SIZES:
            if key:
                extra = {"weight": Pango.Weight.BOLD} if key == "sz-h" else {}
                self.tags[key] = self._tag(key, scale=scale, **extra)
        for key, _n, fam, _c in FONTS:
            if key:
                self.tags[key] = self._tag(key, family=fam)
        for key, _n, col in COLORS:
            self.tags[key] = self._tag(key, foreground=col)
        self.tags["hl"] = self._tag("hl", background=MARK_BG, foreground=MARK_FG)
        self.tags["center"] = self._tag("center", justification=Gtk.Justification.CENTER)
        self.tags["quote"] = self._tag("quote", left_margin=22, style=Pango.Style.ITALIC, foreground="#9aa0a6")
        self.tt = tt
        self.buf.connect_after("insert-text", self._inserted)
        self.buf.connect("mark-set", self._mark_set)
        self.bar = self._build_bar()

    def _tag(self, name, **props):
        t = Gtk.TextTag(name="rt-" + name, **props)
        self.buf.get_tag_table().add(t)
        return t

    # ---------------- Leiste
    def _build_bar(self):
        bar = tui.hbox(spacing=2)
        bar.add_css_class("ml-fmtbar")

        def btn(key, glyph, tip, cb, toggle=True):
            b = Gtk.Button(label=glyph, tooltip_text=tip, focus_on_click=False)
            b.add_css_class("ml-fmt")
            b.connect("clicked", lambda *_: (cb(), self.view.grab_focus()))
            bar.append(b)
            if toggle:
                self.btns[key] = b
            return b

        def sep():
            s = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
            s.add_css_class("ml-fmtsep")
            bar.append(s)
        btn("b", G_BOLD, "Fett (Strg+B)", lambda: self.toggle("b"))
        btn("i", G_ITALIC, "Kursiv (Strg+I)", lambda: self.toggle("i"))
        btn("u", G_UNDER, "Unterstrichen (Strg+U)", lambda: self.toggle("u"))
        btn("s", G_STRIKE, "Durchgestrichen (Strg+Shift+X)", lambda: self.toggle("s"))
        sep()
        self.size_btn = btn("size", G_SIZE, "Schriftgröße", lambda: self._menu(self.size_btn, "size"))
        self.font_btn = btn("font", G_FONT, "Schriftart", lambda: self._menu(self.font_btn, "font"))
        self.color_btn = btn("color", G_COLOR, "Textfarbe", lambda: self._colors(self.color_btn))
        btn("hl", G_MARK, "Hervorheben (Textmarker)", lambda: self.toggle("hl"))
        sep()
        btn("bullet", G_BULLET, "Aufzählung (Strg+Shift+8)", lambda: self.list_toggle(False))
        btn("number", G_NUMBER, "Nummerierte Liste (Strg+Shift+7)", lambda: self.list_toggle(True))
        btn("quote", G_QUOTE, "Zitat (Strg+Shift+9)", lambda: self.para_toggle("quote"))
        btn("center", G_CENTER, "Zentrieren (Strg+Shift+E)", lambda: self.para_toggle("center"))
        sep()
        btn("link", G_LINK, "Link einfügen (Strg+K)", self.ask_link)
        btn("clear", G_CLEAR, "Formatierung entfernen (Strg+Leertaste)", self.clear, toggle=False)
        return bar

    def _menu(self, widget, group):
        import pages
        m = pages.Menu(widget)
        items = SIZES if group == "size" else FONTS
        cur = self._group_value(group)
        for key, name, *_ in items:
            m.item(("● " if key == cur else "   ") + name, lambda k=key: self.set_group(group, k))
        m.popup()

    def _colors(self, widget):
        pop = Gtk.Popover(has_arrow=False)
        pop.add_css_class("ml-menu")
        pop.set_parent(widget)
        pop.connect("closed", lambda p: GLib.idle_add(lambda: (p.unparent(), False)[1]))
        row = tui.hbox(spacing=4)

        def swatch(key, name, col):
            b = Gtk.Button(tooltip_text=name, focus_on_click=False)
            b.add_css_class("ml-swatch")
            da = Gtk.DrawingArea(content_width=18, content_height=18)

            def draw(_a, cr, w, h):
                if col:
                    rgba = Gdk.RGBA()
                    rgba.parse(col)
                    cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, 1)
                    cr.rectangle(0, 0, w, h)
                    cr.fill()
                else:                       # Standardfarbe: durchgestrichenes Feld
                    cr.set_source_rgba(0.6, 0.6, 0.6, 1)
                    cr.set_line_width(1.5)
                    cr.rectangle(1, 1, w - 2, h - 2)
                    cr.move_to(1, h - 1)
                    cr.line_to(w - 1, 1)
                    cr.stroke()
            da.set_draw_func(draw)
            b.set_child(da)
            b.connect("clicked", lambda *_: (pop.popdown(), self.set_group("color", key), self.view.grab_focus()))
            row.append(b)
        swatch(None, "standardfarbe", None)
        for key, name, col in COLORS:
            swatch(key, name, col)
        pop.set_child(row)
        pop.popup()

    # ---------------- Zustand
    def _sel(self):
        r = self.buf.get_selection_bounds()
        return (r[0], r[1]) if r else None

    def _tags_at_cursor(self):
        it = self.buf.get_iter_at_mark(self.buf.get_insert())
        if it.starts_line() and not it.ends_line():
            src = it                                   # Zeilenanfang: wie das Folgende
        elif it.get_offset() > 0:
            src = it.copy()
            src.backward_char()
        else:
            src = it
        return {t.props.name[3:] for t in src.get_tags() if (t.props.name or "").startswith("rt-")} - set(PARA)

    def _mark_set(self, _buf, _it, mark):
        if mark is self.buf.get_insert() and not self.quiet:
            self.active = self._tags_at_cursor()
            self._sync_buttons()

    def _sync_buttons(self):
        sel = self._sel()
        for key, b in self.btns.items():
            if key in INLINE or key == "hl":
                on = self._all_have(key, *sel) if sel else key in self.active
            elif key == "size":
                on = self._group_value("size") is not None
            elif key == "font":
                on = self._group_value("font") is not None
            elif key == "color":
                on = self._group_value("color") is not None
            elif key in PARA:
                on = self._line_has(key)
            elif key == "bullet":
                on = self._line_text().startswith(BULLET)
            elif key == "number":
                on = bool(NUM_RE.match(self._line_text()))
            elif key == "link":
                on = self._link_at() is not None
            else:
                on = False
            (b.add_css_class if on else b.remove_css_class)("on")

    def _group_value(self, group):
        sel = self._sel()
        for k in GROUPS[group]:
            if (self._all_have(k, *sel) if sel else k in self.active):
                return k
        return None

    def _all_have(self, key, a, b):
        tag = self.tags[key]
        it = a.copy()
        if not it.has_tag(tag):
            return False
        it.forward_to_tag_toggle(tag)
        return it.compare(b) >= 0

    # ---------------- Zeichen-Formate
    def toggle(self, key):
        sel = self._sel()
        tag = self.tags[key]
        if sel:
            if self._all_have(key, *sel):
                self.buf.remove_tag(tag, *sel)
            else:
                self.buf.apply_tag(tag, *sel)
        else:
            self.active ^= {key}
        self._sync_buttons()

    def set_group(self, group, key):
        sel = self._sel()
        if sel:
            for k in GROUPS[group]:
                self.buf.remove_tag(self.tags[k], *sel)
            if key:
                self.buf.apply_tag(self.tags[key], *sel)
        else:
            self.active -= set(GROUPS[group])
            if key:
                self.active.add(key)
        self._sync_buttons()

    def clear(self):
        sel = self._sel()
        if sel:
            for name, t in self.tags.items():
                if name not in PARA:
                    self.buf.remove_tag(t, *sel)
            for t in list(self.links):
                self.buf.remove_tag(t, *sel)
        self.active = set()
        self._sync_buttons()

    def _inserted(self, buf, end, text, _n):
        if self.quiet:
            return
        start = end.copy()
        start.backward_chars(len(text))
        for k in self.active:
            if k in self.tags:
                buf.apply_tag(self.tags[k], start, end)
        # Absatz-Formate gelten für die ganze Zeile
        ls = start.copy()
        ls.set_line_offset(0)
        for k in PARA:
            t = self.tags[k]
            if ls.equal(start):
                on = (k in self.para_pending) if end.is_end() else end.has_tag(t)
            else:
                on = self._before(start).has_tag(t)
            le = end.copy()
            if not le.ends_line():
                le.forward_to_line_end()
            (buf.apply_tag if on else buf.remove_tag)(t, ls, le)
        if text.endswith("\n") and end.is_end():
            # neue leere letzte Zeile erbt die Absatz-Formate der Zeile davor
            self.para_pending = {k for k in PARA if self._before(end).has_tag(self.tags[k])}

    @staticmethod
    def _before(it):
        b = it.copy()
        b.backward_char()
        return b

    # ---------------- Absätze
    def _lines(self):
        sel = self._sel()
        if sel:
            a, b = sel
            return range(a.get_line(), b.get_line() + (0 if b.starts_line() and b.get_line() > a.get_line() else 1))
        n = self.buf.get_iter_at_mark(self.buf.get_insert()).get_line()
        return range(n, n + 1)

    def _line_text(self, n=None):
        if n is None:
            n = self.buf.get_iter_at_mark(self.buf.get_insert()).get_line()
        a = _line_iter(self.buf, n)
        b = a.copy()
        if not b.ends_line():
            b.forward_to_line_end()
        return self.buf.get_text(a, b, True)

    def _para_on(self, n, key):
        a = _line_iter(self.buf, n)
        if a.is_end():
            return key in self.para_pending
        return a.has_tag(self.tags[key])

    def _line_has(self, key):
        return self._para_on(self.buf.get_iter_at_mark(self.buf.get_insert()).get_line(), key)

    def _line_bounds(self, n):
        a = _line_iter(self.buf, n)
        b = a.copy()
        if not b.ends_line():
            b.forward_to_line_end()
        if not b.is_end():
            b.forward_char()            # Zeilenumbruch mitnehmen (leere Zeilen behalten ihr Format)
        return a, b

    def para_toggle(self, key):
        lines = self._lines()
        tag = self.tags[key]
        on = not self._para_on(lines[0], key)
        for n in lines:
            a, b = self._line_bounds(n)
            if a.equal(b):              # leere letzte Zeile: gilt fürs Tippen dort
                (self.para_pending.add if on else self.para_pending.discard)(key)
                continue
            (self.buf.apply_tag if on else self.buf.remove_tag)(tag, a, b)
        self._sync_buttons()

    def list_toggle(self, numbered):
        lines = list(self._lines())
        texts = [self._line_text(n) for n in lines]
        has = all((NUM_RE.match(t) if numbered else t.startswith(BULLET)) for t in texts)
        self.buf.begin_user_action()
        for idx, n in enumerate(lines):
            t = texts[idx]
            a = _line_iter(self.buf, n)
            # alten Listenanfang (egal welcher Art) entfernen
            m = NUM_RE.match(t)
            old = len(BULLET) if t.startswith(BULLET) else (len(m.group(0)) if m else 0)
            if old:
                b = a.copy()
                b.forward_chars(old)
                self.buf.delete(a, b)
                a = _line_iter(self.buf, n)
            if not has:
                self.quiet = True
                self.buf.insert(a, f"{idx + 1}. " if numbered else BULLET)
                self.quiet = False
        self.buf.end_user_action()
        if numbered:
            self.renumber(lines[0])
        self._sync_buttons()

    def renumber(self, around):
        """Nummern einer zusammenhängenden Liste neu zählen."""
        n = around
        while n > 0 and NUM_RE.match(self._line_text(n - 1)):
            n -= 1
        k = 1
        total = self.buf.get_line_count()
        self.quiet = True
        while n < total:
            t = self._line_text(n)
            m = NUM_RE.match(t)
            if not m:
                break
            if int(m.group(1)) != k:
                a = _line_iter(self.buf, n)
                b = a.copy()
                b.forward_chars(len(m.group(0)))
                self.buf.delete(a, b)
                self.buf.insert(_line_iter(self.buf, n), f"{k}. ")
            k += 1
            n += 1
        self.quiet = False

    def enter(self):
        """Enter in einer Listenzeile: nächster Punkt; in einer leeren Listenzeile: Liste beenden."""
        it = self.buf.get_iter_at_mark(self.buf.get_insert())
        if self.buf.get_has_selection():
            return False
        t = self._line_text(it.get_line())
        m = NUM_RE.match(t)
        prefix = BULLET if t.startswith(BULLET) else (m.group(0) if m else None)
        if not prefix:
            return False
        if it.get_line_offset() < len(prefix):
            return False
        a = _line_iter(self.buf, it.get_line())
        if t == prefix:                               # leerer Punkt → Liste zu Ende
            b = a.copy()
            b.forward_chars(len(prefix))
            self.buf.delete(a, b)
            return True
        nxt = BULLET if prefix == BULLET else f"{int(m.group(1)) + 1}. "
        self.buf.insert_at_cursor("\n")
        self.quiet = True
        self.buf.insert_at_cursor(nxt)
        self.quiet = False
        if prefix != BULLET:
            self.renumber(self.buf.get_iter_at_mark(self.buf.get_insert()).get_line())
        return True

    # ---------------- Links
    def _link_at(self, it=None):
        it = it or self.buf.get_iter_at_mark(self.buf.get_insert())
        for t in it.get_tags():
            if t in self.links:
                return t
        return None

    def ask_link(self):
        sel = self._sel()
        cur = self._link_at()
        dlg = Gtk.Window(transient_for=self.parent, modal=True, title="link", resizable=False)
        dlg.add_css_class("ml")
        dlg.add_css_class("tui")
        getattr(__import__("glass"), "frame_window", lambda w: w.set_decorated(False))(dlg)
        url = tui.Entry("https://…", text=self.links.get(cur, ""))
        text = tui.Entry("angezeigter text (leer = adresse)")
        if sel:
            text.set_text(self.buf.get_text(*sel, True))
        box = tui.vbox(tui.label("link einfügen", "accent"), url, text, spacing=8)
        row = tui.hbox(spacing=6)
        row.append(tui.label("", hexpand=True))
        if cur:
            row.append(tui.Button("link entfernen", lambda: (self._unlink(cur), dlg.close())))
        row.append(tui.Button("abbrechen", dlg.close))
        ok = tui.Button("einfügen", lambda: done())
        ok.add_css_class("on")
        row.append(ok)
        box.append(row)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(16)
        box.set_size_request(380, -1)
        dlg.set_child(box)

        def done():
            u = url.get_text().strip()
            if not u:
                dlg.close()
                return
            if not re.match(r"^[a-z][a-z0-9+.-]*:", u, re.I):
                u = ("mailto:" + u) if "@" in u and "/" not in u else "https://" + u
            label = text.get_text() or u
            tag = Gtk.TextTag(underline=Pango.Underline.SINGLE, foreground="#4c8df6")
            self.buf.get_tag_table().add(tag)
            self.links[tag] = u
            self.buf.begin_user_action()
            if sel and self.buf.get_has_selection():
                a, b = self.buf.get_selection_bounds()
                if self.buf.get_text(a, b, True) != label:
                    self.buf.delete(a, b)
                    sel_ = None
                else:
                    sel_ = (a, b)
            else:
                sel_ = None
            if sel_ is None:
                it = self.buf.get_iter_at_mark(self.buf.get_insert())
                off = it.get_offset()
                self.quiet = True
                self.buf.insert(it, label)
                self.quiet = False
                a = self.buf.get_iter_at_offset(off)
                b = self.buf.get_iter_at_offset(off + len(label))
                for k in self.active:
                    self.buf.apply_tag(self.tags[k], a, b)
            else:
                a, b = sel_
            self.buf.apply_tag(tag, a, b)
            self.buf.end_user_action()
            self.buf.place_cursor(self.buf.get_iter_at_offset(b.get_offset()))
            dlg.close()
        url.connect("activate", lambda *_: done())
        text.connect("activate", lambda *_: done())
        k = Gtk.EventControllerKey()
        k.connect("key-pressed", lambda _c, kv, *_: (dlg.close(), True)[1] if kv == Gdk.KEY_Escape else False)
        dlg.add_controller(k)
        dlg.connect("close-request", lambda *_: (GLib.idle_add(lambda: (self.view.grab_focus(), False)[1]), False)[1])
        dlg.present()
        url.grab_focus()

    def _unlink(self, tag):
        self.buf.remove_tag(tag, self.buf.get_start_iter(), self.buf.get_end_iter())
        self.links.pop(tag, None)
        self._sync_buttons()

    # ---------------- Tasten
    def key(self, kv, state):
        if not self.view.has_focus():
            return False
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if not ctrl:
            if kv in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not shift:
                return self.enter()
            return False
        k = Gdk.keyval_to_lower(kv)
        if not shift:
            acts = {Gdk.KEY_b: lambda: self.toggle("b"), Gdk.KEY_i: lambda: self.toggle("i"),
                    Gdk.KEY_u: lambda: self.toggle("u"), Gdk.KEY_k: self.ask_link, Gdk.KEY_space: self.clear}
        else:
            acts = {Gdk.KEY_x: lambda: self.toggle("s"), Gdk.KEY_e: lambda: self.para_toggle("center"),
                    Gdk.KEY_8: lambda: self.list_toggle(False), Gdk.KEY_asterisk: lambda: self.list_toggle(False),
                    Gdk.KEY_parenleft: lambda: self.para_toggle("quote"), Gdk.KEY_9: lambda: self.para_toggle("quote"),
                    Gdk.KEY_7: lambda: self.list_toggle(True), Gdk.KEY_slash: lambda: self.list_toggle(True)}
        if k in acts:
            acts[k]()
            return True
        return False

    # ---------------- Ausgabe
    def used(self):
        """Gibt es überhaupt Formatierung? (sonst bleibt die Mail reiner Text)"""
        it = self.buf.get_start_iter()
        return bool(it.get_tags()) or it.forward_to_tag_toggle(None)

    def _segments(self):
        """→ Zeilen: [[(text, {namen}, link|None), …], …]"""
        lines, cur = [], []
        it = self.buf.get_start_iter()
        end = self.buf.get_end_iter()
        while not it.equal(end):
            nxt = it.copy()
            if not nxt.forward_to_tag_toggle(None):
                nxt = end.copy()
            text = self.buf.get_text(it, nxt, True)
            names, link = set(), None
            for t in it.get_tags():
                if t in self.links:
                    link = self.links[t]
                elif (t.props.name or "").startswith("rt-"):
                    names.add(t.props.name[3:])
            parts = text.split("\n")
            for j, p in enumerate(parts):
                if p:
                    cur.append((p, names, link))
                if j < len(parts) - 1:
                    lines.append(cur)
                    cur = []
            it = nxt
        lines.append(cur)
        return lines

    def to_plain(self):
        out = []
        for line in self._segments():
            s = ""
            for text, _names, link in line:
                s += text
                if link and link.rstrip("/") not in (text.rstrip("/"), "mailto:" + text):
                    s += f" ({link.removeprefix('mailto:')})"
            out.append(s)
        return "\n".join(out)

    def to_html(self):
        rows = []                            # (art, inhalt-html, zentriert)
        for n, line in enumerate(self._segments()):
            start = _line_iter(self.buf, n) if n < self.buf.get_line_count() else None
            para = {k for k in PARA if start is not None and start.has_tag(self.tags[k])}
            text = "".join(t for t, *_ in line)
            kind, strip = "p", 0
            if text.startswith(BULLET):
                kind, strip = "ul", len(BULLET)
            elif NUM_RE.match(text):
                kind, strip = "ol", len(NUM_RE.match(text).group(0))
            elif text.startswith(">"):
                kind, strip = "gt", 2 if text.startswith("> ") else 1
            if "quote" in para and kind == "p":
                kind = "q"
            rows.append((kind, self._inline(line, strip), "center" in para))
        out, i = [], 0
        while i < len(rows):
            kind = rows[i][0]
            j = i
            while j < len(rows) and rows[j][0] == kind:
                j += 1
            grp = rows[i:j]
            if kind in ("ul", "ol"):
                out.append(f"<{kind}>" + "".join(f"<li{self._align(c)}>{h or '<br>'}</li>" for _k, h, c in grp)
                           + f"</{kind}>")
            elif kind in ("q", "gt"):
                out.append('<blockquote style="margin:0 0 0 .8ex;border-left:2px solid #ccc;padding-left:1ex">'
                           + "".join(f"<div{self._align(c)}>{h or '<br>'}</div>" for _k, h, c in grp)
                           + "</blockquote>")
            else:
                out.extend(f"<div{self._align(c)}>{h or '<br>'}</div>" for _k, h, c in grp)
            i = j
        return ('<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>'
                '<div dir="ltr" style="font-family:Arial,Helvetica,sans-serif">' + "\n".join(out) + "</div></body></html>")

    @staticmethod
    def _align(center):
        return ' style="text-align:center"' if center else ""

    @staticmethod
    def _inline(line, strip):
        out = []
        for text, names, link in line:
            if strip:
                cut = min(strip, len(text))
                text, strip = text[cut:], strip - cut
                if not text:
                    continue
            h = html.escape(text)
            for k in ("s", "u", "i", "b"):
                if k in names:
                    h = f"<{k}>{h}</{k}>"
            css = []
            for key, _n, _s, c in SIZES:
                if key in names:
                    css.append(f"font-size:{c}")
                    if key == "sz-h":
                        css.append("font-weight:bold")
            for key, _n, _f, c in FONTS:
                if key in names:
                    css.append(f"font-family:{c}")
            for key, _n, col in COLORS:
                if key in names:
                    css.append(f"color:{col}")
            if "hl" in names:
                css.append(f"background-color:{MARK_BG};color:{MARK_FG}")
            if css:
                h = f'<span style="{";".join(css)}">{h}</span>'
            if link:
                h = f'<a href="{html.escape(link, quote=True)}">{h}</a>'
            out.append(h)
        return "".join(out)

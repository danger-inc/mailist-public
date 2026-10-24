# Rechtschreibprüfung für Notizen und Words (Deutsch und Englisch, einzeln abschaltbar).
# Benutzt libhunspell direkt per ctypes (kein Python-Paket nötig); gibt es kein hunspell, wird libenchant versucht.
# Wörterbücher: sudo pacman -S hunspell hunspell-de hunspell-en_us
# Fehler bekommen eine rote Wellenlinie; Rechtsklick zeigt Vorschläge, "Zum Wörterbuch hinzufügen", "Ignorieren".
# Eigene Wörter landen in ~/.local/share/notes/data/words.txt.
import bisect, ctypes, difflib, os, re, sys

import gi
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Gio, GLib, Graphene, Gtk, Pango

import paths

PWL = paths.WORDS
INSTALL_HINT = "sudo pacman -S hunspell hunspell-de hunspell-en_us"
# Sprache → Wörterbücher in Wunschreihenfolge (das erste gefundene wird benutzt)
VARIANTS = {"de": ("de_DE", "de_DE_frami", "de_AT", "de_CH", "de"), "en": ("en_US", "en_GB", "en_CA", "en_AU", "en")}
LANG_NAMES = {"de": "Deutsch", "en": "Englisch"}
DEFAULT_LANGS = ("de", "en")
WORD = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*")
SKIP = re.compile(r"\S*(?:://|www\.|@)\S*|\S+\.(?:de|com|org|net|io|py|txt)\b")

_speller = None
_langs = list(DEFAULT_LANGS)


def dict_dirs():
    dirs = [d for d in os.environ.get("DICPATH", "").split(os.pathsep) if d]
    dirs += [os.path.expanduser("~/.local/share/hunspell"), "/usr/share/hunspell", "/usr/share/myspell",
             "/usr/share/myspell/dicts", "/usr/local/share/hunspell", "/app/share/hunspell",
             os.path.join(sys.prefix, "share", "hunspell"), "/ucrt64/share/hunspell", "/mingw64/share/hunspell"]
    out = []
    for d in dirs:
        if d not in out and os.path.isdir(d):
            out.append(d)
    return out


def find_dict(name):
    for d in dict_dirs():
        aff, dic = os.path.join(d, name + ".aff"), os.path.join(d, name + ".dic")
        if os.path.exists(aff) and os.path.exists(dic):
            return aff, dic
    return None


class _Hunspell:
    """Ein hunspell-Wörterbuch."""
    lib = None

    @classmethod
    def load(cls):
        if cls.lib is None:
            cls.lib = False
            for name in ("libhunspell-1.7.so.0", "libhunspell-1.7.so", "libhunspell.so", "libhunspell-1.6.so.0",
                         "libhunspell-1.7-0.dll", "libhunspell-1.7.0.dylib", "libhunspell.dylib"):
                try:
                    L = ctypes.CDLL(name)
                    break
                except OSError:
                    continue
            else:
                return False
            vp, cp, pp = ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_char_p))
            L.Hunspell_create.restype = vp
            L.Hunspell_create.argtypes = [cp, cp]
            L.Hunspell_get_dic_encoding.restype = cp
            L.Hunspell_get_dic_encoding.argtypes = [vp]
            L.Hunspell_spell.argtypes = [vp, cp]
            L.Hunspell_suggest.argtypes = [vp, pp, cp]
            L.Hunspell_free_list.argtypes = [vp, pp, ctypes.c_int]
            L.Hunspell_add.argtypes = [vp, cp]
            cls.lib = L
        return cls.lib

    def __init__(self, aff, dic):
        L = self.L = self.load()
        self.h = L.Hunspell_create(aff.encode(), dic.encode())
        enc = (L.Hunspell_get_dic_encoding(self.h) or b"UTF-8").decode(errors="ignore")
        try:
            "".encode(enc)
        except LookupError:
            enc = "ISO-8859-1"
        self.enc = enc

    def _b(self, word):
        try:
            return word.encode(self.enc)
        except UnicodeEncodeError:
            return None

    def check(self, word):
        b = self._b(word)
        return bool(b) and self.L.Hunspell_spell(self.h, b) != 0

    def suggest(self, word):
        b = self._b(word)
        if not b:
            return []
        lst = ctypes.POINTER(ctypes.c_char_p)()
        n = self.L.Hunspell_suggest(self.h, ctypes.byref(lst), b)
        out = [lst[i].decode(self.enc, errors="ignore") for i in range(n)]
        if n:
            self.L.Hunspell_free_list(self.h, ctypes.byref(lst), n)
        return out

    def add(self, word):
        b = self._b(word)
        if b:
            self.L.Hunspell_add(self.h, b)


class _Enchant:
    """Rückfall, wenn kein libhunspell da ist (z. B. andere Systeme mit nur enchant)."""
    lib = None
    broker = None

    @classmethod
    def load(cls):
        if cls.lib is None:
            try:
                L = ctypes.CDLL("libenchant-2.so.2")
            except OSError:
                cls.lib = False
                return False
            vp, cp = ctypes.c_void_p, ctypes.c_char_p
            L.enchant_broker_init.restype = vp
            L.enchant_broker_dict_exists.argtypes = [vp, cp]
            L.enchant_broker_request_dict.argtypes = [vp, cp]
            L.enchant_broker_request_dict.restype = vp
            L.enchant_dict_check.argtypes = [vp, cp, ctypes.c_ssize_t]
            L.enchant_dict_suggest.argtypes = [vp, cp, ctypes.c_ssize_t, ctypes.POINTER(ctypes.c_size_t)]
            L.enchant_dict_suggest.restype = ctypes.POINTER(cp)
            L.enchant_dict_free_string_list.argtypes = [vp, ctypes.POINTER(cp)]
            L.enchant_dict_add_to_session.argtypes = [vp, cp, ctypes.c_ssize_t]
            cls.lib = L
            cls.broker = L.enchant_broker_init()
        return cls.lib

    @classmethod
    def open(cls, name):
        L = cls.load()
        if not L or not L.enchant_broker_dict_exists(cls.broker, name.encode()):
            return None
        d = L.enchant_broker_request_dict(cls.broker, name.encode())
        if not d:
            return None
        obj = cls.__new__(cls)
        obj.L, obj.d = L, d
        return obj

    def check(self, word):
        b = word.encode()
        return self.L.enchant_dict_check(self.d, b, len(b)) == 0

    def suggest(self, word):
        b = word.encode()
        n = ctypes.c_size_t()
        arr = self.L.enchant_dict_suggest(self.d, b, len(b), ctypes.byref(n))
        out = []
        if arr:
            out = [arr[i].decode(errors="ignore") for i in range(n.value)]
            self.L.enchant_dict_free_string_list(self.d, arr)
        return out

    def add(self, word):
        b = word.encode()
        self.L.enchant_dict_add_to_session(self.d, b, len(b))


class Speller:
    def __init__(self, langs=DEFAULT_LANGS):
        self.dicts, self.langs, self.codes, self.ignored, self.cache = [], [], [], set(), {}
        self.own = set()
        self.backend = ""
        for code in langs:
            for name in VARIANTS.get(code, (code,)):
                d = None
                found = find_dict(name) if _Hunspell.load() else None
                if found:
                    d = _Hunspell(*found)
                    self.backend = "hunspell"
                else:
                    d = _Enchant.open(name)
                    if d:
                        self.backend = self.backend or "enchant"
                if d:
                    self.dicts.append(d)
                    self.langs.append(name)
                    self.codes.append(code)
                    break
        try:
            os.makedirs(os.path.dirname(PWL), exist_ok=True)
            if not os.path.exists(PWL):
                open(PWL, "a").close()
            with open(PWL, encoding="utf-8") as f:
                self.own = {w.strip() for w in f if w.strip() and not w.startswith("#")}
        except OSError:
            pass
        for w in self.own:                       # damit eigene Wörter auch als Vorschlag auftauchen
            for d in self.dicts:
                d.add(w)

    @property
    def available(self):
        return bool(self.dicts)

    def _own(self, word):
        return word in self.own or (word[:1].isupper() and word[:1].lower() + word[1:] in self.own)

    def check(self, word):
        if word in self.cache:
            return self.cache[word]
        ok = word in self.ignored or self._own(word) or any(d.check(word) for d in self.dicts)
        self.cache[word] = ok
        return ok

    def known(self, word):
        """Wie check(), aber ohne Zwischenspeicher-Seiteneffekte für Wörter, die nur geprüft werden."""
        return word in self.ignored or self._own(word) or any(d.check(word) for d in self.dicts)

    def suggest(self, word, limit=7):
        lists = [d.suggest(word) for d in self.dicts]
        seen, out = set(), []
        for rank in range(max((len(x) for x in lists), default=0)):     # abwechselnd aus jeder Sprache
            for x in lists:
                if rank < len(x) and x[rank] not in seen:
                    seen.add(x[rank])
                    out.append((rank, x[rank]))
        low = word.lower()
        out.sort(key=lambda p: (p[0] > 2, -difflib.SequenceMatcher(None, low, p[1].lower()).ratio(), p[0]))
        return [s for _, s in out[:limit]]

    def add(self, word):
        self.own.add(word)
        try:
            with open(PWL, "a", encoding="utf-8") as f:
                f.write(word + "\n")
        except OSError:
            pass
        for d in self.dicts:
            d.add(word)
        self.cache[word] = True

    def ignore(self, word):
        self.ignored.add(word)
        self.cache[word] = True


def set_langs(langs):
    """Welche Sprachen geprüft werden ("de", "en"). Lädt die Wörterbücher beim nächsten Zugriff neu."""
    global _langs, _speller
    langs = [c for c in (langs or []) if c in VARIANTS]
    if langs != _langs:
        _langs = langs
        _speller = None


def installed_langs():
    """Sprachen, für die überhaupt ein Wörterbuch da ist."""
    return [c for c in VARIANTS if Speller((c,)).available]


def speller():
    global _speller
    if _speller is None:
        _speller = Speller(_langs)
    return _speller


def reload_words():
    """Nach Änderungen an words.txt (Einstellungen): Wörterbücher neu laden."""
    global _speller
    _speller = None


class SpellCheck:
    """Hängt sich an eine Gtk.TextView. enabled() sagt, ob gerade geprüft werden soll."""

    def __init__(self, view, enabled, skip=None):
        """skip(text) → [(anfang, ende)]: Bereiche, die nicht geprüft werden (z. B. LaTeX-Befehle)."""
        self.view, self.enabled, self.skip = view, enabled, skip
        self._id = None
        self._attached = set()
        self.attach(view.get_buffer())
        # Rechtsklick: eigenes Menü im Suite-Stil (statt GTKs grauem Standardmenü), mit Vorschlägen obenauf
        group = Gio.SimpleActionGroup()
        for name, cb in (("replace", self.on_replace), ("add", self.on_add), ("ignore", self.on_ignore)):
            a = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            a.connect("activate", cb)
            group.add_action(a)
        view.insert_action_group("spell", group)
        click = Gtk.GestureClick(button=3)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self.on_right_click)
        view.add_controller(click)
        self.undo = None                 # (rückgängig, wiederholen) – setzt die App, wenn sie eigene hat
        self.target = None

    @property
    def sp(self):
        return speller()

    def attach(self, buf):
        """Weitere Puffer derselben TextView (LaTeX-Projekt mit mehreren Dateien)."""
        if id(buf) in self._attached:
            return
        self._attached.add(id(buf))
        buf.create_tag("spell-err", underline=Pango.Underline.ERROR, underline_rgba=self._rgba("#e5534b"))
        buf.connect("changed", lambda *_: self.queue())

    @property
    def tag(self):
        buf = self.view.get_buffer()
        t = buf.get_tag_table().lookup("spell-err")
        if t is None:
            self.attach(buf)
            t = buf.get_tag_table().lookup("spell-err")
        return t

    @staticmethod
    def _rgba(h):
        c = Gdk.RGBA()
        c.parse(h)
        return c

    def queue(self, delay=350):
        if self._id is not None:
            GLib.source_remove(self._id)
        self._id = GLib.timeout_add(delay, self.run)

    def run(self):
        self._id = None
        buf = self.view.get_buffer()
        s, e = buf.get_bounds()
        tag = self.tag
        buf.remove_tag(tag, s, e)
        if not self.enabled() or not self.sp.available:
            return False
        text = buf.get_slice(s, e, True)
        skip = [(m.start(), m.end()) for m in SKIP.finditer(text)]
        if self.skip:
            skip += self.skip(text)
        merged = []                                   # überlappende Bereiche zusammenlegen
        for a, b in sorted(skip):
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        skip = merged
        starts = [a for a, _ in skip]
        cursor = buf.get_iter_at_mark(buf.get_insert()).get_offset()
        typing = self.view.has_focus()
        for m in WORD.finditer(text):
            w = m.group(0)
            if len(w) < 2 or w.isupper():
                continue
            if typing and m.end() == cursor:            # Wort wird gerade getippt
                continue
            i = bisect.bisect_right(starts, m.start()) - 1
            if i >= 0 and m.start() < skip[i][1]:
                continue
            if not self.sp.check(w.replace("’", "'")):
                buf.apply_tag(tag, buf.get_iter_at_offset(m.start()), buf.get_iter_at_offset(m.end()))
        return False

    def word_at(self, x, y):
        bx, by = self.view.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, int(x), int(y))
        ok, it = self.view.get_iter_at_location(bx, by)
        if not ok or not it.has_tag(self.tag):
            return None
        s, e = it.copy(), it.copy()
        if not s.starts_tag(self.tag):
            s.backward_to_tag_toggle(self.tag)
        e.forward_to_tag_toggle(self.tag)
        return s.get_offset(), e.get_offset(), self.view.get_buffer().get_text(s, e, False)

    def on_right_click(self, gesture, n, x, y):
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)     # GTKs eigenes Menü nicht öffnen
        hit = self.word_at(x, y) if self.enabled() and self.sp.available else None
        self.target = hit
        view, buf = self.view, self.view.get_buffer()
        bx, by = view.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, int(x), int(y))
        ok, it = view.get_iter_at_location(bx, by)
        sel = buf.get_selection_bounds()
        if ok and not (sel and sel[0].compare(it) <= 0 <= sel[1].compare(it)):
            buf.place_cursor(it)                 # Klick außerhalb der Markierung: Cursor dorthin
        view.grab_focus()
        context_menu(view, x, y, self.menu_entries(hit))

    def menu_entries(self, hit):
        view, buf = self.view, self.view.get_buffer()
        editable = view.get_editable()
        has_sel = buf.get_has_selection()
        undo, redo = self.undo or (lambda: view.activate_action("text.undo", None),
                                   lambda: view.activate_action("text.redo", None))
        edit = [("󰕌", "rückgängig", "strg+z", undo, editable),
                ("󰑎", "wiederholen", "strg+⇧+z", redo, editable),
                ("󰆐", "ausschneiden", "strg+x", lambda: view.emit("cut-clipboard"), editable and has_sel),
                ("󰆏", "kopieren", "strg+c", lambda: view.emit("copy-clipboard"), has_sel),
                ("󰆒", "einfügen", "strg+v", lambda: view.emit("paste-clipboard"), editable),
                ("󰆴", "löschen", "entf", lambda: buf.delete_selection(True, editable), editable and has_sel),
                ("󰒆", "alles markieren", "strg+a", lambda: view.emit("select-all", True), True)]
        if hit:
            # Vorschläge zuerst, Bearbeiten als kompakte Symbolreihe darunter (Menü bleibt niedrig)
            word = hit[2]
            sugg = [("󰁔", s, "", (lambda s=s: self.on_replace(None, GLib.Variant("s", s))), True, "sugg")
                    for s in self.sp.suggest(word)[:5]]
            if not sugg:
                sugg = [("", "keine vorschläge", "", None, False)]
            sugg += [("󰓒", f"„{word}“ ins wörterbuch", "", lambda: self.on_add(None, GLib.Variant("s", word)), True),
                     ("󰈉", "ignorieren", "", lambda: self.on_ignore(None, GLib.Variant("s", word)), True)]
            return [("rechtschreibung", sugg), ("bearbeiten", "chips", edit)]
        return [(None, edit[:2]), ("", edit[2:6]), ("", edit[6:])]

    def on_replace(self, action, value):
        if not self.target:
            return
        start, end, _ = self.target
        buf = self.view.get_buffer()
        s, e = buf.get_iter_at_offset(start), buf.get_iter_at_offset(end)
        tags = [t for t in s.get_tags() if t is not self.tag]   # Formatierung des Worts behalten
        buf.begin_user_action()
        buf.place_cursor(s)
        buf.delete(s, e)
        new = value.get_string()
        buf.insert(buf.get_iter_at_offset(start), new)
        s, e = buf.get_iter_at_offset(start), buf.get_iter_at_offset(start + len(new))
        for t in tags:
            buf.apply_tag(t, s, e)
        buf.end_user_action()
        self.target = None
        self.queue(0)

    def on_add(self, action, value):
        self.sp.add(value.get_string())
        self.queue(0)

    def on_ignore(self, action, value):
        self.sp.ignore(value.get_string())
        self.queue(0)


# ---------- Rechtsklick-Menü im Suite-Stil ----------
CTX_CSS = """
popover.ctx-menu > contents { min-width: 230px; padding: 4px; margin-top: 0; }
popover.ctx-menu .fm-sec { padding: 6px 8px 2px 8px; }
popover.ctx-menu .fm-sep { background-color: alpha(@primary, 0.3); min-height: 1px; margin: 3px 6px; }
popover.ctx-menu button.fm-item { padding: 3px 8px; }
popover.ctx-menu button.fm-item.sugg { font-weight: 800; }
popover.ctx-menu .fm-chips { padding: 2px 4px 0 4px; }
popover.ctx-menu button.fm-chip { padding: 3px 0; font-size: 15px; color: @primary; }
popover.ctx-menu button.fm-chip:hover { color: @on_primary; }
popover.ctx-menu button.fm-item:disabled { color: alpha(@foreground, 0.35); }
popover.ctx-menu button.fm-item:disabled .fm-icon { color: alpha(@foreground, 0.3); }
"""
_ctx_css = False


def context_menu(widget, x, y, sections):
    """Popover an (x, y) im Stil des notist-Dateimenüs. sections: [(titel, [(symbol, text, kürzel, fn,
    aktiv[, extra-css])])] – titel None/"" = nur Trennlinie (erster Abschnitt ohne)."""
    global _ctx_css
    if not _ctx_css:
        css = Gtk.CssProvider()
        css.load_from_string(CTX_CSS) if hasattr(css, "load_from_string") else css.load_from_data(CTX_CSS.encode())
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 2)
        _ctx_css = True
    pop = Gtk.Popover(has_arrow=False, halign=Gtk.Align.START, position=Gtk.PositionType.BOTTOM)
    pop.add_css_class("file-menu")
    pop.add_css_class("ctx-menu")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
    for k, (title, *rest) in enumerate(sections):
        items = rest[-1]
        if title:
            row = Gtk.Box(spacing=8)
            row.add_css_class("fm-sec")
            lab = Gtk.Label(label=title, xalign=0)
            lab.add_css_class("fm-sec-title")
            line = Gtk.Box(hexpand=True, valign=Gtk.Align.CENTER)
            line.add_css_class("fm-line")
            row.append(lab)
            row.append(line)
            box.append(row)
        elif k:
            sep = Gtk.Box()
            sep.add_css_class("fm-sep")
            box.append(sep)
        if len(rest) > 1 and rest[0] == "chips":         # nur Symbole nebeneinander, Name als Tooltip
            row = Gtk.Box(spacing=2)
            row.add_css_class("fm-chips")
            for icon, text, hint, fn, active, *extra in items:
                b = Gtk.Button(label=icon, focus_on_click=False, sensitive=bool(active and fn),
                               tooltip_text=f"{text} ({hint})" if hint else text, hexpand=True)
                b.add_css_class("fm-item")
                b.add_css_class("fm-chip")
                if fn:
                    b.connect("clicked", lambda _b, fn=fn: (pop.popdown(), GLib.idle_add(lambda: (fn(), False)[1])))
                row.append(b)
            box.append(row)
            continue
        for icon, text, hint, fn, active, *extra in items:
            b = Gtk.Button(focus_on_click=False, sensitive=bool(active and fn))
            b.add_css_class("fm-item")
            for c in extra:
                b.add_css_class(c)
            r = Gtk.Box(spacing=10)
            i = Gtk.Label(label=icon)
            i.add_css_class("fm-icon")
            lab = Gtk.Label(label=text, xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                            max_width_chars=34)
            r.append(i)
            r.append(lab)
            if hint:
                h = Gtk.Label(label=hint)
                h.add_css_class("fm-hint")
                r.append(h)
            b.set_child(r)
            if fn:
                b.connect("clicked", lambda _b, fn=fn: (pop.popdown(), GLib.idle_add(lambda: (fn(), False)[1])))
            box.append(b)
    pop.set_child(box)
    # TextViews positionieren fremde Popover nicht (nur ihre eigenen) → an einen Kasten darüber hängen
    anchor = widget
    while isinstance(anchor, (Gtk.TextView, Gtk.ScrolledWindow, Gtk.Viewport)) and anchor.get_parent() is not None:
        anchor = anchor.get_parent()
    if anchor is not widget:
        ok, pt = widget.compute_point(anchor, Graphene.Point().init(x, y))
        if ok:
            x, y = pt.x, pt.y
    pop.set_parent(anchor)
    rect = Gdk.Rectangle()
    rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
    pop.set_pointing_to(rect)
    pop.connect("closed", lambda p: GLib.idle_add(lambda: (p.unparent(), False)[1]))
    pop.popup()
    return pop

# Das mailist-Fenster – aufgebaut wie filist: Kopfzeile mit Tabs, Navigationsleiste (zurück/vor, Pfad, Suche),
# links Ordner, in der Mitte die Liste (Spalten, sortierbar), rechts die Vorschau, unten die Statuszeile.
# Löschen ist überall einen Handgriff entfernt: Entf, Papierkorb-Knopf an jeder Zeile, Rechtsklick, Mehrfach-
# auswahl mit großen Knöpfen, „aufräumen“ (alle Mails eines Absenders) – und Strg+Z holt alles zurück.
import datetime as dt, difflib, os, time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

import glass
import gmail
import store as stmod
import swipe
import tui

SIDE_MIN = 150
COMPACT_W = 900          # darunter: Liste ODER Vorschau (wie am Handy)
SYSTEM = [               # (id, symbol, name)
    ("INBOX", "\U000F02FA", "posteingang"),
    ("STARRED", "\U000F04CE", "markiert"),
    ("IMPORTANT", "\U000F0E36", "wichtig"),
    ("SENT", "\U000F048A", "gesendet"),
    ("DRAFT", "\U000F0613", "entwürfe"),
    ("ALLMAIL", "\U000F01F0", "alle mails"),
    ("SPAM", "\U000F0027", "spam"),
    ("TRASH", "\U000F0A7A", "papierkorb"),
]
CATS = [
    ("PRIMARY", "\U000F0E13", "allgemein"),
    ("CATEGORY_PROMOTIONS", "\U000F04F9", "werbung"),
    ("CATEGORY_SOCIAL", "\U000F0849", "soziale netze"),
    ("CATEGORY_UPDATES", "\U000F009A", "benachrichtigungen"),
    ("CATEGORY_FORUMS", "\U000F0369", "foren"),
]
NAMES = {i: n for i, _, n in SYSTEM + CATS}
GLYPHS = {i: g for i, g, _ in SYSTEM + CATS}
WEEKDAYS = ["mo", "di", "mi", "do", "fr", "sa", "so"]
MONTHS = ["jan", "feb", "mär", "apr", "mai", "jun", "jul", "aug", "sep", "okt", "nov", "dez"]


def human_size(n):
    n = n or 0
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024


def short_date(ms):
    if not ms:
        return ""
    d = dt.datetime.fromtimestamp(ms / 1000)
    now = dt.datetime.now()
    if d.date() == now.date():
        return d.strftime("%H:%M")
    if d.date() == (now - dt.timedelta(days=1)).date():
        return "gestern"
    if (now - d).days < 6:
        return f"{WEEKDAYS[d.weekday()]} {d:%H:%M}"
    if d.year == now.year:
        return f"{d.day}. {MONTHS[d.month - 1]}"
    return d.strftime("%d.%m.%y")


def long_date(ms):
    if not ms:
        return ""
    d = dt.datetime.fromtimestamp(ms / 1000)
    return f"{WEEKDAYS[d.weekday()]}, {d.day}. {MONTHS[d.month - 1]} {d.year} · {d:%H:%M}"


def num(n):
    return f"{n:,}".replace(",", ".")


class MailItem(GObject.Object):
    __gtype_name__ = "MailistItem"

    def __init__(self, row):
        super().__init__()
        self.id = row["id"]
        self.row = row


class Tab:
    def __init__(self, loc):
        self.loc = loc
        self.back, self.fwd = [], []
        self.sel = None
        self.button = None


def loc_key(loc):
    return "|".join(str(x) for x in loc)


class MailWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="mailist", hide_on_close=True)
        self.app = app
        self.st = app.store
        self.prefs = app.prefs
        self.set_default_size(*getattr(glass, "fit_size", lambda w, h: (w, h))(1280, 800))
        getattr(glass, "frame_window", lambda w: w.set_decorated(False))(self)
        for c in ("pb-window", "tui", "ml"):
            self.add_css_class(c)
        self.theme = tui.Theme.get()
        self.tabs, self.cur = [], None
        self.undo_stack = []
        self.reading = False           # schmales Fenster: Vorschau statt Liste
        self.compact = None
        self._status_id = None
        self._sel_timer = None
        self._filter_timer = None
        self._search_token = 0
        self.search_ids = {}           # suchbegriff → ids (Gmail-Suche)
        self._more_busy = False
        self._mark_timer = None
        self._side_forced = False

        root = tui.vbox(spacing=0)
        self.set_child(root)
        root.append(self._build_top())
        root.append(self._build_nav())

        self.body = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True, shrink_start_child=False,
                              resize_start_child=False, wide_handle=False)
        self.body.add_css_class("ml-body")
        self.side = tui.vbox(spacing=1)
        self.side.add_css_class("ml-places")
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.side, vexpand=True)
        self.side_panel = tui.Panel("ordner", sc)
        self.side_panel.body.set_vexpand(True)
        self.side_panel.set_size_request(SIDE_MIN, -1)
        self.side_panel.set_visible(self.prefs.get("sidebar", True))
        self.body.set_start_child(self.side_panel)
        self.body.set_position(max(SIDE_MIN, int(self.prefs.get("side_width", 220))))

        # Mitte: Liste | Vorschau – oder Aufräumen / Konto
        import reader, pages
        self.list_panel = tui.Panel("posteingang", self._build_list())
        self.list_panel.body.set_vexpand(True)
        self.list_panel.body.add_css_class("ml-panebody")
        self.list_panel.set_size_request(260, -1)
        self.reader = reader.Reader(self)
        self.read_panel = tui.Panel("vorschau", self.reader)
        self.read_panel.body.set_vexpand(True)
        self.read_panel.set_size_request(300, -1)
        self.split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, start_child=self.list_panel,
                               end_child=self.read_panel, resize_start_child=True, resize_end_child=True,
                               shrink_start_child=False, shrink_end_child=False, wide_handle=False)
        self.split.add_css_class("ml-split")
        self.split.connect("notify::position", self._split_moved)
        self.cleanup = pages.Cleanup(self)
        self.account = pages.Account(self)
        self.main = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=90,
                              hhomogeneous=False, hexpand=True, vexpand=True)
        self.main.add_named(self.split, "mail")
        self.main.add_named(self.cleanup, "cleanup")
        self.main.add_named(self.account, "account")
        self.swipe = swipe.SwipeBack(self.main, self.can_back, self.go_back, where=self._where, target=self._target,
                                     can_fwd=self.can_fwd, fwd=self.go_fwd, fwd_target=self._fwd_target)
        self.body.set_end_child(self.swipe.widget)
        self.body.connect("notify::position", self._side_moved)
        root.append(self.body)
        root.append(self._build_status())

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        self.connect("close-request", self._on_close)
        self.connect("notify::is-active", lambda *_: self.is_active() and self.app.sync_now(quiet=True)
                     if time.time() - int(self.st.get("synced", 0) or 0) > 60 else None)

        start = ("folder", self.prefs.get("start", "INBOX"))
        if not gmail.logged_in() and not self.st.count_local("ALLMAIL"):
            start = ("account",)
        for loc in self.prefs.get("tabs", []) or [list(start)]:
            self.new_tab(tuple(loc), switch=False)
        self.switch_tab(self.tabs[0])
        self.build_side()
        self.sync_state(app.state)

    # ================================================================ Aufbau
    def _build_top(self):
        top = tui.hbox(spacing=2)
        top.add_css_class("topbar")
        self.brand = tui.label("mailist", "brand")
        top.append(self.brand)
        self.tab_box = tui.hbox(spacing=2)
        top.append(self.tab_box)
        plus = Gtk.Button(label="+", tooltip_text="Neuer Tab (Strg+T)", focus_on_click=False)
        plus.add_css_class("tab")
        plus.connect("clicked", lambda *_: self.new_tab(self.cur.loc if self.cur else ("folder", "INBOX")))
        top.append(plus)
        top.append(tui.label("", hexpand=True))
        self.state_btn = Gtk.Button(focus_on_click=False, valign=Gtk.Align.CENTER)
        self.state_btn.add_css_class("flat")
        self.state_btn.connect("clicked", lambda *_: self._state_clicked())
        top.append(self.state_btn)
        top.append(self._changelog_btn())
        gt = glass.GlassToggle(self)
        gt.set_margin_start(6)
        top.append(gt)
        close = tui.Button("✕", self.close, tooltip="Schließen (läuft im Hintergrund weiter, Super+G holt es zurück)")
        close.add_css_class("close")
        close.set_margin_start(4)
        top.append(close)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.EXTERNAL, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                propagate_natural_height=True, child=top)
        return Gtk.WindowHandle(child=sc)

    def _changelog_btn(self):
        here = os.path.dirname(os.path.realpath(__file__))
        version = self.app_version()
        b = Gtk.Button(label=f"v{version}", tooltip_text="changelog – was ist neu?", focus_on_click=False,
                       valign=Gtk.Align.CENTER, margin_start=6)
        b.add_css_class("flat")
        b.add_css_class("off")

        def show(*_):
            import changelog
            changelog.open_window(self, "mailist", os.path.join(here, "CHANGELOG.md"), key="mailist", version=version)
            b.remove_css_class("news")
            b.add_css_class("off")
            b.set_label(f"v{version}")
        b.connect("clicked", show)
        try:
            import changelog
            if version and changelog.check_new("mailist", version):
                b.add_css_class("news")
                b.remove_css_class("off")
                b.set_label(f"󰋚 v{version}")
                GLib.timeout_add(800, lambda: (self.say(f"neu in v{version} – klick oben rechts auf „v{version}“",
                                                        seconds=12), False)[1])
        except Exception as e:
            print("changelog:", e)
        return b

    @staticmethod
    def app_version():
        try:
            return open(os.path.join(os.path.dirname(os.path.realpath(__file__)), "VERSION")).read().strip()
        except OSError:
            return ""

    def _build_nav(self):
        nav = tui.hbox(spacing=4)
        nav.add_css_class("ml-nav")

        def flat(glyph, tip, cb):
            b = Gtk.Button(label=glyph, tooltip_text=tip, focus_on_click=False, valign=Gtk.Align.CENTER)
            b.add_css_class("flat")
            b.add_css_class("ml-navbtn")
            b.connect("clicked", lambda *_: cb())
            nav.append(b)
            return b
        self.btn_back = flat("\U000F0141", "Zurück (Alt+←)", self.go_back)
        self.btn_fwd = flat("\U000F0142", "Vor (Alt+→)", self.go_fwd)
        self.btn_sync = flat("\U000F0450", "Abgleichen (F5)", lambda: self.app.sync_now())
        self.crumbs = tui.hbox(spacing=6, hexpand=True)
        self.crumbs.add_css_class("ml-crumbs")
        nav.append(self.crumbs)
        self.search = tui.Entry("suchen · enter = in ganz gmail", width=26)
        self.search.set_hexpand(False)
        self.search.connect("changed", self._filter_changed)
        self.search.connect("activate", lambda *_: self.server_search())
        nav.append(self.search)
        self.menu_btn = Gtk.MenuButton(label="\U000F035C", tooltip_text="Ansicht & Einstellungen",
                                       valign=Gtk.Align.CENTER)
        self.menu_btn.add_css_class("tbtn")
        self.menu_btn.set_popover(self._settings_popover())
        nav.append(self.menu_btn)
        comp = Gtk.Button(tooltip_text="Neue Mail (Strg+N)", focus_on_click=False, valign=Gtk.Align.CENTER)
        comp.set_child(tui.hbox(tui.label("\U000F03EB"), tui.label("neu"), spacing=6))
        comp.add_css_class("ml-compose")
        comp.connect("clicked", lambda *_: self.app.compose())
        nav.append(comp)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.EXTERNAL, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                propagate_natural_height=True, child=nav)
        return sc

    def _settings_popover(self):
        pop = Gtk.Popover(has_arrow=False)
        pop.add_css_class("tpop")
        box = tui.vbox(spacing=2)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(6)
        self.chk = {}

        def check(key, text, default, cb=None):
            c = tui.Check(text, self.prefs.get(key, default),
                          lambda v: (self.app.set_pref(key, v), cb and cb(v)))
            self.chk[key] = c
            box.append(c)
        check("sidebar", "ordner-leiste  (f9)", True, lambda v: self.side_panel.set_visible(v))
        check("preview", "vorschau  (f3)", True, lambda v: self.apply_layout())
        check("col_size", "spalte größe", True, lambda v: self.col_size.set_visible(v))
        check("notify", "neue mails melden", True)
        check("mark_read", "beim lesen als gelesen markieren", True)
        check("html", "html-mails formatiert zeigen", True, lambda v: self.reader.rerender())
        box.append(tui.label("abgleich alle", "dim"))
        iv = tui.Select(lambda v: (self.app.set_pref("interval_min", int(v)), self.app._schedule()))
        iv.set_options([("1", "1 minute"), ("2", "2 minuten"), ("5", "5 minuten"), ("15", "15 minuten")],
                       str(self.prefs.get("interval_min", 2)))
        box.append(iv)
        box.append(tui.label("mails im zwischenspeicher", "dim"))
        lim = tui.Select(self._set_limit)
        lim.set_options([("500", "500 neueste"), ("2000", "2.000 neueste"), ("5000", "5.000 neueste"),
                         ("10000", "10.000 neueste")], str(self.prefs.get("sync_limit", 2000)))
        box.append(lim)
        pop.set_child(box)
        return pop

    def _set_limit(self, v):
        self.app.set_pref("sync_limit", int(v))
        self.app.syncer.limit = int(v)
        self.st.put("history", None)            # beim nächsten Abgleich neu füllen
        self.app.sync_now()

    def _build_status(self):
        self.status = tui.label("", "msg", hexpand=True, ellipsize=True)
        self.undo_btn = Gtk.Button(label="rückgängig", tooltip_text="Strg+Z", valign=Gtk.Align.CENTER, visible=False)
        self.undo_btn.add_css_class("tbtn")
        self.undo_btn.add_css_class("ml-undo")
        self.undo_btn.connect("clicked", lambda *_: self.undo())
        self.sel_info = tui.label("", "dim")
        self.hints = tui.hbox(spacing=12)
        for k, v in (("entf", "löschen"), ("strg+z", "zurück"), ("r", "antworten"), ("e", "archiv"),
                     ("s", "stern"), ("strg+n", "neu")):
            self.hints.append(tui.hbox(tui.label(k, "accent"), tui.label(v, "dim"), spacing=5))
        bar = tui.hbox(self.status, self.undo_btn, self.sel_info, self.hints, spacing=14)
        bar.add_css_class("statusbar")
        return Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.EXTERNAL, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                  propagate_natural_height=True, child=bar)

    # ---------------- Liste
    def _build_list(self):
        self.model = Gio.ListStore(item_type=MailItem)
        self.cv = Gtk.ColumnView(show_row_separators=False, show_column_separators=False, hexpand=True,
                                 vexpand=True, reorderable=False)
        self.cv.add_css_class("ml-list")
        self.sorted = Gtk.SortListModel(model=self.model, sorter=self.cv.get_sorter())
        self.sel = Gtk.MultiSelection(model=self.sorted)
        self.cv.set_model(self.sel)
        self.sel.connect("selection-changed", self._sel_changed)
        self.cv.connect("activate", lambda cv, pos: self._activate(pos))

        def col(title, setup, bind, sorter=None, expand=False, width=None, resizable=True):
            f = Gtk.SignalListItemFactory()
            f.connect("setup", lambda _f, li: setup(li))
            f.connect("bind", lambda _f, li: bind(li, li.get_item()))
            c = Gtk.ColumnViewColumn(title=title, factory=f, expand=expand, resizable=resizable)
            if width:
                c.set_fixed_width(width)
            if sorter:
                c.set_sorter(Gtk.CustomSorter.new(sorter, None))
            self.cv.append_column(c)
            return c

        def cell(li, *kids, spacing=6):
            b = tui.hbox(*kids, spacing=spacing)
            li.set_child(b)
            return b

        # 1) ungelesen / Stern
        def s_flags(li):
            b = cell(li, tui.label("", "ml-dot"), tui.label("", "ml-star"), spacing=2)
            b.set_size_request(30, -1)

        def b_flags(li, it):
            b = li.get_child()
            r = it.row
            b.mid = it.id
            dot, star = b.get_first_child(), b.get_last_child()
            dot.set_label("●" if r["unread"] else "")
            star.set_label("\U000F04CE" if r["starred"] else "")
            (b.add_css_class if r["unread"] else b.remove_css_class)("unread")
        col("", s_flags, b_flags, width=44, resizable=False,
            sorter=lambda a, b, _: (b.row["unread"] * 2 + b.row["starred"]) - (a.row["unread"] * 2 + a.row["starred"]))

        # 2) von / an
        def s_from(li):
            cell(li, tui.label("", "ml-from", ellipsize=True, hexpand=True))

        def b_from(li, it):
            b = li.get_child()
            b.mid = it.id
            r = it.row
            (b.add_css_class if r["unread"] else b.remove_css_class)("unread")
            lbl = b.get_first_child()
            lbl.set_label(self.who(r))
            lbl.set_tooltip_text(r["from_addr"] if not self._sent_view() else (r["to_addr"] or ""))
        self.col_from = col("von", s_from, b_from, width=int(self.prefs.get("w_from", 210)),
                            sorter=lambda a, b, _: _cmp(self.who(a.row).lower(), self.who(b.row).lower()))

        # 3) Betreff + Vorschau
        def s_subj(li):
            cell(li, tui.label("", "ml-subj", ellipsize=True, hexpand=True, markup=True))

        def b_subj(li, it):
            b = li.get_child()
            b.mid = it.id
            r = it.row
            (b.add_css_class if r["unread"] else b.remove_css_class)("unread")
            subj = " ".join((r["subject"] or "(ohne betreff)").split())
            snip = " ".join((r["snippet"] or "").split())
            b.get_first_child().set_markup(
                f"{tui.esc(subj)}<span alpha='55%' weight='normal'>  {tui.esc(snip[:160])}</span>")
        col("betreff", s_subj, b_subj, expand=True,
            sorter=lambda a, b, _: _cmp((a.row["subject"] or "").lower(), (b.row["subject"] or "").lower()))

        # 4) Anhang + Sicherheit
        def s_flag(li):
            cell(li, tui.label("", "ml-flag"), tui.label("", "ml-flag"), spacing=2)

        def b_flag(li, it):
            import guard
            b = li.get_child()
            b.mid = it.id
            att, sec = b.get_first_child(), b.get_last_child()
            att.set_label("\U000F03E2" if it.row["att"] else "")
            lvl, why = guard.quick(it.row)
            sec.set_label(guard.GLYPH.get(lvl, "") if lvl in ("mittel", "hoch") else "")
            sec.set_tooltip_text(why[-1][1] if lvl in ("mittel", "hoch") and why else None)
            for l in ("mittel", "hoch"):
                (sec.add_css_class if lvl == l else sec.remove_css_class)(l)
        col("", s_flag, b_flag, width=46, resizable=False)

        # 5) Datum
        def s_date(li):
            cell(li, tui.label("", "ml-date", xalign=1, hexpand=True))

        def b_date(li, it):
            b = li.get_child()
            b.mid = it.id
            (b.add_css_class if it.row["unread"] else b.remove_css_class)("unread")
            lbl = b.get_first_child()
            lbl.set_label(short_date(it.row["date"]))
            lbl.set_tooltip_text(long_date(it.row["date"]))
        self.col_date = col("datum", s_date, b_date, width=92, resizable=False,
                            sorter=lambda a, b, _: _cmp(a.row["date"], b.row["date"]))

        # 6) Größe
        def s_size(li):
            cell(li, tui.label("", "ml-size", xalign=1, hexpand=True))

        def b_size(li, it):
            b = li.get_child()
            b.mid = it.id
            b.get_first_child().set_label(human_size(it.row["size"]))
        self.col_size = col("größe", s_size, b_size, width=74, resizable=False,
                            sorter=lambda a, b, _: _cmp(a.row["size"], b.row["size"]))
        self.col_size.set_visible(self.prefs.get("col_size", True))

        # 7) Papierkorb an jeder Zeile (erscheint beim Drüberfahren)
        def s_act(li):
            btn = Gtk.Button(label="\U000F0A7A", tooltip_text="löschen (Entf)", focus_on_click=False,
                             valign=Gtk.Align.CENTER)
            btn.add_css_class("ml-rowact")
            btn.connect("clicked", self._row_trash)
            cell(li, btn)

        def b_act(li, it):
            b = li.get_child()
            b.mid = it.id
            btn = b.get_first_child()
            btn.mid = it.id
            btn.set_tooltip_text("endgültig löschen" if self.folder() == "TRASH" else "löschen (Entf)")
        col("", s_act, b_act, width=34, resizable=False)

        self.cv.sort_by_column(self.col_date, Gtk.SortType.DESCENDING)
        right = Gtk.GestureClick(button=3)
        right.connect("pressed", self._right_click)
        self.cv.add_controller(right)
        drag = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        drag.connect("prepare", self._drag_prepare)
        self.cv.add_controller(drag)

        self.list_scroll = Gtk.ScrolledWindow(child=self.cv, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.list_scroll.connect("edge-reached", lambda _s, pos: pos == Gtk.PositionType.BOTTOM and self.load_more())
        self.empty = tui.vbox(tui.label("\U000F01EE", "ml-bigglyph", xalign=0.5),
                              tui.label("hier ist nichts", "ml-empty", xalign=0.5), spacing=8,
                              valign=Gtk.Align.CENTER, vexpand=True)
        self.list_stack = Gtk.Stack(vexpand=True)
        self.list_stack.add_named(self.list_scroll, "list")
        self.list_stack.add_named(self.empty, "empty")
        self.foot = tui.label("", "ml-listfoot", hexpand=True, ellipsize=True)
        self.more_btn = Gtk.Button(label="ältere laden", focus_on_click=False)
        self.more_btn.add_css_class("ml-link")
        self.more_btn.connect("clicked", lambda *_: self.load_more(force=True))
        self.empty_trash_btn = Gtk.Button(label="papierkorb leeren", focus_on_click=False, visible=False)
        self.empty_trash_btn.add_css_class("ml-link")
        self.empty_trash_btn.connect("clicked", lambda *_: self.empty_folder())
        return tui.vbox(self.list_stack, tui.hbox(self.foot, self.empty_trash_btn, self.more_btn, spacing=6),
                        spacing=0)

    def who(self, r):
        if self._sent_view() or (r["from_addr"] and r["from_addr"] == self.st.me):
            to = stmod.addr_list(r["to_addr"])
            if not to:
                return "an: –"
            names = [n or a for n, a in to]
            return "an: " + ", ".join(names[:2]) + (f" +{len(names) - 2}" if len(names) > 2 else "")
        return r["from_name"] or r["from_addr"] or "?"

    def _sent_view(self):
        return self.cur is not None and self.cur.loc[0] == "folder" and self.cur.loc[1] in ("SENT", "DRAFT")

    # ================================================================ Seitenleiste
    def build_side(self):
        tui.clear(self.side)
        f = self.folder() if self.cur and self.cur.loc[0] == "folder" else None
        page = self.cur.loc[0] if self.cur else None
        labs = {r["id"]: r for r in self.st.labels()}

        def place(key, glyph, name, active, cb, count=None, soft=False, sub=False, tip=None):
            b = Gtk.Button(focus_on_click=False, tooltip_text=tip)
            b.add_css_class("ml-place")
            if active:
                b.add_css_class("active")
            row = tui.hbox(tui.label(glyph, "pglyph"), tui.label(name, hexpand=True, ellipsize=True), spacing=8)
            if count:
                c = tui.label(num(count) if count < 10000 else "9999+", "ml-count")
                if soft:
                    c.add_css_class("soft")
                row.append(c)
            b.set_child(row)
            b.connect("clicked", lambda *_: cb())
            if sub:
                b.add_css_class("ml-sub")
            if key:
                tgt = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
                tgt.connect("drop", lambda _t, val, x, y, k=key: self._drop_on(k, val))
                tgt.connect("enter", lambda *_a, bb=b: (bb.add_css_class("drop"), Gdk.DragAction.MOVE)[1])
                tgt.connect("leave", lambda *_a, bb=b: bb.remove_css_class("drop"))
                b.add_controller(tgt)
            self.side.append(b)
            return b

        def unread(lid):
            u = labs[lid]["unread"] if lid in labs else None
            return u if u is not None else self.st.count_local(lid, unread=True)

        for lid, glyph, name in SYSTEM:
            if lid == "IMPORTANT" and lid not in labs:
                continue
            cnt = None
            if lid in ("INBOX", "SPAM"):
                cnt = unread(lid)
            elif lid == "DRAFT":
                cnt = labs[lid]["total"] if lid in labs else None
            place(lid, glyph, name, page == "folder" and f == lid, lambda l=lid: self.go(("folder", l)),
                  cnt, soft=lid in ("SPAM", "DRAFT"))
            if lid == "INBOX" and any(c in labs and (labs[c]["total"] or 0) > 0 for c, _, _ in CATS[1:]):
                for cid, cg, cn in CATS:
                    cnt = None if cid == "PRIMARY" else (labs[cid]["unread"] if cid in labs else None)
                    if cid != "PRIMARY" and cid in labs and not labs[cid]["total"]:
                        continue
                    place(cid, cg, cn, page == "folder" and f == cid, lambda l=cid: self.go(("folder", l)),
                          cnt, soft=True, sub=True)
        user = sorted((r for r in labs.values() if r["type"] == "user"), key=lambda r: r["name"].lower())
        if user:
            self.side.append(tui.label("labels", "ml-place-head"))
            for r in user:
                depth = r["name"].count("/")
                name = r["name"].rsplit("/", 1)[-1]
                b = place(r["id"], "\U000F04F9", name, page == "folder" and f == r["id"],
                          lambda l=r["id"]: self.go(("folder", l)), r["unread"], soft=True, tip=r["name"])
                b.set_margin_start(14 * depth)
        self.side.append(tui.label("werkzeuge", "ml-place-head"))
        place(None, "\U000F00E2", "aufräumen", page == "cleanup", lambda: self.go(("cleanup",)),
              tip="Wer schickt am meisten? Alle Mails eines Absenders auf einmal löschen")
        place(None, "\U000F0004", "konto", page == "account", lambda: self.go(("account",)),
              tip="Anmeldung, Zwischenspeicher, Abmelden")
        n = self.st.count_local("ALLMAIL") + self.st.count_local("TRASH") + self.st.count_local("SPAM")
        self.side_foot = tui.label(f"{num(n)} mails · {human_size(self.st.size_on_disk())} lokal", "ml-sidefoot",
                                   wrap=True)
        self.side.append(self.side_foot)

    def _drop_on(self, key, val):
        ids = [i for i in (val or "").split(",") if i]
        if not ids:
            return False
        if key == "TRASH":
            self.trash(ids)
        elif key == "SPAM":
            self.apply({"op": "modify", "ids": ids, "add": ["SPAM"], "remove": ["INBOX"]}, f"{len(ids)} als spam")
        elif key == "INBOX":
            self.apply({"op": "modify", "ids": ids, "add": ["INBOX"], "remove": ["SPAM"]}, "in den posteingang")
        elif key == "STARRED":
            self.star(ids, True)
        elif key in ("ALLMAIL",):
            self.archive(ids)
        elif key in NAMES or key.startswith("CATEGORY_") or key == "PRIMARY":
            return False
        else:
            self.apply({"op": "modify", "ids": ids, "add": [key], "remove": []}, "label gesetzt")
        return True

    def _drag_prepare(self, src, x, y):
        ids = self.selected_ids()
        if not ids:
            return None
        return Gdk.ContentProvider.new_for_value(",".join(ids))

    # ================================================================ Tabs & Orte
    def new_tab(self, loc, switch=True):
        t = Tab(tuple(loc))
        btn = Gtk.ToggleButton(focus_on_click=False)
        btn.add_css_class("tab")
        x = Gtk.Button(label="✕", focus_on_click=False, tooltip_text="Tab schließen (Strg+W)")
        x.add_css_class("ml-tabx")
        x.connect("clicked", lambda *_: self.close_tab(t))
        t.lbl = tui.label("")
        t.key = tui.label("", "key")
        btn.set_child(tui.hbox(t.key, t.lbl, x, spacing=4))
        if self.tabs:
            btn.set_group(self.tabs[0].button)
        btn.connect("toggled", lambda b: b.get_active() and self.switch_tab(t))
        t.button = btn
        self.tabs.append(t)
        self.tab_box.append(btn)
        self._number_tabs()
        if switch:
            self.switch_tab(t)
        return t

    def _number_tabs(self):
        multi = len(self.tabs) > 1
        for i, t in enumerate(self.tabs):
            t.key.set_label(tui.SUP[i + 1] if i < 9 else "")
            t.lbl.set_label(self.loc_title(t.loc))
            t.button.get_child().get_last_child().set_visible(multi)

    def switch_tab(self, t):
        if self.cur is t:
            return
        if self.cur:
            self.cur.sel = self.current_id()
        self.cur = t
        if not t.button.get_active():
            t.button.set_active(True)
        self.show_loc(keep=t.sel)

    def close_tab(self, t=None):
        t = t or self.cur
        if len(self.tabs) <= 1:
            return
        i = self.tabs.index(t)
        self.tabs.remove(t)
        self.tab_box.remove(t.button)
        tui.release_later(t.button)
        if self.cur is t:
            self.cur = None
            self.switch_tab(self.tabs[min(i, len(self.tabs) - 1)])
        self._number_tabs()

    def cycle_tab(self, d):
        i = (self.tabs.index(self.cur) + d) % len(self.tabs)
        self.switch_tab(self.tabs[i])

    def loc_title(self, loc):
        if loc[0] == "folder":
            return self.folder_name(loc[1])
        if loc[0] == "search":
            return f"„{loc[1]}“"
        if loc[0] == "sender":
            return loc[2] if len(loc) > 2 and loc[2] else loc[1]
        return {"cleanup": "aufräumen", "account": "konto"}.get(loc[0], loc[0])

    def folder_name(self, fid):
        if fid in NAMES:
            return NAMES[fid]
        r = next((r for r in self.st.labels() if r["id"] == fid), None)
        return (r["name"].rsplit("/", 1)[-1] if r else fid).lower() if r else fid

    def folder(self):
        if self.cur and self.cur.loc[0] == "folder":
            return self.cur.loc[1]
        if self.cur and self.cur.loc[0] == "sender":
            return "ALLMAIL"
        return "ALLMAIL"

    def go(self, loc, push=True):
        loc = tuple(loc)
        t = self.cur
        if t.loc == loc and loc[0] != "search":
            self.set_reading(False)
            return
        if push:
            t.back.append((t.loc, self.current_id()))
            t.fwd.clear()
        t.loc = loc
        if self.search.get_text() and loc[0] != "search":
            self._silent_clear_search()
        self.show_loc()

    def can_back(self):
        return bool(self.reading and self.compact) or bool(self.cur and self.cur.back)

    def can_fwd(self):
        return bool(self.cur and self.cur.fwd)

    def go_back(self):
        if self.reading and self.compact:
            self.set_reading(False)
            return
        t = self.cur
        if not t.back:
            return
        t.fwd.append((t.loc, self.current_id()))
        t.loc, sel = t.back.pop()
        self.show_loc(keep=sel)

    def go_fwd(self):
        t = self.cur
        if not t.fwd:
            return
        t.back.append((t.loc, self.current_id()))
        t.loc, sel = t.fwd.pop()
        self.show_loc(keep=sel)

    def _where(self):
        if not self.cur:
            return None, None
        return loc_key(self.cur.loc) + ("|r" if self.reading and self.compact else ""), self.loc_title(self.cur.loc)

    def _target(self):
        if self.reading and self.compact:
            return loc_key(self.cur.loc), self.loc_title(self.cur.loc)
        if self.cur and self.cur.back:
            loc = self.cur.back[-1][0]
            return loc_key(loc), self.loc_title(loc)
        return None

    def _fwd_target(self):
        if self.cur and self.cur.fwd:
            loc = self.cur.fwd[-1][0]
            return loc_key(loc), self.loc_title(loc)
        return None

    def show_loc(self, keep=None):
        t = self.cur
        kind = t.loc[0]
        self._number_tabs()
        self.btn_back.set_sensitive(self.can_back())
        self.btn_fwd.set_sensitive(self.can_fwd())
        self._crumbs()
        if kind in ("cleanup", "account"):
            self.sel_info.set_label("")
        if kind == "cleanup":
            self.main.set_visible_child_name("cleanup")
            self.cleanup.refresh()
        elif kind == "account":
            self.main.set_visible_child_name("account")
            self.account.refresh()
        else:
            self.main.set_visible_child_name("mail")
            self.list_panel.set_title(self.loc_title(t.loc))
            self.reload(keep=keep, scroll_top=True)
            self.empty_trash_btn.set_visible(kind == "folder" and t.loc[1] in ("TRASH", "SPAM"))
            self.empty_trash_btn.set_label("papierkorb leeren" if t.loc[1:2] == ("TRASH",) else "spam leeren")
        self.build_side()
        self.apply_layout()

    def _crumbs(self):
        tui.clear(self.crumbs)
        loc = self.cur.loc
        parts = ["gmail"]
        if loc[0] == "folder":
            if loc[1].startswith("CATEGORY_") or loc[1] == "PRIMARY":
                parts.append("posteingang")
            parts.append(self.folder_name(loc[1]))
        elif loc[0] == "search":
            parts += ["suche", loc[1]]
        elif loc[0] == "sender":
            parts += ["absender", loc[1]]
        else:
            parts.append(self.loc_title(loc))
        for i, p in enumerate(parts):
            if i:
                self.crumbs.append(tui.label("›", "ml-sep"))
            lbl = tui.label(p, "ml-crumb", ellipsize=True)
            if i == len(parts) - 1:
                lbl.add_css_class("last")
            self.crumbs.append(lbl)

    # ================================================================ Liste füllen
    def rows_for(self, loc):
        text = self.search.get_text().strip() if loc[0] != "search" else None
        if loc[0] == "folder":
            return self.st.listing(loc[1], text=text)
        if loc[0] == "sender":
            return self.st.listing("ALLMAIL", text=text, sender=loc[1])
        if loc[0] == "search":
            ids = self.search_ids.get(loc[1])
            if ids is None:
                return self.st.listing("ALLMAIL", text=loc[1])
            return self.st.listing(None, ids=ids)
        return []

    def reload(self, keep=None, scroll_top=False):
        """Liste aus der Datenbank neu aufbauen – nur die Unterschiede werden getauscht (Auswahl/Scrollstand
        bleiben)."""
        if not self.cur or self.cur.loc[0] not in ("folder", "sender", "search"):
            return
        sel_ids = set(self.selected_ids()) if keep is None else ({keep} if keep else set())
        first_pos = self._first_selected_pos()
        rows = self.rows_for(self.cur.loc)
        old = [self.model.get_item(i).id for i in range(self.model.get_n_items())]
        new = [r["id"] for r in rows]
        if scroll_top:
            self.model.remove_all()
            old = []
        byid = {self.model.get_item(i).id: self.model.get_item(i) for i in range(self.model.get_n_items())}
        sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
        for tag, i1, i2, j1, j2 in reversed(sm.get_opcodes()):
            if tag == "equal":
                # geänderte Einträge als neues Objekt einsetzen – GTK zeichnet dasselbe Objekt sonst nicht neu
                for k in range(i1, i2):
                    r = rows[j1 + (k - i1)]
                    if tuple(self.model.get_item(k).row) != tuple(r):
                        self.model.splice(k, 1, [MailItem(r)])
            else:
                fresh = []
                for j in range(j1, j2):
                    it = byid.get(new[j])
                    fresh.append(it if it is not None and tuple(it.row) == tuple(rows[j]) else MailItem(rows[j]))
                self.model.splice(i1, i2 - i1, fresh)
        self.list_stack.set_visible_child_name("list" if rows else "empty")
        self._update_foot(len(rows))
        if sel_ids:
            if set(self.selected_ids()) != sel_ids:
                self.select_ids(sel_ids, fallback=first_pos)
        elif scroll_top:
            self.reader.show_empty(self.loc_title(self.cur.loc))
        if scroll_top:
            GLib.idle_add(lambda: (self.list_scroll.get_vadjustment().set_value(0), False)[1])

    def _update_foot(self, n):
        loc = self.cur.loc
        bits = [f"{num(n)} mails"]
        if loc[0] == "folder":
            u = self.st.count_local(loc[1], unread=True)
            if u:
                bits.append(f"{num(u)} ungelesen")
            r = next((r for r in self.st.labels() if r["id"] == loc[1]), None)
            if r and r["total"] and r["total"] > n:
                bits.append(f"bei gmail {num(r['total'])}")
        elif loc[0] == "search" and loc[1] not in self.search_ids:
            bits.append("nur lokal – enter sucht in ganz gmail")
        self.foot.set_label(" · ".join(bits))
        self.more_btn.set_visible(loc[0] == "folder" and gmail.logged_in())

    def refresh(self, soft=True):
        """Nach Abgleich/Änderung: Liste, Ordner, Vorschau auffrischen."""
        if not self.cur:
            return
        kind = self.cur.loc[0]
        if kind in ("folder", "sender", "search"):
            self.reload()
            self.reader.refresh_row()
        elif kind == "cleanup":
            self.cleanup.refresh()
        elif kind == "account":
            self.account.refresh()
        self.build_side()

    def load_more(self, force=False):
        if self._more_busy or not self.cur or self.cur.loc[0] != "folder" or not gmail.logged_in():
            return
        if not force and self.model.get_n_items() < 30:
            return
        self._more_busy = True
        self.more_btn.set_label("lädt …")
        self.app.syncer.request("more", self.cur.loc[1])

    def more_done(self, folder, n, has_more):
        self._more_busy = False
        self.more_btn.set_label("ältere laden" if has_more else "alles geladen")
        self.more_btn.set_sensitive(has_more)
        if n:
            self.say(f"{n} ältere mails geladen")
            self.reload()

    # ================================================================ Auswahl
    def selected_ids(self):
        out = []
        bs = self.sel.get_selection()
        ok, it, pos = Gtk.BitsetIter.init_first(bs)
        while ok:
            item = self.sorted.get_item(pos)
            if item:
                out.append(item.id)
            ok, pos = it.next()
        return out

    def current_id(self):
        ids = self.selected_ids()
        return ids[0] if len(ids) == 1 else (ids[0] if ids else None)

    def _first_selected_pos(self):
        bs = self.sel.get_selection()
        return bs.get_minimum() if not bs.is_empty() else None

    def select_ids(self, ids, fallback=None, scroll=False):
        n = self.sorted.get_n_items()
        bs = Gtk.Bitset.new_empty()
        first = None
        for i in range(n):
            if self.sorted.get_item(i).id in ids:
                bs.add(i)
                first = i if first is None else first
        if first is None and fallback is not None and n:
            first = min(fallback, n - 1)
            bs.add(first)
        self.sel.set_selection(bs, Gtk.Bitset.new_range(0, n))
        if first is not None and scroll:
            self.cv.scroll_to(first, None, Gtk.ListScrollFlags.FOCUS, None)

    def reveal(self, mid):
        """Mail zeigen (Benachrichtigung angeklickt): in den Posteingang, auswählen, öffnen."""
        if not self.cur or self.cur.loc[0] not in ("folder",):
            self.go(("folder", "INBOX"))
        ids = [self.sorted.get_item(i).id for i in range(self.sorted.get_n_items())]
        if mid not in ids:
            self.go(("folder", "ALLMAIL"))
        self.select_ids({mid}, scroll=True)
        self.set_reading(True)

    def _sel_changed(self, *_):
        if self._sel_timer:
            GLib.source_remove(self._sel_timer)
        self._sel_timer = GLib.timeout_add(90, self._sel_settled)

    def _sel_settled(self):
        self._sel_timer = None
        ids = self.selected_ids()
        if len(ids) == 1:
            self.reader.show(ids[0])
            self.sel_info.set_label("")
        elif ids:
            size = sum((self.st.message(i) or {"size": 0})["size"] or 0 for i in ids[:5000])
            self.reader.show_bulk(ids)
            self.sel_info.set_label(f"{num(len(ids))} ausgewählt · {human_size(size)}")
        else:
            self.reader.show_empty(self.loc_title(self.cur.loc) if self.cur else "")
            self.sel_info.set_label("")
        return False

    def _activate(self, pos):
        it = self.sorted.get_item(pos)
        if not it:
            return
        if " DRAFT " in (it.row["labels"] or ""):
            self.reader.edit_draft()
            return
        self.set_reading(True)

    def schedule_mark_read(self, mid):
        if self._mark_timer:
            GLib.source_remove(self._mark_timer)
            self._mark_timer = None
        if not self.prefs.get("mark_read", True):
            return
        r = self.st.message(mid)
        if not r or not r["unread"]:
            return

        def mark():
            self._mark_timer = None
            if self.current_id() == mid:
                self.mark([mid], read=True, quiet=True)
            return False
        self._mark_timer = GLib.timeout_add(900, mark)

    # ================================================================ Layout
    def do_size_allocate(self, width, height, baseline):
        Gtk.ApplicationWindow.do_size_allocate(self, width, height, baseline)
        compact = width < COMPACT_W
        if compact != self.compact:
            self.compact = compact
            GLib.idle_add(lambda: (self.apply_layout(), False)[1])
        tier = 2 if width >= 1100 else (1 if width >= 860 else 0)
        if tier != getattr(self, "_tier", None):
            self._tier = tier
            GLib.idle_add(lambda: (self._apply_tier(tier), False)[1])
        if width != getattr(self, "_alloc_w", None):
            self._alloc_w = width
            GLib.idle_add(lambda: (self._fit_split(), False)[1])

    def _apply_tier(self, tier):
        self.brand.set_visible(tier >= 1)
        self.hints.set_visible(tier >= 2)
        self.search.set_width_chars(26 if tier >= 2 else 12)
        self.col_size.set_visible(tier >= 2 and self.prefs.get("col_size", True))
        self.col_from.set_fixed_width(int(self.prefs.get("w_from", 210)) if tier >= 2 else 150)
        # schmal: Ordnerleiste vorübergehend weg (F9 holt sie), sonst wie eingestellt
        self.side_panel.set_visible(self.prefs.get("sidebar", True) and (tier >= 1 or self._side_forced))

    def apply_layout(self):
        preview = self.prefs.get("preview", True)
        if self.compact or not preview:
            reading = self.reading
            self.list_panel.set_visible(not reading)
            self.read_panel.set_visible(reading)
        else:
            self.list_panel.set_visible(True)
            self.read_panel.set_visible(True)
            self._fit_split()
        self.btn_back.set_sensitive(self.can_back())

    def _fit_split(self):
        if not (self.list_panel.get_visible() and self.read_panel.get_visible()):
            return False
        w = self.split.get_width()
        if w <= 0:
            return False
        frac = float(self.prefs.get("split", 0.5))
        self._placing = True
        self.split.set_position(int(max(260, min(w - 300, w * frac))))
        self._placing = False
        return False

    def _split_moved(self, *_):
        if getattr(self, "_placing", False):
            return
        w = self.split.get_width()
        if w > 600 and self.split.get_position() > 0:
            self.prefs["split"] = round(self.split.get_position() / w, 3)

    def _side_moved(self, *_):
        p = self.body.get_position()
        if p >= SIDE_MIN:
            self.prefs["side_width"] = p

    def set_reading(self, on):
        if on == self.reading:
            if on:
                self.reader.focus_text()
            return
        self.reading = on
        self.apply_layout()
        if on:
            self.reader.focus_text()
        else:
            self.cv.grab_focus()

    # ================================================================ Aktionen
    def targets(self, ids=None):
        return ids if ids is not None else self.selected_ids()

    def apply(self, op, msg=None, undoable=True, quiet=False):
        if not op["ids"]:
            return
        inverse = stmod.apply_local(self.st, op)
        self.st.queue(op)
        self.app.syncer.request("flush")
        if undoable and inverse:
            self.undo_stack.append((inverse, msg))
            del self.undo_stack[:-30]
        if msg and not quiet:
            self.say(msg + ("  ·  strg+z = rückgängig" if undoable and inverse else ""), undo=bool(inverse and undoable))
        self.refresh()

    def trash(self, ids=None):
        ids = self.targets(ids)
        if not ids:
            return
        if self.cur.loc[0] == "folder" and self.cur.loc[1] == "TRASH":
            self.delete_forever(ids)
            return
        self.apply({"op": "trash", "ids": ids}, f"{num(len(ids))} {'mail' if len(ids) == 1 else 'mails'} gelöscht")

    def delete_forever(self, ids=None):
        import pages
        ids = self.targets(ids)
        if not ids:
            return
        pages.confirm(self, "endgültig löschen?",
                      f"{num(len(ids))} {'mail wird' if len(ids) == 1 else 'mails werden'} bei gmail sofort und "
                      f"für immer gelöscht – das lässt sich nicht rückgängig machen.",
                      "endgültig löschen", lambda: self.apply({"op": "delete", "ids": ids},
                                                             f"{num(len(ids))} endgültig gelöscht", undoable=False))

    def empty_folder(self):
        f = self.cur.loc[1]
        ids = [r["id"] for r in self.st.listing(f, limit=100000)]
        if ids:
            self.delete_forever(ids)

    def archive(self, ids=None):
        ids = self.targets(ids)
        self.apply({"op": "modify", "ids": ids, "add": [], "remove": ["INBOX"]},
                   f"{num(len(ids))} archiviert (in „alle mails“)")

    def mark(self, ids=None, read=True, quiet=False):
        ids = self.targets(ids)
        self.apply({"op": "modify", "ids": ids, "add": [] if read else ["UNREAD"], "remove": ["UNREAD"] if read else []},
                   f"{num(len(ids))} als {'gelesen' if read else 'ungelesen'} markiert", quiet=quiet,
                   undoable=not quiet)

    def toggle_read(self):
        ids = self.selected_ids()
        if ids:
            r = self.st.message(ids[0])
            self.mark(ids, read=bool(r and r["unread"]))

    def star(self, ids=None, on=None):
        ids = self.targets(ids)
        if not ids:
            return
        if on is None:
            r = self.st.message(ids[0])
            on = not (r and r["starred"])
        self.apply({"op": "modify", "ids": ids, "add": ["STARRED"] if on else [], "remove": [] if on else ["STARRED"]},
                   "markiert" if on else "markierung entfernt")

    def spam(self, ids=None, on=True):
        ids = self.targets(ids)
        if on:
            self.apply({"op": "modify", "ids": ids, "add": ["SPAM"], "remove": ["INBOX"]}, f"{len(ids)} als spam gemeldet")
        else:
            self.apply({"op": "modify", "ids": ids, "add": ["INBOX"], "remove": ["SPAM"]}, "kein spam – im posteingang")

    def restore(self, ids=None):
        ids = self.targets(ids)
        self.apply({"op": "untrash", "ids": ids}, f"{num(len(ids))} wiederhergestellt")

    def undo(self):
        if not self.undo_stack:
            self.say("nichts rückgängig zu machen")
            return
        op, msg = self.undo_stack.pop()
        stmod.apply_local(self.st, op)
        self.st.queue(op)
        self.app.syncer.request("flush")
        self.say(f"rückgängig: {msg}" if msg else "rückgängig gemacht")
        self.refresh()
        if op.get("ids"):
            self.select_ids(set(op["ids"]))

    def _row_trash(self, btn):
        mid = getattr(btn, "mid", None)
        if not mid:
            return
        ids = self.selected_ids()
        self.trash(ids if mid in ids else [mid])

    # ================================================================ Suche
    def _filter_changed(self, *_):
        if self._filter_timer:
            GLib.source_remove(self._filter_timer)
        self._filter_timer = GLib.timeout_add(160, self._filter_settled)

    def _filter_settled(self):
        self._filter_timer = None
        if self.cur.loc[0] == "search":
            if not self.search.get_text().strip():
                self.go_back()
            return False
        if self.cur.loc[0] in ("folder", "sender"):
            self.reload(scroll_top=True)
        return False

    def _silent_clear_search(self):
        self.search.handler_block_by_func(self._filter_changed)
        self.search.set_text("")
        self.search.handler_unblock_by_func(self._filter_changed)

    def server_search(self):
        q = self.search.get_text().strip()
        if not q:
            return
        if not gmail.logged_in():
            self.say("gmail-suche braucht eine anmeldung – zeige nur lokale treffer")
            return
        self._search_token += 1
        self.search_ids.pop(q, None)
        self.go(("search", q))
        self.say(f"suche „{q}“ in gmail …")
        self.app.syncer.request("search", q, self._search_token)

    def search_done(self, token, ids):
        if token != self._search_token or self.cur.loc[0] != "search":
            return
        self.search_ids[self.cur.loc[1]] = ids
        self.say(f"{len(ids)} treffer in gmail")
        self.reload(scroll_top=True)

    # ================================================================ Rechtsklick
    def _row_at(self, x, y):
        w = self.cv.pick(x, y, Gtk.PickFlags.DEFAULT)
        while w is not None and w is not self.cv:
            if hasattr(w, "mid"):
                return w.mid
            w = w.get_parent()
        return None

    def _right_click(self, g, n, x, y):
        mid = self._row_at(x, y)
        if not mid:
            return
        if mid not in self.selected_ids():
            self.select_ids({mid})
        g.set_state(Gtk.EventSequenceState.CLAIMED)
        self.context_menu(self.selected_ids(), self.cv, x, y)

    def context_menu(self, ids, widget, x, y):
        import pages
        if not ids:
            return
        one = len(ids) == 1
        r = self.st.message(ids[0]) if one else None
        in_trash = self.cur.loc[0] == "folder" and self.cur.loc[1] == "TRASH"
        in_spam = self.cur.loc[0] == "folder" and self.cur.loc[1] == "SPAM"
        pop = pages.Menu(widget, x, y)
        q = [("\U000F0A7A", "endgültig löschen" if in_trash else "löschen (Entf)", lambda: self.trash(ids), True)]
        if one:
            q = [("\U000F045A", "antworten (r)", lambda: self.reader.reply("reply"), False),
                 ("\U000F0F5D", "allen antworten (a)", lambda: self.reader.reply("all"), False)] + q
        q.insert(len(q) - 1, ("\U000F003C", "archivieren (e)", lambda: self.archive(ids), False))
        pop.quick(q)
        unread = any((self.st.message(i) or {"unread": 0})["unread"] for i in ids[:200])
        pop.item("als gelesen markieren" if unread else "als ungelesen markieren",
                 lambda: self.mark(ids, read=unread))
        starred = all((self.st.message(i) or {"starred": 0})["starred"] for i in ids[:200])
        pop.item("markierung entfernen" if starred else "markieren (stern)", lambda: self.star(ids, not starred))
        if one:
            pop.item("weiterleiten (f)", lambda: self.reader.reply("forward"))
        pop.sep()
        if in_trash:
            pop.item("wiederherstellen", lambda: self.restore(ids))
        elif in_spam:
            pop.item("kein spam", lambda: self.spam(ids, on=False))
        else:
            pop.item("als spam melden", lambda: self.spam(ids))
        if r and r["from_addr"]:
            addr = r["from_addr"]
            n = len(self.st.ids_from(addr))
            pop.item(f"alle von {addr} zeigen ({n})", lambda: self.go(("sender", addr, r["from_name"] or addr)))
            pop.item(f"alle {n} von diesem absender löschen", lambda: self.trash(self.st.ids_from(addr)), danger=True)
        if not in_trash:
            pop.item("endgültig löschen (shift+entf)", lambda: self.delete_forever(ids), danger=True)
        pop.popup()

    # ================================================================ Tasten
    def _focus_in_text(self):
        f = self.get_focus()
        return isinstance(f, (Gtk.Text, Gtk.Entry)) or (isinstance(f, Gtk.TextView) and f.get_editable())

    def _key(self, _c, kv, _code, state):
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        alt = bool(state & Gdk.ModifierType.ALT_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        typing = self._focus_in_text()
        if kv == Gdk.KEY_Escape:
            if typing:
                if self.search.get_text():
                    self.search.set_text("")
                self.cv.grab_focus()
                return True
            if self.reading and (self.compact or not self.prefs.get("preview", True)):
                self.set_reading(False)
                return True
            if self.reader.focused():
                self.cv.grab_focus()
                return True
            return False
        if ctrl:
            k = Gdk.keyval_to_lower(kv)
            if k == Gdk.KEY_t:
                self.new_tab(self.cur.loc)
            elif k == Gdk.KEY_w:
                self.close_tab() if len(self.tabs) > 1 else self.close()
            elif k == Gdk.KEY_q:
                self.close()
            elif k == Gdk.KEY_f:
                self.search.grab_focus()
            elif k == Gdk.KEY_r:
                self.app.sync_now()
            elif k == Gdk.KEY_n:
                self.app.compose()
            elif k == Gdk.KEY_z and not typing:
                self.undo()
            elif k == Gdk.KEY_a and not typing:
                self.sel.select_all()
                self.cv.grab_focus()
            elif kv in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
                self.cycle_tab(-1 if shift else 1)
            elif kv == Gdk.KEY_Page_Down:
                self.cycle_tab(1)
            elif kv == Gdk.KEY_Page_Up:
                self.cycle_tab(-1)
            else:
                return False
            return True
        if alt:
            if kv == Gdk.KEY_Left:
                self.go_back()
            elif kv == Gdk.KEY_Right:
                self.go_fwd()
            elif Gdk.KEY_1 <= kv <= Gdk.KEY_9 and kv - Gdk.KEY_1 < len(self.tabs):
                self.switch_tab(self.tabs[kv - Gdk.KEY_1])
            else:
                return False
            return True
        if kv == Gdk.KEY_F5:
            self.app.sync_now()
            return True
        if kv == Gdk.KEY_F9:
            on = not self.side_panel.get_visible()
            self._side_forced = on
            self.side_panel.set_visible(on)
            self.app.set_pref("sidebar", on)
            self.chk["sidebar"].set_active(on)
            return True
        if kv == Gdk.KEY_F3:
            on = not self.prefs.get("preview", True)
            self.app.set_pref("preview", on)
            self.chk["preview"].set_active(on)
            self.apply_layout()
            return True
        if typing:
            return False
        if self.cur.loc[0] not in ("folder", "sender", "search"):
            return False
        if kv in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete, Gdk.KEY_BackSpace) or (kv == Gdk.KEY_numbersign):
            (self.delete_forever if shift else self.trash)()
            return True
        ch = Gdk.keyval_to_unicode(kv)
        ch = chr(ch).lower() if ch else ""
        acts = {"r": lambda: self.reader.reply("reply"), "a": lambda: self.reader.reply("all"),
                "f": lambda: self.reader.reply("forward"), "e": self.archive, "s": self.star, "u": self.toggle_read,
                "#": self.trash, "/": self.search.grab_focus, "!": self.spam}
        if ch in acts and self.selected_ids():
            acts[ch]()
            return True
        if ch in ("j", "k"):
            self._step(1 if ch == "j" else -1)
            return True
        if kv == Gdk.KEY_space and self.selected_ids() and not self.reader.focused():
            self.reader.page(1 if not shift else -1)
            return True
        return False

    def _step(self, d):
        n = self.sorted.get_n_items()
        if not n:
            return
        pos = self._first_selected_pos()
        pos = 0 if pos is None else max(0, min(n - 1, pos + d))
        self.sel.select_item(pos, True)
        self.cv.scroll_to(pos, None, Gtk.ListScrollFlags.FOCUS, None)

    # ================================================================ Status
    def say(self, text, error=False, seconds=6, undo=False):
        if error:
            print(time.strftime("%H:%M:%S"), "fehler:", text, flush=True)
            seconds = max(seconds, 15)
        self.status.set_label(("✗ " if error else "▸ ") + str(text).strip().replace("\n", " "))
        (self.status.add_css_class if error else self.status.remove_css_class)("err")
        self.undo_btn.set_visible(undo)
        if self._status_id:
            GLib.source_remove(self._status_id)
        self._status_id = GLib.timeout_add_seconds(max(seconds, 10 if undo else seconds), self._clear_status)

    def _clear_status(self):
        self.status.set_label("")
        self.undo_btn.set_visible(False)
        self._status_id = None
        return False

    def sync_state(self, st, msg=None):
        synced = self.st.get("synced")
        when = time.strftime("%H:%M", time.localtime(int(synced))) if synced else ""
        label, tip = {
            "busy": (f"\U000F04E6 {msg or 'abgleich …'}", "gleicht gerade mit gmail ab"),
            "offline": ("\U000F0B5A offline", f"keine verbindung – änderungen werden später gesendet ({msg or ''})"),
            "expired": ("⚠ neu anmelden", "google hat die anmeldung beendet (testmodus: nach 7 tagen) – klick zum "
                                          "neu anmelden"),
            "noauth": ("\U000F0004 anmelden", "noch nicht mit gmail verbunden – klick zum einrichten"),
        }.get(st, (f"\U000F012C {when}" if when else "\U000F012C", "letzter abgleich mit gmail – klick = jetzt"))
        self.state_btn.set_label(label)
        self.state_btn.set_tooltip_text(tip)
        for c in ("off", "news"):
            self.state_btn.remove_css_class(c)
        if st in ("expired", "noauth"):
            self.state_btn.add_css_class("news")
        elif st == "idle":
            self.state_btn.add_css_class("off")
        self.btn_sync.set_sensitive(st != "busy")
        if st in ("expired", "noauth", "idle") and self.cur and self.cur.loc[0] == "account":
            self.account.refresh()

    def _state_clicked(self):
        if self.app.state in ("expired", "noauth"):
            self.go(("account",))
        else:
            self.app.sync_now()

    def api_error(self, e):
        if e.status == 403 and e.reason in ("SERVICE_DISABLED", "accessNotConfigured"):
            self.say("die gmail-api ist in deinem google-cloud-projekt noch nicht aktiviert – siehe konto", error=True)
            self.account.api_disabled = True
            self.go(("account",))
        elif e.status == 403:
            self.say(f"gmail verweigert den zugriff: {e} – evtl. neu anmelden", error=True)
        else:
            self.say(f"gmail: {e}", error=True)

    # ================================================================ Sonstiges
    def open_eml(self, path):
        import reader
        try:
            with open(path, "rb") as f:
                raw = f.read(30_000_000)
        except OSError as e:
            self.say(f"kann {path} nicht lesen: {e}", error=True)
            return
        self.reader.show_raw(raw, os.path.basename(path))
        self.set_reading(True)

    def save_state(self):
        self.prefs["tabs"] = [list(t.loc) for t in self.tabs if t.loc[0] in ("folder", "cleanup")] or None
        try:
            self.prefs["w_from"] = self.col_from.get_fixed_width()
        except Exception:
            pass
        self.app.save_prefs()

    def _on_close(self, *_):
        self.save_state()
        self.set_reading(False) if self.compact else None
        GLib.timeout_add(1500, lambda: (tui.trim_memory(), False)[1])
        return False


def _cmp(a, b):
    return (a > b) - (a < b)

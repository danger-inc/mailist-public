# Übersetzer Deutsch ⇄ Englisch: kleines Fenster unten rechts (Statusleiste „übersetzer“, Strg+Umschalt+T).
# Wörter schlägt notist offline im Wörterbuch der TU Chemnitz nach („ding“, GPL, ~200 000 Einträge), einmal geladen
# und in eine SQLite-Datei übersetzt (~/.local/share/notes/dict/de-en.sqlite). Ergebnisse sind nach Wortart und
# Bedeutung geordnet; ein Klick auf ein Wort kopiert es. Ganze Sätze gehen nur auf Klick/Enter online (MyMemory).
# Weitere Sprachen (Deutsch ⇄ Französisch, Spanisch …) lassen sich optional laden: WikDict (aus Wiktionary,
# CC BY-SA), je Richtung eine fertige SQLite-Datei unter ~/.local/share/notes/dict/wikdict/.
import json, lzma, os, re, sqlite3, threading, urllib.parse, urllib.request

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

import i18n  # noqa: E402
import paths  # noqa: E402

DICT_DIR = os.path.join(paths.HOME, "dict")
DB = os.path.join(DICT_DIR, "de-en.sqlite")
URL = "https://ftp.tu-chemnitz.de/pub/Local/urz/ding/de-en/de-en.txt.xz"
# Paket „trans-de-en“ (Debian/Ubuntu) bzw. ding bringt die Datei schon mit – dann nichts herunterladen
LOCAL = ("/usr/share/trans/de-en", "/usr/share/dict/de-en.txt", "/usr/share/ding/de-en.txt")
ONLINE = "https://api.mymemory.translated.net/get?{}"
DB_VERSION = 2
WIKDICT_URL = "https://download.wikdict.com/dictionaries/sqlite/2/{}.sqlite3"
WIKDICT_DIR = os.path.join(DICT_DIR, "wikdict")
# Sprachen, die WikDict mit Deutsch anbietet (Englisch kommt aus ding)
LANGS = {"en": "Englisch", "fr": "Französisch", "es": "Spanisch", "it": "Italienisch", "nl": "Niederländisch",
         "pt": "Portugiesisch", "pl": "Polnisch", "sv": "Schwedisch", "da": "Dänisch", "no": "Norwegisch",
         "fi": "Finnisch", "cs": "Tschechisch", "ru": "Russisch", "tr": "Türkisch", "el": "Griechisch",
         "ja": "Japanisch", "zh": "Chinesisch", "la": "Latein", "bg": "Bulgarisch", "ca": "Katalanisch",
         "lt": "Litauisch", "id": "Indonesisch"}
# Wortarten aus WikDict (je nach Sprache benannt) → unsere Gruppen
WD_TYPES = {"substantiv": "Substantiv", "noun": "Substantiv", "nom": "Substantiv", "sustantivo": "Substantiv",
            "sostantivo": "Substantiv", "zelfstandig naamwoord": "Substantiv", "verb": "Verb", "verbe": "Verb",
            "verbo": "Verb", "werkwoord": "Verb", "adjektiv": "Adjektiv", "adjective": "Adjektiv",
            "adjectif": "Adjektiv", "adjetivo": "Adjektiv", "aggettivo": "Adjektiv", "adverb": "Adverb",
            "adverbe": "Adverb", "adverbio": "Adverb", "avverbio": "Adverb", "eigenname": "Eigenname",
            "proper noun": "Eigenname", "redewendung": "Wendung", "präposition": "Präposition",
            "konjunktion": "Konjunktion", "adj": "Adjektiv", "adv": "Adverb",
            "nom propre": "Eigenname", "locution": "Wendung", "pronomen": "Pronomen", "interjektion": "Ausruf", "numerale": "Zahlwort"}

WORDTYPES = (  # (Bezeichnung, ding-Kürzel)
    ("Substantiv", ("m", "f", "n", "pl", "m,f", "m,n", "n,m", "f,n", "n,f", "f,m", "n,pl", "m,f,n")),
    ("Verb", ("vt", "vi", "vr", "v")),
    ("Adjektiv", ("adj",)),
    ("Adverb", ("adv",)),
    ("Präposition", ("prp",)),
    ("Konjunktion", ("conj",)),
    ("Pronomen", ("pron", "ppron", "relativ.pron", "pron interrog")),
    ("Ausruf", ("interj",)),
    ("Zahlwort", ("num",)),
    ("Artikel", ("art",)),
)
TYPE_OF = {k: name for name, ks in WORDTYPES for k in ks}
ARTICLE = {"m": "der", "f": "die", "n": "das", "pl": "die"}
FILLER = re.compile(r"^(jd[nmsr]?|etw|sich|sb|sth|sb's|one's|oneself|so|sth's)\.?(/\S+)?$", re.I)
NOTE = re.compile(r"\{[^}]*\}|\[[^\]]*\]|\([^)]*\)|<[^>]*>")


# ---------------- Wörterbuch ----------------
def split_syn(s):
    """„a; b (x; y); c“ → [a, b (x; y), c] – Semikolon in Klammern trennt nicht."""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}" and depth:
            depth -= 1
        if ch == ";" and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def clean(s):
    """Suchschlüssel: ohne {Wortart} [Gebiet] (Hinweis), klein, Leerraum zusammengefasst."""
    return re.sub(r"\s+", " ", NOTE.sub(" ", s)).strip().lower()


def keys_for(s, en):
    """Schlüssel einer Fundstelle: der Text selbst, bei Verben auch ohne „to“, ohne jdn./etw./sich."""
    k = clean(s)
    if not k:
        return []
    out = [k]
    words = k.split()
    if en and words[0] == "to" and len(words) > 1:
        words = words[1:]
        out.append(" ".join(words))
    core = [w for w in words if not FILLER.match(w)]
    if core and len(core) < len(words):
        out.append(" ".join(core))
    return list(dict.fromkeys(out))


def tags(s):
    """Wortart-Kürzel einer Fundstelle: „Haus {n}“ → ["n"]."""
    return [t.split(";")[0].strip() for t in re.findall(r"\{([^}]*)\}", s)]


def word_type(de, en):
    for t in tags(de):
        if t in TYPE_OF:
            return TYPE_OF[t]
    if clean(en).startswith("to "):
        return "Verb"
    return "Sonstiges"


def pretty(s):
    """Fundstelle lesbar: „Haus {n}; Zuhause {n}“ → „das Haus · das Zuhause“, Wortart-Kürzel weg."""
    out = []
    for syn in split_syn(s):
        t = tags(syn)
        art = ARTICLE.get(t[0]) if t else None
        syn = re.sub(r"\s*\{[^}]*\}", "", syn).strip()
        if art == "die" and t[0] == "pl":
            syn += " (Plural)"
        out.append(f"{art} {syn}" if art else syn)
    return " · ".join(x for x in out if x)


def available():
    return os.path.exists(DB)


def local_source():
    return next((p for p in LOCAL if os.path.exists(p)), None)


def build(lines, progress=None):
    """ding-Zeilen → SQLite (erst in eine Zwischendatei, dann umbenennen)."""
    os.makedirs(DICT_DIR, exist_ok=True)
    tmp = DB + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("CREATE TABLE e(id INTEGER PRIMARY KEY, de TEXT, en TEXT)")
    con.execute("CREATE TABLE k(w TEXT, side INTEGER, id INTEGER, sub INTEGER, syn INTEGER, "
                "PRIMARY KEY(w, side, id, sub)) WITHOUT ROWID")
    con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    fts = True
    try:
        con.execute("CREATE VIRTUAL TABLE f USING fts5(de, en, content='e', content_rowid='id', detail='column')")
    except sqlite3.OperationalError:
        fts = False
    entries, keys = [], []
    n = 0
    for line in lines:
        if not line or line.startswith("#") or " :: " not in line:
            continue
        de, en = line.rstrip("\n").split(" :: ", 1)
        n += 1
        entries.append((n, de, en))
        for side, text in ((0, de), (1, en)):
            for sub, part in enumerate(text.split(" | ")):
                syns = split_syn(part)
                if sub and len(clean(part).split()) > 3:     # lange Wendung: findet die Volltextsuche
                    continue
                for j, syn in enumerate(syns):
                    for k in keys_for(syn, side == 1):
                        keys.append((k, side, n, sub, j))
        if len(entries) >= 5000:
            con.executemany("INSERT INTO e VALUES (?,?,?)", entries)
            con.executemany("INSERT OR IGNORE INTO k VALUES (?,?,?,?,?)", keys)
            entries, keys = [], []
            if progress:
                progress(n)
    con.executemany("INSERT INTO e VALUES (?,?,?)", entries)
    con.executemany("INSERT OR IGNORE INTO k VALUES (?,?,?,?,?)", keys)
    if fts:
        con.execute("INSERT INTO f(f) VALUES ('rebuild')")
    con.execute("INSERT INTO meta VALUES ('version', ?)", (str(DB_VERSION),))
    con.execute("INSERT INTO meta VALUES ('entries', ?)", (str(n),))
    con.commit()
    con.execute("VACUUM")
    con.close()
    os.replace(tmp, DB)
    return n


def install(progress, done):
    """Im Hintergrund: Wörterbuch laden (oder vorhandene Datei nehmen) und einrichten.
    progress(text, anteil 0..1 oder None), done(fehler oder None) – beides im GTK-Hauptthread."""
    def ui(fn, *a):
        GLib.idle_add(lambda: (fn(*a), False)[1])

    def work():
        try:
            src = local_source()
            if src:
                with open(src, encoding="utf-8", errors="replace") as f:
                    lines = f.read().splitlines()
            else:
                req = urllib.request.Request(URL, headers={"User-Agent": "notist"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    total = int(r.headers.get("Content-Length") or 0)
                    data, got = bytearray(), 0
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        data += chunk
                        got += len(chunk)
                        ui(progress, i18n.tr("lade wörterbuch … {} MB", f"{got / 1e6:.1f}"),
                           got / total * 0.5 if total else None)
                lines = lzma.decompress(bytes(data)).decode("utf-8", "replace").splitlines()
            total = len(lines) or 1
            build(lines, lambda n: ui(progress, i18n.tr("richte wörterbuch ein …"), 0.5 + 0.5 * n / total))
            ui(done, None)
        except Exception as e:  # noqa: BLE001 – Netz weg, Platte voll …
            ui(done, str(e))
    threading.Thread(target=work, daemon=True).start()


def wd_path(pair):
    return os.path.join(WIKDICT_DIR, pair + ".sqlite3")


def wd_installed():
    """Geladene Zusatzsprachen (beide Richtungen vorhanden)."""
    return [k for k in LANGS if k != "en" and os.path.exists(wd_path(f"de-{k}")) and os.path.exists(wd_path(f"{k}-de"))]


def wd_install(lang, progress, done):
    """Beide Richtungen de-xx und xx-de laden, Index anlegen. progress(text, anteil), done(fehler) im Hauptthread."""
    def ui(fn, *a):
        GLib.idle_add(lambda: (fn(*a), False)[1])

    def work():
        try:
            os.makedirs(WIKDICT_DIR, exist_ok=True)
            pairs = (f"de-{lang}", f"{lang}-de")
            for i, pair in enumerate(pairs):
                tmp = wd_path(pair) + ".tmp"
                req = urllib.request.Request(WIKDICT_URL.format(pair), headers={"User-Agent": "notist"})
                with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as f:
                    total, got = int(r.headers.get("Content-Length") or 0), 0
                    while True:
                        chunk = r.read(262144)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        ui(progress, i18n.tr("lade {} … {} MB", pair, f"{got / 1e6:.0f}"),
                           (i + (got / total if total else 0)) / 2)
                con = sqlite3.connect(tmp)
                con.execute("CREATE INDEX IF NOT EXISTS ix_rep ON translation(written_rep COLLATE NOCASE)")
                con.commit()
                con.close()
                os.replace(tmp, wd_path(pair))
            ui(done, None)
        except Exception as e:  # noqa: BLE001
            for pair in (f"de-{lang}", f"{lang}-de"):
                for f in (wd_path(pair) + ".tmp",):
                    if os.path.exists(f):
                        os.remove(f)
            ui(done, str(e))
    threading.Thread(target=work, daemon=True).start()


def wd_remove(lang):
    for pair in (f"de-{lang}", f"{lang}-de"):
        try:
            os.remove(wd_path(pair))
        except OSError:
            pass


class WikDict:
    """Deutsch ⇄ andere Sprache aus WikDict – liefert dasselbe Format wie Dictionary.lookup."""

    def __init__(self, lang):
        self.lang = lang
        self.cons = {}

    def con(self, pair):
        if pair not in self.cons:
            self.cons[pair] = sqlite3.connect(wd_path(pair), check_same_thread=False)
        return self.cons[pair]

    def guess(self, q):
        w = q.strip().split()[0] if q.strip() else ""
        hit = lambda pair: self.con(pair).execute(
            "SELECT 1 FROM translation WHERE written_rep=? COLLATE NOCASE LIMIT 1", (w,)).fetchone()
        if re.search(r"[äöüß]", q.lower()) or hit(f"de-{self.lang}"):
            return "de"
        return self.lang if hit(f"{self.lang}-de") else "de"

    def lookup(self, q, direction="auto"):
        q = re.sub(r"\s+", " ", q).strip()
        if not q:
            return []
        sides = {"auto": ("de", self.lang), "de": ("de",)}.get(direction, (self.lang,))
        out = []
        for src in sides:
            res = self._side(q, src)
            if res["groups"]:
                out.append(res)
        if direction == "auto" and any(r["exact"] for r in out):
            out = [r for r in out if r["exact"]]
        out.sort(key=lambda r: -r["score"])
        return out

    def _side(self, q, src):
        pair = f"{src}-{self.lang}" if src == "de" else f"{self.lang}-de"
        con = self.con(pair)
        sql = ("SELECT lexentry, written_rep, sense, trans_list, score FROM translation WHERE {} "
               "ORDER BY importance DESC, score DESC LIMIT 60")
        rows = con.execute(sql.format("written_rep=? COLLATE NOCASE"), (q,)).fetchall()
        exact = len(rows)
        if not rows and len(q) >= 3:            # Wortanfang
            rows = con.execute(sql.format("written_rep>=? COLLATE NOCASE AND written_rep<? COLLATE NOCASE"),
                               (q, q + "￿")).fetchall()[:30]
        groups, order, seen = {}, [], set()
        for lex, rep_, sense, trans, _score in rows:
            words = [w.strip() for w in (trans or "").split(" | ") if w.strip() and "#" not in w]
            key = (rep_, tuple(words))
            if not words or key in seen:
                continue
            seen.add(key)
            parts = (lex or "").split("/", 1)[-1].split("__")
            wt = WD_TYPES.get(parts[1].lower(), parts[1]) if len(parts) > 2 else "Sonstiges"
            ctx = (sense or "").replace("\n", " ").strip()
            if len(ctx) > 140:
                ctx = ctx[:139] + "…"
            if not exact and rep_.lower() != q.lower():
                ctx = f"{rep_}: {ctx}" if ctx else rep_
            if wt not in groups:
                groups[wt] = []
                order.append(wt)
            if len(groups[wt]) < 8:
                groups[wt].append({"words": [(w, None, "") for w in words], "context": ctx, "rank": 0})
        return {"dir": src, "groups": [(g, groups[g]) for g in order], "phrases": [],
                "score": exact * 10 + len(rows), "exact": exact}


class Dictionary:
    def __init__(self):
        self.con = None

    def open(self):
        if self.con is None and available():
            try:
                self.con = sqlite3.connect(DB, check_same_thread=False)
                self.has_fts = bool(self.con.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='f'").fetchone())
            except sqlite3.Error:
                self.con = None
        return self.con

    def entry(self, eid):
        return self.con.execute("SELECT de, en FROM e WHERE id=?", (eid,)).fetchone()

    def guess(self, q):
        """Sprache der Eingabe raten: Wie viele Wörter kennt das deutsche bzw. englische Wörterbuch?"""
        words = re.findall(r"[\wäöüß'-]+", q.lower())
        if re.search(r"[äöüß]", q.lower()):
            return "de"
        score = [0, 0]
        for w in words[:12]:
            for side in (0, 1):
                if self.con.execute("SELECT 1 FROM k WHERE w=? AND side=? LIMIT 1", (w, side)).fetchone():
                    score[side] += 1
        return "en" if score[1] > score[0] else "de"

    def lookup(self, q, direction="auto"):
        """→ [{"dir": "de"/"en", "groups": [(wortart, [bedeutung, …])], "phrases": [(quelle, ziel), …]}]
        bedeutung = {"words": [(wort, artikel, hinweis)], "context": str}"""
        if not self.open():
            return []
        qk = clean(q)
        if not qk:
            return []
        out = []
        for side in {"auto": (0, 1), "de": (0,), "en": (1,)}[direction]:
            res = self._side(qk, side)
            if res["groups"] or res["phrases"]:
                out.append(res)
        if direction == "auto" and any(r["exact"] for r in out):      # nur Wortanfang-Treffer: Rauschen
            out = [r for r in out if r["exact"]]
        # „auto“: die Richtung mit dem besseren Treffer zuerst
        out.sort(key=lambda r: -r["score"])
        return out

    def _side(self, qk, side):
        rows = self.con.execute("SELECT id, sub, syn FROM k WHERE w=? AND side=? LIMIT 300", (qk, side)).fetchall()
        exact = len(rows)
        if not rows and len(qk) >= 3:           # nichts Genaues: Wortanfang („hau“ → Haus, hauen …)
            rows += self.con.execute("SELECT id, sub, syn FROM k WHERE w>? AND w<? AND side=? AND sub=0 LIMIT 40",
                                     (qk, qk + "￿", side)).fetchall()
        seen, items = set(), []
        for eid, sub, syn in rows:
            if (eid, sub) in seen:
                continue
            seen.add((eid, sub))
            de, en = self.entry(eid)
            src_all, tgt_all = (de, en) if side == 0 else (en, de)
            srcs, tgts = src_all.split(" | "), tgt_all.split(" | ")
            if sub >= len(tgts):
                continue
            hit = qk in keys_for(split_syn(srcs[sub])[syn] if syn < len(split_syn(srcs[sub])) else "", side == 1)
            # genauer Treffer zuerst, dann Hauptwort vor Wendung, dann eindeutige (wenige Synonyme) vor allgemeinen
            items.append(((0 if hit else 2) + (1 if sub else 0), syn, len(split_syn(srcs[sub])), -len(srcs), eid,
                          sub, srcs, tgts, de.split(" | "), en.split(" | ")))
        items.sort(key=lambda t: t[:5])
        groups, order = {}, []
        for rank, syn, _ns, _n, eid, sub, srcs, tgts, des, ens in items[:40]:
            wt = word_type(des[sub] if sub < len(des) else des[0], ens[sub] if sub < len(ens) else ens[0])
            words = []
            for w in split_syn(tgts[sub]):
                hint = " ".join(re.findall(r"\[[^\]]*\]|\([^)]*\)", w))
                t = tags(w)
                art = ARTICLE.get(t[0]) if (side == 1 and t) else None
                plain = re.sub(r"\s+", " ", NOTE.sub(" ", w)).strip()
                if plain:
                    words.append((plain, art, hint))
            if not words:
                continue
            m = {"words": words, "context": srcs[sub].strip(), "rank": rank}
            if wt not in groups:
                groups[wt] = []
                order.append(wt)
            groups[wt].append(m)
        phrases = self._phrases(qk, side)
        best = min((m["rank"] for g in groups.values() for m in g), default=9)
        return {"dir": "de" if side == 0 else "en", "groups": [(g, groups[g][:8]) for g in order],
                "phrases": phrases, "score": (10 - best) * 10 + min(len(items), 9), "exact": exact}

    def _phrases(self, qk, side):
        """Wendungen und Beispiele, in denen das Wort vorkommt (kurze zuerst)."""
        words = re.findall(r"[\wäöüß'-]+", qk)
        if not words or not getattr(self, "has_fts", False):
            return []
        col = "de" if side == 0 else "en"
        match = " AND ".join(f'{col}:"{w}"' for w in words if w.replace("'", "").replace("-", "").isalnum())
        if not match:
            return []
        try:
            ids = [r[0] for r in self.con.execute("SELECT rowid FROM f WHERE f MATCH ? LIMIT 400", (match,))]
        except sqlite3.Error:
            return []
        pat = re.compile(r"(?<![\wäöüß])" + re.escape(qk) + r"(?![\wäöüß])", re.I)
        out, seen = [], set()
        for eid in ids:
            de, en = self.entry(eid)
            srcs, tgts = (de, en) if side == 0 else (en, de)
            srcs, tgts = srcs.split(" | "), tgts.split(" | ")
            for i, s in enumerate(srcs):
                if i >= len(tgts) or not pat.search(s):
                    continue
                c = clean(s)
                if c == qk or c in seen or len(c.split()) < 2:
                    continue
                seen.add(c)
                out.append((s.strip(), tgts[i].strip()))
        out.sort(key=lambda p: (len(p[0]), p[0].lower()))
        return out[:30]


def online(q, src, done, lang="en"):
    """Satz übersetzen über MyMemory (kostenlos, ohne Konto). done(text oder None, fehler) im Hauptthread.
    lang = die Fremdsprache des Paars (de ⇄ lang)."""
    tgt = lang if src == "de" else "de"

    def work():
        try:
            url = ONLINE.format(urllib.parse.urlencode({"q": q[:500], "langpair": f"{src}|{tgt}"}))
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "notist"}),
                                        timeout=12) as r:
                data = json.loads(r.read().decode("utf-8"))
            text = (data.get("responseData") or {}).get("translatedText")
            alts = []
            for m in data.get("matches") or []:
                t = (m.get("translation") or "").strip()
                if t and t != text and t not in alts and len(alts) < 3:
                    alts.append(t)
            GLib.idle_add(lambda: (done([text] + alts if text else None, None), False)[1])
        except Exception as e:  # noqa: BLE001
            GLib.idle_add(lambda: (done(None, str(e)), False)[1])
    threading.Thread(target=work, daemon=True).start()


# ---------------- Fenster ----------------
CSS = """
.trx { background-color: alpha(@background, 0.97); border: 1px solid @primary; border-radius: 8px;
    padding: 8px 10px 6px 10px; box-shadow: 0 6px 22px alpha(black, 0.35); }
.trx .trx-title { font-weight: 800; color: @primary; }
.trx button.trx-dir { padding: 0 8px; min-height: 22px; font-size: 11px; border-radius: 4px; }
.trx button.trx-dir { background-image: none; box-shadow: none; }
.trx button.trx-dir:checked, .trx button.trx-dir:checked:hover { background-color: @primary; background-image: none;
    color: @on_primary; }
.trx entry { margin: 6px 0 4px 0; }
.trx .trx-sec { font-size: 11px; font-weight: 800; color: @secondary; margin-top: 8px; }
.trx .trx-type { font-size: 11px; font-weight: 800; color: @primary; margin-top: 6px; }
.trx .trx-ctx { font-size: 11px; color: alpha(@foreground, 0.55); }
.trx button.trx-word { padding: 1px 7px; min-height: 0; border-radius: 4px; border: 1px solid alpha(@primary, 0.45);
    background: none; font-weight: 600; }
.trx button.trx-word:hover { background-color: @primary; color: @on_primary; }
.trx .trx-art { font-size: 11px; color: alpha(@foreground, 0.5); }
.trx button.trx-phrase { padding: 2px 6px; min-height: 0; border: none; background: none; border-radius: 4px; }
.trx button.trx-phrase:hover { background-color: alpha(@primary, 0.18); }
.trx .trx-src { font-size: 11px; color: alpha(@foreground, 0.6); }
.trx .trx-foot { font-size: 10px; color: alpha(@foreground, 0.45); margin-top: 4px; }
.trx .trx-ok { color: @primary; font-weight: 800; }
.trx .trx-empty { color: alpha(@foreground, 0.6); margin: 8px 0; }
.trx progressbar trough { min-height: 4px; } .trx progressbar progress { min-height: 4px; background-color: @primary; }
"""


class Translator(Gtk.Revealer):
    """Kleines Übersetzer-Fenster für die rechte untere Ecke (als Overlay). show_for(text) öffnet es mit Text."""

    def __init__(self, prefs, save_prefs):
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_UP, transition_duration=160,
                         halign=Gtk.Align.END, valign=Gtk.Align.END, margin_end=10, margin_bottom=8,
                         reveal_child=False, can_target=False)
        self.prefs, self.save_prefs = prefs, save_prefs
        self.dict = Dictionary()
        self.wd = {}                     # Sprache → WikDict
        self.lang_mode = False           # Sprachliste statt Ergebnissen
        self.online_btn = None
        self._fresh = False              # nach einer Suche: nächster Klick ins Feld markiert alles
        self.on_toggled = None
        self._timer = 0
        self._installing = False
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, width_request=380)
        box.add_css_class("trx")
        head = Gtk.Box(spacing=4)
        t = Gtk.Label(label="übersetzer", xalign=0, hexpand=True)
        t.add_css_class("trx-title")
        head.append(t)
        self.dir_btns = {}
        group = None
        self.lang_btn = Gtk.Button(focus_on_click=False, tooltip_text="Sprache wählen / weitere laden")
        self.lang_btn.add_css_class("trx-dir")
        self.lang_btn.connect("clicked", lambda *_: self.show_langs(not self.lang_mode))
        head.append(self.lang_btn)
        for key, text, tip in (("auto", "auto", "Sprache selbst erkennen"),
                               ("de", "de → en", "Deutsch nach …"), ("en", "en → de", "… nach Deutsch")):
            b = Gtk.ToggleButton(label=text, tooltip_text=tip, focus_on_click=False)
            b.add_css_class("trx-dir")
            if group:
                b.set_group(group)
            group = group or b
            b.connect("toggled", lambda btn, k=key: btn.get_active() and self.set_dir(k))
            self.dir_btns[key] = b
            head.append(b)
        x = Gtk.Button(label="󰅖", tooltip_text="Schließen (Esc oder Strg+Umschalt+T)", focus_on_click=False)
        x.add_css_class("trx-dir")
        x.connect("clicked", lambda *_: self.toggle(False))
        head.append(x)
        box.append(head)
        self.entry = Gtk.SearchEntry(placeholder_text="Wort oder Satz – Deutsch oder Englisch")
        self.entry.connect("search-changed", self.on_changed)
        self.entry.connect("activate", lambda *_: self.on_enter())
        # 1. Klick ins Feld nach einer Suche markiert alles (gleich lostippen), Doppelklick ebenso
        click = Gtk.GestureClick(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self.on_entry_press)
        self.entry.add_controller(click)
        fc = Gtk.EventControllerFocus()
        fc.connect("leave", lambda *_: setattr(self, "_fresh", bool(self.entry.get_text())))
        self.entry.add_controller(fc)
        self.entry.connect("stop-search", lambda *_: self.toggle(False))
        box.append(self.entry)
        self.results = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                         max_content_height=380)
        self.scroll.set_child(self.results)
        box.append(self.scroll)
        foot = Gtk.Box(spacing=6)
        self.status = Gtk.Label(label="", xalign=0, hexpand=True, ellipsize=3)
        self.status.add_css_class("trx-foot")
        foot.append(self.status)
        src = Gtk.Label(label="wörterbuch: tu chemnitz (ding)", xalign=1)
        src.add_css_class("trx-foot")
        foot.append(src)
        self.src_lbl = src
        box.append(foot)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        box.add_controller(keys)
        self.set_child(box)
        d = self.settings().get("dir", "auto")
        self.dir_btns.get(d, self.dir_btns["auto"]).set_active(True)
        self.first = None
        self.update_lang_ui()

    def settings(self):
        return self.prefs.setdefault("translator", {})

    # ---------- Sprache ----------
    def lang(self):
        lg = self.settings().get("lang", "en")
        return lg if lg == "en" or lg in wd_installed() else "en"

    def source(self):
        lg = self.lang()
        if lg == "en":
            return self.dict
        if lg not in self.wd:
            self.wd[lg] = WikDict(lg)
        return self.wd[lg]

    def update_lang_ui(self):
        lg = self.lang()
        self.lang_btn.set_label(f"󰗊 {lg} ▾" if not self.lang_mode else f"󰗊 {lg} ▴")
        self.dir_btns["de"].set_label(f"de → {lg}")
        self.dir_btns["en"].set_label(f"{lg} → de")
        name = LANGS.get(lg, lg)
        self.dir_btns["de"].set_tooltip_text(i18n.tr("Deutsch nach {}", name))
        self.dir_btns["en"].set_tooltip_text(i18n.tr("{} nach Deutsch", name))
        self.src_lbl.set_label("wörterbuch: tu chemnitz (ding)" if lg == "en" else "wörterbuch: wikdict (wiktionary)")

    def set_lang(self, lg):
        self.settings()["lang"] = lg
        self.save_prefs()
        self.lang_mode = False
        self.update_lang_ui()
        self.queue(0)
        self.entry.grab_focus()

    def show_langs(self, on=True):
        self.lang_mode = on
        self.update_lang_ui()
        self.queue(0)

    def render_langs(self):
        self.label("sprache", "trx-sec")
        have = wd_installed()
        cur = self.lang()
        busy = getattr(self, "_wd_busy", None)
        for k, name in LANGS.items():
            ok = k == "en" or k in have
            row = Gtk.Box(spacing=6)
            b = Gtk.Button(focus_on_click=False, hexpand=True, sensitive=ok)
            b.add_css_class("trx-phrase")
            lab = Gtk.Label(label=f"{'●' if k == cur else ' '} {k}  {i18n.tr(name)}", xalign=0)
            b.set_child(lab)
            b.connect("clicked", lambda _b, kk=k: self.set_lang(kk))
            row.append(b)
            if k == "en":
                row.append(self.small("eingebaut", "trx-ctx"))
            elif ok:
                x = Gtk.Button(label="󰆴", focus_on_click=False, tooltip_text=i18n.tr("{} wieder löschen", name))
                x.add_css_class("trx-dir")
                x.connect("clicked", lambda _b, kk=k: self.remove_lang(kk))
                row.append(x)
            else:
                d = Gtk.Button(label="⟳ lädt …" if busy == k else "󰇚 laden (~35 MB)", focus_on_click=False,
                               sensitive=busy is None)
                d.add_css_class("trx-dir")
                d.connect("clicked", lambda _b, kk=k: self.install_lang(kk))
                row.append(d)
            self.results.append(row)
        self.label("Wörterbücher aus Wiktionary (wikdict.com, CC BY-SA) – einmal laden, danach offline. "
                   "Sätze übersetzt „online“ in jeder Sprache.", "trx-ctx")

    def small(self, text, cls):
        lab = Gtk.Label(label=text)
        lab.add_css_class(cls)
        return lab

    def install_lang(self, lg):
        self._wd_busy = lg
        self.queue(0)

        def progress(text, frac):
            self.status.set_label(text)

        def done(err):
            self._wd_busy = None
            if err:
                self.status.set_label(i18n.tr("Fehler: {}", err))
                self.queue(0)
            else:
                self.status.set_label(i18n.tr("{} bereit", LANGS.get(lg, lg).lower()))
                self.set_lang(lg)
        wd_install(lg, progress, done)

    def remove_lang(self, lg):
        w = self.wd.pop(lg, None)
        if w:
            for c in w.cons.values():
                c.close()
        wd_remove(lg)
        if self.settings().get("lang") == lg:
            self.settings()["lang"] = "en"
            self.save_prefs()
        self.update_lang_ui()
        self.queue(0)

    # ---------- Eingabe ----------
    def on_changed(self, *_):
        if not self.entry.get_text() and self.is_open() and not self.entry_focused():
            self.entry.grab_focus()            # ✕ gedrückt → gleich weiterschreiben
        self._fresh = False
        self.queue()

    def entry_focused(self):
        root = self.entry.get_root()
        f = root.get_focus() if root else None
        return f is not None and (f is self.entry or f.is_ancestor(self.entry))

    def on_entry_press(self, gesture, n, x, y):
        if not self.entry.get_text():
            return
        if n >= 2 or self._fresh:
            self._fresh = False
            if n >= 2:
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            if not self.entry_focused():
                self.entry.grab_focus()
            GLib.idle_add(lambda: (self.entry.select_region(0, -1), False)[1])

    def on_enter(self):
        """Enter: steht „online übersetzen“ als Hauptweg da (kein Treffer oder ganzer Satz), los – sonst kopieren."""
        b = self.online_btn
        if b is not None and b.get_parent() is not None and b.get_visible() and b.get_sensitive() and \
                (self.first is None or getattr(b, "primary", False)):
            b.emit("clicked")
        else:
            self.copy_first()
    def set_dir(self, key):
        if self.settings().get("dir") != key:
            self.settings()["dir"] = key
            self.save_prefs()
        self.queue(0)

    def is_open(self):
        return self.get_reveal_child()

    def toggle(self, on=None, text=None):
        on = (not self.is_open()) if on is None else on
        self.set_reveal_child(on)
        self.set_can_target(on)
        if on:
            if text:
                self.entry.set_text(text.strip()[:500])
            self.entry.grab_focus()
            self.entry.select_region(0, -1)
            self._fresh = False
            self.queue(0)
        if self.on_toggled:
            self.on_toggled(on)

    def on_key(self, ctrl, keyval, code, state):
        if keyval == Gdk.KEY_Escape:
            self.toggle(False)
            return True
        return False

    def queue(self, delay=160):
        if self._timer:
            GLib.source_remove(self._timer)
        self._timer = GLib.timeout_add(max(delay, 1), self._run)

    # ---------- Anzeige ----------
    def clear(self):
        while (c := self.results.get_first_child()):
            self.results.remove(c)
        self.first = None
        self.online_btn = None

    def label(self, text, cls, **kw):
        lab = Gtk.Label(label=text, xalign=0, wrap=True, **kw)
        lab.add_css_class(cls)
        self.results.append(lab)
        return lab

    def _run(self):
        self._timer = 0
        self.clear()
        if self.lang_mode:
            self.render_langs()
            return False
        q = self.entry.get_text().strip()
        if self.lang() != "en":
            return self._run_with(q, self.source())
        if not available():
            self.show_install()
            if q:
                self.online_button(q)
            return False
        return self._run_with(q, self.dict)

    def _run_with(self, q, source):
        lg = self.lang()
        name = i18n.tr(LANGS.get(lg, lg)).lower()
        if not q:
            self.label(i18n.tr("Tippe ein Wort – deutsch oder {}. Ein Klick auf eine Übersetzung kopiert sie, "
                               "Enter kopiert die erste (oder übersetzt online).", name), "trx-empty")
            return False
        direction = self.settings().get("dir", "auto")
        direction = {"en": lg}.get(direction, direction)
        try:
            res = source.lookup(q, direction)
        except sqlite3.Error as e:
            self.label(i18n.tr("Wörterbuch nicht lesbar: {}", str(e)), "trx-empty")
            return False
        both = len(res) > 1
        for r in res:
            if both or direction == "auto":
                self.label(i18n.tr("deutsch → {}", name) if r["dir"] == "de" else i18n.tr("{} → deutsch", name),
                           "trx-sec")
            for wt, means in r["groups"]:
                self.label(i18n.tr(wt), "trx-type")
                for m in means:
                    self.meaning(m)
            if r["phrases"]:
                self.label("wendungen & beispiele", "trx-sec")
                for src, tgt in r["phrases"][:12 if both else 20]:
                    self.phrase(src, tgt)
        if not res:
            self.label(i18n.tr("„{}“ steht nicht im Wörterbuch.", q), "trx-empty")
        if not res or len(q.split()) >= 3:
            self.online_button(q, primary=True)
        self.scroll.get_vadjustment().set_value(0)
        self._fresh = True                     # nächster Klick ins Feld markiert alles
        return False

    def meaning(self, m):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        flow = Gtk.Box()
        import writer
        flow.set_layout_manager(writer.WrapLayout(spacing=4, row_spacing=3))
        for word, art, hint in m["words"]:
            b = Gtk.Button(focus_on_click=False, tooltip_text=i18n.tr("„{}“ kopieren", word))
            b.add_css_class("trx-word")
            inner = Gtk.Box(spacing=4)
            if art:
                a = Gtk.Label(label=art)
                a.add_css_class("trx-art")
                inner.append(a)
            inner.append(Gtk.Label(label=word))
            if hint:
                h = Gtk.Label(label=hint)
                h.add_css_class("trx-art")
                inner.append(h)
            b.set_child(inner)
            b.connect("clicked", lambda _b, w=word: self.copy(w))
            b.set_cursor_from_name("pointer")
            flow.append(b)
            if self.first is None:
                self.first = word
        row.append(flow)
        ctx = Gtk.Label(label=pretty(m["context"]), xalign=0, wrap=True, max_width_chars=48)
        ctx.add_css_class("trx-ctx")
        row.append(ctx)
        row.set_margin_bottom(3)
        self.results.append(row)

    def phrase(self, src, tgt):
        b = Gtk.Button(focus_on_click=False, tooltip_text="Übersetzung kopieren")
        b.add_css_class("trx-phrase")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        s = Gtk.Label(label=src, xalign=0, wrap=True, max_width_chars=48)
        s.add_css_class("trx-src")
        t = Gtk.Label(label=re.sub(r"\s*\{[^}]*\}", "", tgt), xalign=0, wrap=True, max_width_chars=48)
        inner.append(s)
        inner.append(t)
        b.set_child(inner)
        plain = re.sub(r"\s+", " ", NOTE.sub(" ", tgt)).strip()
        b.connect("clicked", lambda *_: self.copy(plain))
        b.set_cursor_from_name("pointer")
        self.results.append(b)

    def online_button(self, q, primary=False):
        b = Gtk.Button(label="󰖟 online übersetzen (MyMemory) ⏎" if primary else "󰖟 online übersetzen (MyMemory)",
                       focus_on_click=False, halign=Gtk.Align.START,
                       tooltip_text="Schickt den Text an api.mymemory.translated.net – nur auf Klick oder Enter")
        b.primary = primary
        self.online_btn = b
        b.add_css_class("trx-dir")
        b.set_margin_top(6)

        def go(*_):
            b.set_sensitive(False)
            b.set_label(i18n.tr("übersetze …"))
            d = self.settings().get("dir", "auto")
            lg = self.lang()
            if d == "de":
                src = "de"
            elif d == "en":
                src = lg
            elif lg != "en":
                src = self.source().guess(q)
            else:
                src = self.dict.guess(q) if self.dict.open() else \
                    ("de" if re.search(r"[äöüß]", q.lower()) else "en")
            online(q, src, lambda texts, err: self.show_online(b, texts, err), lang=lg)
        b.connect("clicked", go)
        self.results.append(b)

    def show_online(self, btn, texts, err):
        if btn.get_parent() is None:            # inzwischen neu gesucht
            return
        btn.set_visible(False)
        if not texts:
            self.label(i18n.tr("Online-Übersetzung ging nicht ({})", err or "keine Antwort"), "trx-empty")
            return
        self.label("online (mymemory)", "trx-sec")
        for t in texts:
            self.phrase(t, t)
        self.first = texts[0]                   # Enter kopiert jetzt die Online-Übersetzung
        self.online_btn = None

    def show_install(self):
        self.label("Für die Wörter braucht notist einmalig das freie Wörterbuch der TU Chemnitz "
                   "(etwa 6 MB, danach offline).", "trx-empty")
        if local_source():
            self.label(i18n.tr("Gefunden: {} – wird nur eingelesen.", local_source()), "trx-ctx")
        self.prog = Gtk.ProgressBar(visible=self._installing)
        self.results.append(self.prog)
        b = Gtk.Button(label="󰇚 wörterbuch einrichten", focus_on_click=False, halign=Gtk.Align.START,
                       sensitive=not self._installing)
        b.add_css_class("trx-dir")
        b.connect("clicked", lambda *_: self.start_install(b))
        self.results.append(b)

    def start_install(self, btn):
        self._installing = True
        btn.set_sensitive(False)
        self.prog.set_visible(True)

        def progress(text, frac):
            self.status.set_label(text)
            if getattr(self, "prog", None) is not None:
                self.prog.set_fraction(frac) if frac is not None else self.prog.pulse()

        def done(err):
            self._installing = False
            self.status.set_label(i18n.tr("Fehler: {}", err) if err else i18n.tr("wörterbuch bereit"))
            self.queue(0)
        install(progress, done)

    def copy(self, text):
        self.get_clipboard().set(text)
        self.status.set_label(i18n.tr("kopiert: {}", text))
        self.status.add_css_class("trx-ok")
        GLib.timeout_add(1600, lambda: (self.status.remove_css_class("trx-ok"), False)[1])

    def copy_first(self):
        if self.first:
            self.copy(self.first)

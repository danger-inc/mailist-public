# Lokaler Zwischenspeicher (SQLite) + Abgleich mit Gmail.
# Die Liste kommt immer sofort aus der Datenbank; Änderungen (gelesen, Stern, löschen, senden) gelten sofort
# lokal und landen in einer Warteschlange, die der Abgleich zu Gmail schickt – auch nach einer Offline-Phase.
# Abgleich: erst einmal die neuesten N Mails (Kopfzeilen per Sammelanfrage), danach nur noch die Änderungen
# seit dem letzten Stand (Gmail-History). Mail-Texte werden erst beim Öffnen geladen und dann zwischengespeichert.
import email.header, email.utils, json, os, sqlite3, threading, time, urllib.error, zlib

import gmail

CACHE = os.path.expanduser(os.environ.get("MAILIST_CACHE", "~/.cache/mailist"))
DB = os.path.join(CACHE, "mail.db")
BODY_KEEP = 600            # so viele Mail-Texte bleiben zwischengespeichert
HIDDEN = ("TRASH", "SPAM")
NOISY = ("CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, thread TEXT, labels TEXT, from_name TEXT, from_addr TEXT, to_addr TEXT,
    subject TEXT, snippet TEXT, date INTEGER, size INTEGER, att INTEGER, unread INTEGER, starred INTEGER,
    hdr TEXT);
CREATE INDEX IF NOT EXISTS m_date ON messages(date DESC);
CREATE INDEX IF NOT EXISTS m_from ON messages(from_addr);
CREATE INDEX IF NOT EXISTS m_thread ON messages(thread);
CREATE TABLE IF NOT EXISTS bodies (id TEXT PRIMARY KEY, raw BLOB, t INTEGER);
CREATE TABLE IF NOT EXISTS labels (id TEXT PRIMARY KEY, name TEXT, type TEXT, total INTEGER, unread INTEGER,
    color TEXT);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS pending (n INTEGER PRIMARY KEY AUTOINCREMENT, op TEXT);
"""


def dec(s):
    """RFC-2047-kodierte Kopfzeile lesbar machen (Gmail liefert meist schon Klartext)."""
    if not s or "=?" not in s:
        return s or ""
    try:
        return str(email.header.make_header(email.header.decode_header(s)))
    except Exception:
        return s


def split_addr(value):
    name, addr = email.utils.parseaddr(dec(value or ""))
    return name.strip().strip('"'), addr.strip().lower()


def addr_list(value):
    return [(n.strip().strip('"'), a.strip().lower()) for n, a in email.utils.getaddresses([dec(value or "")]) if a]


class Store:
    def __init__(self, path=DB):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.local = threading.local()
        self.lock = threading.RLock()        # Schreiben aus mehreren Threads nacheinander
        c = self.db
        c.executescript(SCHEMA)
        c.commit()
        self.me = self.get("me") or ""

    @property
    def db(self):
        c = getattr(self.local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            self.local.c = c
        return c

    # ---------------- kv
    def get(self, k, default=None):
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def put(self, k, v):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, None if v is None else str(v)))
            self.db.commit()

    def reset(self):
        """Alles Zwischengespeicherte weg (beim Abmelden / anderes Konto)."""
        with self.lock:
            for t in ("messages", "bodies", "labels", "kv", "pending"):
                self.db.execute(f"DELETE FROM {t}")
            self.db.commit()
        self.me = ""

    # ---------------- Einträge
    def ingest(self, metas):
        """Gmail-Metadaten speichern (neu oder aktualisiert)."""
        rows = []
        for m in metas:
            h = {x["name"].lower(): x.get("value", "") for x in (m.get("payload") or {}).get("headers", [])}
            fn, fa = split_addr(h.get("from"))
            labels = m.get("labelIds") or []
            mt = (m.get("payload") or {}).get("mimeType", "")
            extra = {k: h[k] for k in ("reply-to", "return-path", "authentication-results", "list-unsubscribe",
                                       "message-id", "cc") if h.get(k)}
            rows.append((m["id"], m.get("threadId"), " " + " ".join(labels) + " ", fn, fa, dec(h.get("to")),
                         dec(h.get("subject")), m.get("snippet", ""), int(m.get("internalDate") or 0),
                         int(m.get("sizeEstimate") or 0), int(mt in ("multipart/mixed", "multipart/report")),
                         int("UNREAD" in labels), int("STARRED" in labels), json.dumps(extra)))
        if rows:
            with self.lock:
                self.db.executemany("INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                self.db.commit()

    def known(self, ids):
        out = set()
        ids = list(ids)
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            q = ",".join("?" * len(part))
            out.update(r[0] for r in self.db.execute(f"SELECT id FROM messages WHERE id IN ({q})", part))
        return out

    def message(self, mid):
        return self.db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()

    def set_labels(self, ids, add=(), remove=()):
        with self.lock:
            for mid in ids:
                r = self.db.execute("SELECT labels FROM messages WHERE id=?", (mid,)).fetchone()
                if not r:
                    continue
                labs = [x for x in r[0].split() if x not in remove]
                labs += [a for a in add if a not in labs]
                self.db.execute("UPDATE messages SET labels=?, unread=?, starred=? WHERE id=?",
                                (" " + " ".join(labs) + " ", int("UNREAD" in labs), int("STARRED" in labs), mid))
            self.db.commit()

    def drop(self, ids):
        with self.lock:
            for mid in ids:
                self.db.execute("DELETE FROM messages WHERE id=?", (mid,))
                self.db.execute("DELETE FROM bodies WHERE id=?", (mid,))
            self.db.commit()

    # ---------------- Abfragen für die Ansichten
    @staticmethod
    def where_for(folder):
        """Ordner-Schlüssel → (SQL-Bedingung, Parameter). folder: Label-ID, 'ALL', 'ALLMAIL', 'PRIMARY'."""
        hide = " AND ".join(f"labels NOT LIKE '% {h} %'" for h in HIDDEN)
        if folder in HIDDEN:
            return "labels LIKE ?", [f"% {folder} %"]
        if folder == "ALLMAIL":
            return hide, []
        if folder == "PRIMARY":
            noisy = " AND ".join(f"labels NOT LIKE '% {n} %'" for n in NOISY + ("CATEGORY_UPDATES",))
            return f"labels LIKE '% INBOX %' AND {noisy} AND {hide}", []
        return f"labels LIKE ? AND {hide}", [f"% {folder} %"]

    def listing(self, folder, text=None, sender=None, ids=None, limit=3000):
        if ids is not None:
            if not ids:
                return []
            q = ",".join("?" * len(ids))
            return self.db.execute(f"SELECT * FROM messages WHERE id IN ({q}) ORDER BY date DESC", list(ids)).fetchall()
        w, p = self.where_for(folder)
        if sender:
            w += " AND from_addr = ?"
            p.append(sender)
        if text:
            for word in text.lower().split():
                w += " AND (lower(subject) LIKE ? OR lower(from_name) LIKE ? OR from_addr LIKE ? OR lower(snippet) LIKE ?" \
                     " OR lower(to_addr) LIKE ?)"
                p += [f"%{word}%"] * 5
        return self.db.execute(f"SELECT * FROM messages WHERE {w} ORDER BY date DESC LIMIT ?", p + [limit]).fetchall()

    def count_local(self, folder, unread=False):
        w, p = self.where_for(folder)
        if unread:
            w += " AND unread=1"
        return self.db.execute(f"SELECT COUNT(*) FROM messages WHERE {w}", p).fetchone()[0]

    def oldest(self, folder):
        w, p = self.where_for(folder)
        r = self.db.execute(f"SELECT MIN(date) FROM messages WHERE {w}", p).fetchone()
        return r[0] if r and r[0] else None

    def senders(self, folder="ALLMAIL", limit=400):
        """Aufräumen: wer schickt am meisten? → [(addr, name, anzahl, größe, neueste, ungelesen)]"""
        w, p = self.where_for(folder)
        return self.db.execute(
            f"SELECT from_addr, MAX(from_name), COUNT(*), SUM(size), MAX(date), SUM(unread), "
            f"MAX(CASE WHEN hdr LIKE '%list-unsubscribe%' THEN 1 ELSE 0 END) FROM messages "
            f"WHERE {w} AND from_addr != '' AND from_addr != ? GROUP BY from_addr "
            f"ORDER BY COUNT(*) DESC, MAX(date) DESC LIMIT ?", p + [self.me, limit]).fetchall()

    def ids_from(self, addr, folder="ALLMAIL"):
        w, p = self.where_for(folder)
        return [r[0] for r in self.db.execute(f"SELECT id FROM messages WHERE {w} AND from_addr=?", p + [addr])]

    def thread(self, tid):
        return self.db.execute("SELECT * FROM messages WHERE thread=? ORDER BY date", (tid,)).fetchall()

    def sent_before(self, addr, before_ms):
        """Wie viele Mails kamen von dieser Adresse schon vorher? (neuer Absender = Vorsicht)"""
        return self.db.execute("SELECT COUNT(*) FROM messages WHERE from_addr=? AND date<?",
                               (addr, before_ms)).fetchone()[0] + self.db.execute(
            "SELECT COUNT(*) FROM messages WHERE labels LIKE '% SENT %' AND lower(to_addr) LIKE ?",
            (f"%{addr}%",)).fetchone()[0]

    def contacts(self, prefix, limit=8):
        pre = f"%{prefix.lower()}%"
        rows = self.db.execute(
            "SELECT from_name, from_addr, COUNT(*) c FROM messages WHERE (lower(from_name) LIKE ? OR from_addr LIKE ?)"
            " AND from_addr != ? AND labels NOT LIKE '% SPAM %' GROUP BY from_addr ORDER BY c DESC LIMIT ?",
            (pre, pre, self.me, limit)).fetchall()
        return [(r[0], r[1]) for r in rows]

    # ---------------- Label
    def save_labels(self, labs):
        with self.lock:
            for l in labs:
                col = (l.get("color") or {}).get("backgroundColor")
                old = self.db.execute("SELECT total, unread FROM labels WHERE id=?", (l["id"],)).fetchone()
                self.db.execute("INSERT OR REPLACE INTO labels VALUES (?,?,?,?,?,?)",
                                (l["id"], l.get("name", l["id"]), l.get("type", "user"),
                                 l.get("messagesTotal", old[0] if old else None),
                                 l.get("messagesUnread", old[1] if old else None), col))
            known = {l["id"] for l in labs}
            for (lid,) in self.db.execute("SELECT id FROM labels").fetchall():
                if lid not in known:
                    self.db.execute("DELETE FROM labels WHERE id=?", (lid,))
            self.db.commit()

    def save_counts(self, counts):
        with self.lock:
            for lid, js in counts.items():
                self.db.execute("UPDATE labels SET total=?, unread=? WHERE id=?",
                                (js.get("messagesTotal"), js.get("messagesUnread"), lid))
            self.db.commit()

    def labels(self):
        return self.db.execute("SELECT * FROM labels").fetchall()

    def label_unread(self, lid):
        r = self.db.execute("SELECT unread FROM labels WHERE id=?", (lid,)).fetchone()
        return r[0] if r and r[0] is not None else None

    # ---------------- Texte
    def body(self, mid):
        r = self.db.execute("SELECT raw FROM bodies WHERE id=?", (mid,)).fetchone()
        if not r:
            return None
        try:
            return zlib.decompress(r[0])
        except zlib.error:
            return None

    def save_body(self, mid, raw):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO bodies VALUES (?,?,?)", (mid, zlib.compress(raw, 6), int(time.time())))
            n = self.db.execute("SELECT COUNT(*) FROM bodies").fetchone()[0]
            if n > BODY_KEEP:
                self.db.execute("DELETE FROM bodies WHERE id IN (SELECT id FROM bodies ORDER BY t LIMIT ?)",
                                (n - BODY_KEEP,))
            self.db.commit()

    # ---------------- umbenannte Mails (IMAP: Verschieben vergibt eine neue Nummer)
    def alias(self, old, new):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", ("alias:" + old, new))
            self.db.execute("UPDATE OR REPLACE messages SET id=? WHERE id=?", (new, old))
            self.db.execute("UPDATE OR REPLACE bodies SET id=? WHERE id=?", (new, old))
            self.db.commit()

    def resolve(self, ids):
        out = []
        for i in ids:
            seen = 0
            while seen < 20:
                r = self.db.execute("SELECT v FROM kv WHERE k=?", ("alias:" + i,)).fetchone()
                if not r:
                    break
                i, seen = r[0], seen + 1
            out.append(i)
        return out

    # ---------------- Warteschlange
    def queue(self, op):
        with self.lock:
            self.db.execute("INSERT INTO pending (op) VALUES (?)", (json.dumps(op),))
            self.db.commit()

    def pending(self):
        return [(r[0], json.loads(r[1])) for r in self.db.execute("SELECT n, op FROM pending ORDER BY n")]

    def done(self, n):
        with self.lock:
            self.db.execute("DELETE FROM pending WHERE n=?", (n,))
            self.db.commit()

    def size_on_disk(self):
        try:
            return sum(os.path.getsize(self.path + s) for s in ("", "-wal") if os.path.exists(self.path + s))
        except OSError:
            return 0


# ====================================================================== Abgleich
class Syncer:
    """Alles, was mit dem Postfach spricht, läuft hier – nacheinander in einem Arbeits-Thread je Konto.
    Was genau passiert, weiß das Backend (GmailBackend hier, ImapBackend in imapmail.py).
    on_event(art, daten) wird im Arbeits-Thread gerufen (UI muss selbst in den GTK-Thread wechseln)."""

    def __init__(self, store, on_event, limit=2000, backend=None, name="mailist-sync"):
        self.st = store
        self.on_event = on_event
        self.limit = limit
        self.backend = backend or GmailBackend()
        self.jobs = []
        self.cv = threading.Condition()
        self.busy = False
        self.thread = threading.Thread(target=self._loop, daemon=True, name=name)
        self.thread.start()

    # ---- Aufträge
    def request(self, kind, *args):
        with self.cv:
            if kind in ("sync", "flush") and any(j[0] == kind for j in self.jobs):
                return
            self.jobs.append((kind, args))
            self.cv.notify()

    def _loop(self):
        while True:
            with self.cv:
                while not self.jobs:
                    self.cv.wait()
                kind, args = self.jobs.pop(0)
            self.busy = True
            try:
                with self.backend.bound():
                    getattr(self, "_job_" + kind)(*args)
            except gmail.ExpiredError:
                self.on_event("expired", None)
            except gmail.AuthError as e:
                self.on_event("auth", str(e))
            except gmail.ApiError as e:
                self.on_event("api", e)
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                self.on_event("offline", str(e))
            except Exception as e:     # nie den Arbeits-Thread verlieren
                import traceback
                traceback.print_exc()
                self.on_event("error", f"{type(e).__name__}: {e}")
            finally:
                self.busy = False
                self.on_event("idle", kind)

    def ready(self):
        b = self.backend
        return b.logged_in() and not b.expired()

    # ---- Aufträge → Backend
    def _job_sync(self):
        if not self.ready():
            return
        if not self.backend.online():
            self.on_event("offline", "keine Verbindung")
            return
        self.on_event("busy", "abgleich …")
        self.flush()
        new = self.backend.sync(self)
        self.st.put("synced", int(time.time()))
        self.on_event("synced", new)

    def flush(self):
        """Eigene Änderungen ans Postfach schicken (in Reihenfolge; Netzfehler → später erneut)."""
        b = self.backend
        errors = []
        for n, op in self.st.pending():
            op = dict(op)
            op["ids"] = self.st.resolve(op.get("ids", []))
            if op.get("respam"):
                op["respam"] = self.st.resolve(op["respam"])
            try:
                b.apply(self, op)
                if op["op"] == "send":
                    self.on_event("sent", op.get("subject", ""))
                self.st.done(n)
            except gmail.ApiError as e:
                if e.status in (400, 403, 404):
                    self.st.done(n)                      # dauerhaft abgelehnt – nicht ewig wiederholen
                    bad = list(getattr(e, "failed", None) or op["ids"])
                    try:
                        b.resync(self, bad)              # lokal wieder auf den echten Stand
                    except Exception:
                        pass
                    what = {"modify": "ändern", "trash": "löschen", "untrash": "wiederherstellen",
                            "delete": "endgültig löschen", "send": "senden"}.get(op["op"], op["op"])
                    msg = getattr(e, "args", [""])[0]
                    errors.append(f"{what}: {b.name} hat abgelehnt – {msg}")
                    print(f"mailist: {op['op']} abgelehnt: {e} {e.reason} ids={bad[:5]}", flush=True)
                    if e.status == 403 and e.reason in ("insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"):
                        errors[-1] = "Berechtigung fehlt – bitte neu anmelden"
                else:
                    if e.reason == "rate":
                        errors.append(f"{b.name} bremst gerade – deine änderungen gehen in einer halben minute raus")
                        threading.Timer(30, lambda: self.request("flush")).start()
                    else:
                        errors.append(f"{op['op']}: {e} – später erneut")
                    break
        if errors:
            self.on_event("errors", errors)

    def _job_flush(self):
        if self.ready() and self.backend.online():
            if not self.st.pending():
                return
            self.flush()
            self.backend.after_flush(self)

    def _job_more(self, folder):
        self.backend.more(self, folder)

    def _job_search(self, query, token):
        self.backend.search(self, query, token)


class GmailBackend:
    """Gmail über die REST-API. token: Pfad der Token-Datei dieses Kontos."""
    kind = "gmail"
    name = "gmail"
    categories = True             # Gmail-Kategorien (Werbung, Soziale Netze …) und „wichtig“
    labels_are_folders = False    # eine Mail kann mehrere Label haben

    def __init__(self, token=None):
        self.token = token or gmail.TOKEN

    def bound(self):
        return gmail.bound(self.token)

    def logged_in(self):
        with self.bound():
            return gmail.logged_in()

    def expired(self):
        with self.bound():
            return gmail.expired()

    def signed_in_at(self):
        with self.bound():
            return gmail.signed_in_at()

    def online(self):
        return gmail.online()

    def raw(self, mid):
        with self.bound():
            return gmail.raw(mid)[0]

    def attachment(self, mid, aid):
        with self.bound():
            return gmail.attachment(mid, aid)

    def logout(self):
        with self.bound():
            gmail.logout()

    # ---- Abgleich
    def sync(self, sy):
        st = sy.st
        hid = st.get("history")
        if not st.me:
            prof = gmail.profile()
            st.me = prof.get("emailAddress", "").lower()
            st.put("me", st.me)
        st.save_labels(gmail.labels())
        new = []
        if hid:
            try:
                new = self.incremental(sy, hid)
            except gmail.ApiError as e:
                if e.status != 404:
                    raise
                self.full(sy)
        else:
            self.full(sy)
        ids = [r["id"] for r in st.labels()]
        st.save_counts(gmail.label_counts(ids))
        return new

    def full(self, sy):
        """Erstabgleich (oder History zu alt): neueste N Mails, Stück für Stück, Neueste zuerst."""
        st = sy.st
        hid = gmail.profile().get("historyId")
        ids, _ = gmail.list_ids(limit=sy.limit, spam_trash=True)
        known = st.known(ids)
        stale = [i for i in ids if i in known]
        todo = [i for i in ids if i not in known] + stale        # bekannte am Ende auffrischen (Label)
        done = 0
        for i in range(0, len(todo), 120):
            part = todo[i:i + 120]
            metas = gmail.metadata(part)
            st.ingest(metas.values())
            done += len(part)
            sy.on_event("progress", (done, len(todo)))
        if ids:
            # was lokal im selben Zeitraum liegt, aber bei Gmail fehlt, ist dort gelöscht
            oldest = st.db.execute("SELECT MIN(date) FROM messages WHERE id IN (%s)" % ",".join(
                "?" * min(len(ids), 900)), ids[-900:]).fetchone()[0] or 0
            have = {r[0] for r in st.db.execute("SELECT id FROM messages WHERE date >= ?", (oldest,))}
            gone = have - set(ids)
            if gone:
                st.drop(gone)
        st.put("history", hid)

    def incremental(self, sy, hid):
        st = sy.st
        hist, newhid = gmail.history(hid)
        add, fetch, drop = [], set(), set()
        for h in hist:
            for x in h.get("messagesAdded", []):
                m = x["message"]
                fetch.add(m["id"])
                if "INBOX" in m.get("labelIds", []) and "UNREAD" in m.get("labelIds", []) \
                        and not any(n in m.get("labelIds", []) for n in NOISY):
                    add.append(m["id"])
            for x in h.get("messagesDeleted", []):
                drop.add(x["message"]["id"])
            for key in ("labelsAdded", "labelsRemoved"):
                for x in h.get(key, []):
                    mid = x["message"]["id"]
                    if mid in fetch or mid in drop:
                        continue
                    labs = x["message"].get("labelIds")
                    if labs is not None and st.known([mid]):
                        with st.lock:
                            st.db.execute("UPDATE messages SET labels=?, unread=?, starred=? WHERE id=?",
                                          (" " + " ".join(labs) + " ", int("UNREAD" in labs),
                                           int("STARRED" in labs), mid))
                            st.db.commit()
                    else:
                        fetch.add(mid)
        fetch -= drop
        if drop:
            st.drop(drop)
        if fetch:
            metas = gmail.metadata(list(fetch))
            st.ingest(metas.values())
        st.put("history", newhid)
        return [m for m in add if m not in drop]

    def apply(self, sy, op):
        ids = op.get("ids", [])
        k = op["op"]
        if k == "modify":
            gmail.modify(ids, op.get("add", []), op.get("remove", []))
        elif k == "trash":
            gmail.trash(ids)
        elif k == "untrash":
            gmail.trash(ids, undo=True)
            if op.get("respam"):
                gmail.modify(op["respam"], ["SPAM"], ["INBOX"])
        elif k == "delete":
            gmail.delete(ids)
        elif k == "send":
            import base64
            gmail.send(base64.b64decode(op["raw"]), op.get("thread"))

    def resync(self, sy, ids):
        """Nach einer Ablehnung: Kopfdaten frisch holen, damit die Liste wieder stimmt."""
        if not ids:
            return
        try:
            metas = gmail.metadata(ids)
        except (gmail.ApiError, OSError):
            return
        sy.st.ingest(metas.values())

    def after_flush(self, sy):
        ids = [r["id"] for r in sy.st.labels()]
        sy.st.save_counts(gmail.label_counts(ids))       # Zähler links (ungelesen) gleich auffrischen

    def more(self, sy, folder, want=150):
        """Ältere Mails eines Ordners nachladen – lückenlos: Gmail-Liste Seite für Seite weiterblättern
        (Stand je Ordner gemerkt) und nur Unbekannte holen. Früher ging es ab der ältesten lokalen Mail weiter;
        lag durch eine Suche eine sehr alte Mail im Speicher, sprang das um Jahre zurück."""
        st = sy.st
        key = "more:" + folder
        label = None if folder in ("ALLMAIL", "PRIMARY") else folder
        q = "category:primary" if folder == "PRIMARY" else None
        if folder == "PRIMARY":
            label = "INBOX"
        page = st.get(key) or None
        if page == "END":
            sy.on_event("more", (folder, 0, False))
            return
        need, seen = [], set()
        for _ in range(60):
            try:
                ids, nxt = gmail.list_ids(label=label, q=q, limit=250, page=page, spam_trash=folder in HIDDEN)
            except gmail.ApiError as e:
                if page and e.status == 400:      # Seitenmarke abgelaufen → von vorn
                    page = None
                    continue
                raise
            known = st.known(ids)
            need += [i for i in ids if i not in known and i not in seen]
            seen.update(ids)
            page = nxt
            if not page or len(need) >= want:
                break
        for i in range(0, len(need), 120):
            st.ingest(gmail.metadata(need[i:i + 120]).values())
            sy.on_event("progress", (min(i + 120, len(need)), len(need)))
        st.put(key, page or "END")           # erst merken, wenn alles da ist (sonst Lücke)
        sy.on_event("more", (folder, len(need), bool(page)))

    def search(self, sy, query, token):
        ids, _ = gmail.list_ids(q=query, limit=300)
        need = [i for i in ids if i not in sy.st.known(ids)]
        for i in range(0, len(need), 120):
            sy.st.ingest(gmail.metadata(need[i:i + 120]).values())
        sy.on_event("search", (token, ids))


def apply_local(st, op):
    """Änderung sofort lokal sichtbar machen; gibt die Gegen-Operation (für Rückgängig) zurück."""
    ids = op["ids"] = st.resolve(op["ids"])
    if op.get("respam"):
        op["respam"] = st.resolve(op["respam"])
    k = op["op"]
    if k == "modify":
        st.set_labels(ids, op.get("add", []), op.get("remove", []))
        return {"op": "modify", "ids": ids, "add": op.get("remove", []), "remove": op.get("add", [])}
    if k == "trash":
        # Spam und Papierkorb schließen sich bei Gmail aus → Spam-Label lokal mit entfernen, fürs Rückgängig merken
        spam = [i for i in ids if (r := st.message(i)) and " SPAM " in (r["labels"] or "")]
        st.set_labels(ids, ["TRASH"], ["SPAM"])
        inv = {"op": "untrash", "ids": ids}
        if spam:
            inv["respam"] = spam
        return inv
    if k == "untrash":
        st.set_labels(ids, [], ["TRASH"])
        if op.get("respam"):
            st.set_labels(op["respam"], ["SPAM"], ["INBOX"])
        return {"op": "trash", "ids": ids}
    if k == "delete":
        st.drop(ids)
        return None
    return None

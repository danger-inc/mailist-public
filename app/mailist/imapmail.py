# IMAP/SMTP-Postfächer (GMX, web.de, Outlook, Uni-Mail …) im selben Modell wie Gmail: jeder IMAP-Ordner ist
# ein „Label“ (Posteingang = INBOX, Gesendet = SENT …), gelesen/markiert sind Label (UNREAD/STARRED) wie bei
# Gmail. Eine Mail liegt in genau einem Ordner; „Label setzen“ heißt hier „in den Ordner verschieben“.
# Mail-ID = <ordnerkürzel>.<uid>. Verschieben vergibt eine neue UID → Store.alias(alt, neu).
import base64, contextlib, email, email.policy, email.utils, hashlib, imaplib, json, re, smtplib, socket, ssl
import threading, time

import gmail                       # Fehlerklassen (AuthError, ApiError) – gleiche Behandlung wie bei Gmail
import vault

HEAD_FIELDS = ("FROM TO CC SUBJECT DATE MESSAGE-ID REPLY-TO RETURN-PATH LIST-UNSUBSCRIBE AUTHENTICATION-RESULTS "
               "REFERENCES IN-REPLY-TO CONTENT-TYPE")
FIRST = {"INBOX": 400, "SENT": 150, "DRAFT": 50, "TRASH": 60, "SPAM": 60}      # Erstabgleich je Ordner
FIRST_OTHER = 120
MORE = 150
# kein Mail-Ordner (Exchange/Outlook zeigen Kalender & Co. per IMAP mit an)
NOT_MAIL = {"calendar", "kalender", "contacts", "kontakte", "tasks", "aufgaben", "notes", "notizen", "journal",
            "sync issues", "synchronisierungsprobleme", "outbox", "postausgang", "rss feeds", "rss-feeds",
            "conversation history", "unterhaltungsverlauf", "conflicts", "konflikte", "local failures",
            "lokale fehler", "server failures", "serverfehler", "yammer root", "files", "dateien"}
SPECIAL = {"\\Sent": "SENT", "\\Drafts": "DRAFT", "\\Trash": "TRASH", "\\Junk": "SPAM", "\\Archive": "ARCHIVE"}
BY_NAME = {
    "SENT": ("sent", "sent items", "sent messages", "sent mail", "gesendet", "gesendete elemente",
             "gesendete objekte", "gesendete nachrichten"),
    "DRAFT": ("drafts", "draft", "entwürfe", "entwurf"),
    "TRASH": ("trash", "deleted items", "deleted messages", "papierkorb", "gelöschte elemente",
              "gelöschte objekte", "bin"),
    "SPAM": ("spam", "junk", "junk e-mail", "junk-e-mail", "junk email", "bulk mail", "unerwünscht"),
    "ARCHIVE": ("archive", "archiv", "archives"),
}

PRESETS = {
    "gmx": {"name": "gmx", "imap_host": "imap.gmx.net", "imap_port": 993, "imap_tls": "ssl",
            "smtp_host": "mail.gmx.net", "smtp_port": 587, "smtp_tls": "starttls", "save_sent": True},
    "webde": {"name": "web.de", "imap_host": "imap.web.de", "imap_port": 993, "imap_tls": "ssl",
              "smtp_host": "smtp.web.de", "smtp_port": 587, "smtp_tls": "starttls", "save_sent": True},
    "outlook": {"name": "outlook", "imap_host": "outlook.office365.com", "imap_port": 993, "imap_tls": "ssl",
                "smtp_host": "smtp-mail.outlook.com", "smtp_port": 587, "smtp_tls": "starttls", "save_sent": False},
    "posteo": {"name": "posteo", "imap_host": "posteo.de", "imap_port": 993, "imap_tls": "ssl",
               "smtp_host": "posteo.de", "smtp_port": 587, "smtp_tls": "starttls", "save_sent": True},
    "mailbox": {"name": "mailbox.org", "imap_host": "imap.mailbox.org", "imap_port": 993, "imap_tls": "ssl",
                "smtp_host": "smtp.mailbox.org", "smtp_port": 587, "smtp_tls": "starttls", "save_sent": True},
    "icloud": {"name": "icloud", "imap_host": "imap.mail.me.com", "imap_port": 993, "imap_tls": "ssl",
               "smtp_host": "smtp.mail.me.com", "smtp_port": 587, "smtp_tls": "starttls", "save_sent": True},
}
DOMAINS = {"gmx.de": "gmx", "gmx.net": "gmx", "gmx.at": "gmx", "gmx.ch": "gmx", "web.de": "webde",
           "outlook.com": "outlook", "outlook.de": "outlook", "hotmail.com": "outlook", "hotmail.de": "outlook",
           "live.com": "outlook", "live.de": "outlook", "msn.com": "outlook", "posteo.de": "posteo",
           "posteo.net": "posteo", "mailbox.org": "mailbox", "icloud.com": "icloud", "me.com": "icloud",
           "mac.com": "icloud"}

security = {"require_tls": True, "verify_cert": True}      # von der App aus den Einstellungen gesetzt


def guess(address):
    """Bekannter Anbieter zur Adresse → Voreinstellung (oder None)."""
    dom = (address or "").rsplit("@", 1)[-1].lower()
    return dict(PRESETS[DOMAINS[dom]]) if dom in DOMAINS else None


# ---------------------------------------------------------------- IMAP-UTF-7 (Ordnernamen)
def utf7_decode(s):
    out, i = [], 0
    while i < len(s):
        if s[i] == "&":
            j = s.index("-", i)
            if j == i + 1:
                out.append("&")
            else:
                b = s[i + 1:j].replace(",", "/")
                b += "=" * (-len(b) % 4)
                out.append(base64.b64decode(b).decode("utf-16-be", "replace"))
            i = j + 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def utf7_encode(s):
    out, buf = [], []

    def flush():
        if buf:
            b = base64.b64encode("".join(buf).encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            out.append("&" + b + "-")
            buf.clear()
    for ch in s:
        if 0x20 <= ord(ch) <= 0x7e:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def _q(name):
    """Ordnername für einen IMAP-Befehl (UTF-7, in Anführungszeichen)."""
    return '"' + utf7_encode(name).replace("\\", "\\\\").replace('"', '\\"') + '"'


def fkey(name):
    return hashlib.sha1(name.encode()).hexdigest()[:6]


def split_id(mid):
    k, _, u = mid.partition(".")
    return k, int(u) if u.isdigit() else None


def uid_set(uids):
    """[1,2,3,7] → '1:3,7'"""
    uids = sorted(set(uids))
    out, i = [], 0
    while i < len(uids):
        j = i
        while j + 1 < len(uids) and uids[j + 1] == uids[j] + 1:
            j += 1
        out.append(str(uids[i]) if i == j else f"{uids[i]}:{uids[j]}")
        i = j + 1
    return ",".join(out)


def expand_set(s):
    out = []
    for part in s.split(","):
        if ":" in part:
            a, b = part.split(":")
            a, b = int(a), int(b)
            out += list(range(a, b + 1)) if a <= b else list(range(a, b - 1, -1))
        elif part:
            out.append(int(part))
    return out


def _err(e):
    t = e.args[0] if getattr(e, "args", None) else e
    return _txt(t)


def _txt(t):
    if isinstance(t, tuple):
        t = b" ".join(x for x in t if isinstance(x, bytes))
    return t.decode(errors="replace") if isinstance(t, bytes) else str(t)


class Conn:
    """Eine IMAP-Verbindung mit Fehlerübersetzung. Nicht threadsicher – jede Rolle hat ihre eigene."""

    def __init__(self, conf, password):
        self.conf = conf
        host, port, tls = conf["imap_host"], int(conf.get("imap_port") or 993), conf.get("imap_tls", "ssl")
        ctx = _context()
        try:
            if tls == "ssl":
                self.c = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=30)
            else:
                self.c = imaplib.IMAP4(host, port, timeout=30)
                if tls == "starttls":
                    self.c.starttls(ssl_context=ctx)
                elif security["require_tls"]:
                    raise gmail.AuthError("unverschlüsselte verbindung abgelehnt (einstellungen → sicherheit)")
        except ssl.SSLCertVerificationError as e:
            raise gmail.AuthError(f"zertifikat des servers ungültig: {e.verify_message}") from None
        except (socket.gaierror, ConnectionRefusedError) as e:
            raise OSError(f"server {host} nicht erreichbar ({e})") from None
        try:
            self.c.login(conf.get("user") or conf["address"], password)
        except imaplib.IMAP4.abort as e:
            raise OSError(_err(e)) from None
        except imaplib.IMAP4.error as e:
            raise gmail.AuthError(f"anmeldung abgelehnt – benutzername/passwort prüfen ({_err(e)})") from None
        try:
            typ, dat = self.c.capability()
            caps = (dat[0] or b"").decode(errors="replace").split() if typ == "OK" else list(self.c.capabilities)
        except imaplib.IMAP4.error:
            caps = list(self.c.capabilities)
        self.caps = {str(x).upper() for x in caps}
        self.selected = None
        self.last = time.time()

    def has(self, cap):
        return cap.upper() in self.caps

    def cmd(self, fn, *args):
        try:
            typ, data = getattr(self.c, fn)(*args)
        except imaplib.IMAP4.readonly as e:
            raise gmail.ApiError(400, f"ordner ist schreibgeschützt ({_err(e)})") from None
        except imaplib.IMAP4.abort as e:
            raise OSError(f"verbindung abgebrochen: {_err(e)}") from None
        except imaplib.IMAP4.error as e:
            raise gmail.ApiError(400, _err(e)) from None
        self.last = time.time()
        if typ != "OK":
            raise gmail.ApiError(400, " ".join(_txt(d) for d in data if d) or typ)
        return data

    def uid(self, *args):
        try:
            typ, data = self.c.uid(*args)
        except imaplib.IMAP4.readonly as e:
            raise gmail.ApiError(400, f"ordner ist schreibgeschützt ({_err(e)})") from None
        except imaplib.IMAP4.abort as e:
            raise OSError(f"verbindung abgebrochen: {_err(e)}") from None
        except imaplib.IMAP4.error as e:
            raise gmail.ApiError(400, _err(e)) from None
        self.last = time.time()
        if typ != "OK":
            raise gmail.ApiError(400, " ".join(_txt(d) for d in data if d) or typ)
        return data

    def select(self, name, readonly=False):
        data = self.cmd("select", _q(name), readonly)
        self.selected = name
        uv = self.c.untagged_responses.get("UIDVALIDITY", [b"0"])[-1]
        return int(data[0] or 0), int(uv)

    def close(self):
        try:
            self.c.logout()
        except Exception:
            pass


def _context():
    ctx = ssl.create_default_context()
    if not security["verify_cert"]:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


_RE_LIST = re.compile(rb'\((?P<flags>[^)]*)\) (?P<delim>NIL|"(?:[^"\\]|\\.)*") (?P<name>.*)$')


def parse_list(data):
    out = []
    i = 0
    while i < len(data):
        d = data[i]
        if d is None:
            i += 1
            continue
        if isinstance(d, tuple):          # Name als Literal
            head, name = d[0], d[1]
            m = _RE_LIST.match(head.rsplit(b" {", 1)[0] + b" X")
            if m:
                out.append((m.group("flags").decode().split(), name.decode()))
            i += 1
            continue
        m = _RE_LIST.match(d)
        if m:
            name = m.group("name").strip()
            if name.startswith(b'"'):
                name = name[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
            out.append((m.group("flags").decode().split(), name.decode()))
        i += 1
    return [(f, utf7_decode(n)) for f, n in out]


_RE_UID = re.compile(rb"UID (\d+)")
_RE_FLAGS = re.compile(rb"FLAGS \(([^)]*)\)")
_RE_DATE = re.compile(rb'INTERNALDATE "([^"]+)"')
_RE_SIZE = re.compile(rb"RFC822\.SIZE (\d+)")


def parse_fetch(data):
    """imaplib-FETCH-Antwort → [{uid, flags, date, size, head}] (Reihenfolge der Teile ist je Server anders)."""
    out = []
    for d in data:
        if isinstance(d, tuple):
            out.append({"meta": d[0], "head": d[1]})
        elif isinstance(d, bytes):
            if re.match(rb"^\d+ \(", d):
                out.append({"meta": d, "head": b""})
            elif out:
                out[-1]["meta"] += b" " + d          # Rest nach dem Literal (z. B. „ FLAGS (\Seen))“)
    res = []
    for c in out:
        m = _RE_UID.search(c["meta"])
        if not m:
            continue
        f = _RE_FLAGS.search(c["meta"])
        dt = _RE_DATE.search(c["meta"])
        sz = _RE_SIZE.search(c["meta"])
        ts = 0
        if dt:
            try:
                ts = int(email.utils.parsedate_to_datetime(
                    _imapdate(dt.group(1).decode())).timestamp() * 1000)
            except (TypeError, ValueError):
                ts = 0
        res.append({"uid": int(m.group(1)), "flags": f.group(1).decode().split() if f else [],
                    "date": ts, "size": int(sz.group(1)) if sz else 0, "head": c["head"] or b""})
    return res


def _imapdate(s):
    # „21-Oct-2026 15:42:01 +0200“ → RFC 2822
    d, t, z = s.split()
    day, mon, year = d.split("-")
    return f"{day} {mon} {year} {t} {z}"


# ====================================================================== Backend
class ImapBackend:
    kind = "imap"
    categories = False
    labels_are_folders = True

    def __init__(self, account_id, conf):
        self.id = account_id
        self.conf = conf
        self.name = conf.get("name") or conf.get("imap_host") or "postfach"
        self._conns = {}
        self.st = None                # setzt das Konto (der Leser holt Texte darüber)
        self._locks = {"sync": threading.RLock(), "fetch": threading.RLock()}

    # ---- Zustand
    @contextlib.contextmanager
    def bound(self):
        yield

    def password(self):
        return vault.get(self.id)

    def logged_in(self):
        return bool(self.conf.get("imap_host") and self.password() is not None)

    def expired(self):
        return False

    def online(self):
        try:
            socket.create_connection((self.conf["imap_host"], int(self.conf.get("imap_port") or 993)),
                                     timeout=4).close()
            return True
        except OSError:
            return False

    def logout(self):
        self.drop_conns()
        vault.forget(self.id)

    def drop_conns(self):
        for c in list(self._conns.values()):
            c.close()
        self._conns.clear()

    def conn(self, role="sync"):
        c = self._conns.get(role)
        if c is not None and time.time() - c.last > 240:      # lange ruhig → Server hat evtl. getrennt
            try:
                c.c.noop()
                c.last = time.time()
            except Exception:
                c.close()
                c = None
        if c is None:
            pw = self.password()
            if pw is None:
                raise gmail.AuthError("passwort fehlt – unter „konten“ eingeben")
            c = self._conns[role] = Conn(self.conf, pw)
        return c

    @contextlib.contextmanager
    def using(self, role="sync"):
        with self._locks[role]:
            try:
                yield self.conn(role)
            except OSError:
                c = self._conns.pop(role, None)
                if c:
                    c.close()
                raise

    @staticmethod
    def test(conf, password):
        """Anmeldung an IMAP und SMTP probieren → None oder Fehlertext."""
        try:
            c = Conn(conf, password)
            c.close()
        except (gmail.AuthError, gmail.ApiError, OSError) as e:
            return f"imap: {e}"
        try:
            s = _smtp(conf, password)
            s.quit()
        except smtplib.SMTPAuthenticationError as e:
            return f"smtp: anmeldung abgelehnt ({e.smtp_error.decode(errors='replace')[:120]})"
        except (smtplib.SMTPException, OSError, gmail.AuthError) as e:
            return f"smtp: {e}"
        return None

    # ---- Ordner
    def folders(self, sy):
        """→ {kürzel: {name, label, special}} (gemerkt in kv „imap:folders“)."""
        with self.using() as c:
            lst = parse_list(c.cmd("list", '""', '"*"'))
        seen_special = {}
        out = {}
        for flags, name in lst:
            fl = {f.capitalize() if f.startswith("\\") else f for f in flags}
            if any(f.lower() in ("\\noselect", "\\nonexistent", "\\all", "\\flagged") for f in flags):
                continue
            low = name.lower()
            top = low.split("/")[0].split(".")[0]
            if low in NOT_MAIL or top in NOT_MAIL:
                continue
            special = None
            if name.upper() == "INBOX":
                special = "INBOX"
            else:
                for f, lab in SPECIAL.items():
                    if f.lower() in {x.lower() for x in fl}:
                        special = lab
            if special and special in seen_special:
                special = None
            if special:
                seen_special[special] = name
            out[fkey(name)] = {"name": name, "special": special}
        # ohne Kennzeichnung: nach Namen raten
        for lab, names in BY_NAME.items():
            if lab in seen_special:
                continue
            for k, f in out.items():
                if not f["special"] and f["name"].lower().split("/")[-1] in names:
                    f["special"] = lab
                    seen_special[lab] = f["name"]
                    break
        for k, f in out.items():
            f["label"] = f["special"] if f["special"] in ("INBOX", "SENT", "DRAFT", "TRASH", "SPAM") else "f:" + k
        sy.st.put("imap:folders", json.dumps(out))
        return out

    def known_folders(self, st):
        try:
            return json.loads(st.get("imap:folders") or "{}")
        except ValueError:
            return {}

    def folder_of_label(self, st, label):
        for k, f in self.known_folders(st).items():
            if f["label"] == label:
                return k, f
        return None, None

    def special(self, st, lab):
        for k, f in self.known_folders(st).items():
            if f.get("special") == lab:
                return k, f
        return None, None

    # ---- Abgleich
    def sync(self, sy):
        st = sy.st
        if not st.me:
            st.me = (self.conf.get("address") or "").lower()
            st.put("me", st.me)
        folders = self.folders(sy)
        labels = []
        new = []
        for k, f in folders.items():
            got = self._sync_folder(sy, k, f)
            labels.append({"id": f["label"], "name": _display(f), "type": "system" if not f["label"].startswith(
                "f:") else "user", "messagesTotal": got[0], "messagesUnread": got[1]})
            new += got[2]
        st.save_labels(labels)
        # Ordner, die es nicht mehr gibt: Mails weg
        keys = set(folders)
        stale = [r[0] for r in st.db.execute("SELECT id FROM messages") if split_id(r[0])[0] not in keys]
        if stale:
            st.drop(stale)
        return new

    def _state(self, st, k):
        try:
            return json.loads(st.get("imap:f:" + k) or "{}")
        except ValueError:
            return {}

    def _sync_folder(self, sy, k, f):
        st = sy.st
        state = self._state(st, k)
        with self.using() as c:
            stat = c.cmd("status", _q(f["name"]), "(MESSAGES UIDNEXT UNSEEN UIDVALIDITY)")
            txt = b" ".join(x if isinstance(x, bytes) else b"" for x in stat).decode(errors="replace")
            num = {m.group(1): int(m.group(2)) for m in re.finditer(r"(MESSAGES|UIDNEXT|UNSEEN|UIDVALIDITY) (\d+)", txt)}
            total, unseen = num.get("MESSAGES", 0), num.get("UNSEEN", 0)
            sig = [num.get("UIDNEXT"), total, unseen]
            if state.get("uv") and num.get("UIDVALIDITY") and state["uv"] != num["UIDVALIDITY"]:
                st.drop([r[0] for r in st.db.execute("SELECT id FROM messages WHERE id LIKE ?", (k + ".%",))])
                state = {}
            first = not state
            if state.get("sig") == sig and time.time() - state.get("checked", 0) < 900:
                return total, unseen, []
            exists, uv = c.select(f["name"], readonly=True)
            all_uids = [int(x) for x in (c.uid("search", None, "ALL")[0] or b"").split()]
            have = {split_id(r[0])[1] for r in st.db.execute("SELECT id FROM messages WHERE id LIKE ?", (k + ".%",))}
            have.discard(None)
            new_uids = []
            if first:
                n = FIRST.get(f["label"], FIRST_OTHER)
                want = all_uids[-n:]
            else:
                top = state.get("top", 0)
                want = [u for u in all_uids if u > top]
            want = [u for u in want if u not in have]
            # weg (anderswo gelöscht/verschoben)
            cur = set(all_uids)
            gone = [f"{k}.{u}" for u in have if u not in cur]
            if gone:
                st.drop(gone)
            # Flags der bekannten auffrischen
            keep = sorted(u for u in have if u in cur)
            if keep:
                rows = parse_fetch(c.uid("fetch", f"{keep[0]}:*", "(UID FLAGS)"))
                with st.lock:
                    for r in rows:
                        mid = f"{k}.{r['uid']}"
                        old = st.db.execute("SELECT labels FROM messages WHERE id=?", (mid,)).fetchone()
                        if not old:
                            continue
                        labs = self._labels(f["label"], r["flags"])
                        if old[0] != labs:
                            st.db.execute("UPDATE messages SET labels=?, unread=?, starred=? WHERE id=?",
                                          (labs, int(" UNREAD " in labs), int(" STARRED " in labs), mid))
                    st.db.commit()
            if want:
                new_uids = self._fetch_heads(sy, c, k, f, want)
            state.update(uv=uv, sig=sig, checked=time.time(), top=max(all_uids or [state.get("top", 0)]))
            st.put("imap:f:" + k, json.dumps(state))
        fresh = [] if first or f["label"] != "INBOX" else [
            f"{k}.{u}" for u in new_uids if (m := st.message(f"{k}.{u}")) and m["unread"]]
        return total, unseen, fresh

    @staticmethod
    def _labels(label, flags):
        fl = {x.lower() for x in flags}
        labs = [label]
        if "\\seen" not in fl:
            labs.append("UNREAD")
        if "\\flagged" in fl:
            labs.append("STARRED")
        return " " + " ".join(labs) + " "

    def _fetch_heads(self, sy, c, k, f, uids):
        st = sy.st
        done = []
        uids = sorted(uids, reverse=True)                     # neueste zuerst sichtbar
        for i in range(0, len(uids), 100):
            part = uids[i:i + 100]
            rows = parse_fetch(c.uid("fetch", uid_set(part),
                                     f"(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({HEAD_FIELDS})])"))
            metas = [self._meta(k, f, r) for r in rows]
            st.ingest(metas)
            done += [r["uid"] for r in rows]
            sy.on_event("progress", (min(i + 100, len(uids)), len(uids)))
        return done

    def _meta(self, k, f, r):
        """IMAP-Kopf → dasselbe Format wie Gmail-Metadaten (für Store.ingest)."""
        msg = email.message_from_bytes(r["head"], policy=email.policy.compat32)
        heads = [{"name": n, "value": str(v)} for n, v in msg.items()]
        labs = self._labels(f["label"], r["flags"]).split()
        refs = (msg.get("References") or msg.get("In-Reply-To") or "").split()
        thread = (refs[0] if refs else msg.get("Message-ID") or f"{k}.{r['uid']}").strip()
        ctype = (msg.get_content_type() or "").lower()
        return {"id": f"{k}.{r['uid']}", "threadId": thread, "labelIds": labs, "snippet": "",
                "internalDate": r["date"], "sizeEstimate": r["size"],
                "payload": {"headers": heads, "mimeType": ctype}}

    def more(self, sy, folder, want=MORE):
        st = sy.st
        folders = self.known_folders(st)
        if folder in ("ALLMAIL", "STARRED"):
            targets = [(k, f) for k, f in folders.items() if f["label"] not in ("TRASH", "SPAM")]
            per = 60
        else:
            targets = [(k, f) for k, f in folders.items() if f["label"] == folder]
            per = want
        got, more_left = 0, False
        with self.using() as c:
            for k, f in targets:
                have = {split_id(r[0])[1] for r in st.db.execute("SELECT id FROM messages WHERE id LIKE ?",
                                                                    (k + ".%",))}
                have.discard(None)
                c.select(f["name"], readonly=True)
                all_uids = [int(x) for x in (c.uid("search", None, "ALL")[0] or b"").split()]
                low = min(have) if have else (all_uids[-1] + 1 if all_uids else 0)
                older = [u for u in all_uids if u < low and u not in have]
                take = older[-per:]
                if take:
                    got += len(self._fetch_heads(sy, c, k, f, take))
                more_left = more_left or len(older) > len(take)
        sy.on_event("more", (folder, got, more_left))

    def search(self, sy, query, token):
        st = sy.st
        ids = []
        with self.using() as c:
            for k, f in self.known_folders(st).items():
                if f["label"] in ("TRASH", "SPAM"):
                    continue
                try:
                    c.select(f["name"], readonly=True)
                    try:
                        query.encode("ascii")
                        found = c.uid("search", None, "TEXT", '"' + query.replace('"', "") + '"')
                    except UnicodeEncodeError:
                        c.c.literal = query.encode()
                        found = c.uid("search", "CHARSET", "UTF-8", "TEXT")
                except gmail.ApiError:
                    continue
                uids = [int(x) for x in (found[0] or b"").split()][-100:]
                if not uids:
                    continue
                have = st.known(f"{k}.{u}" for u in uids)
                need = [u for u in uids if f"{k}.{u}" not in have]
                if need:
                    self._fetch_heads(sy, c, k, f, need)
                ids += [f"{k}.{u}" for u in uids]
        rows = st.listing(None, ids=ids)
        sy.on_event("search", (token, [r["id"] for r in rows][:300]))

    # ---- Text
    def raw(self, mid):
        k, uid = split_id(mid)
        f = self.known_folders(self.st).get(k)
        if not f or uid is None:
            raise gmail.ApiError(404, "mail nicht mehr da")
        with self.using("fetch") as c:
            if c.selected != f["name"]:
                c.select(f["name"], readonly=True)
            rows = c.uid("fetch", str(uid), "(BODY.PEEK[])")
        for d in rows:
            if isinstance(d, tuple):
                return d[1]
        raise gmail.ApiError(404, "mail nicht mehr da (anderswo verschoben/gelöscht?)")

    def attachment(self, mid, aid):
        raise gmail.ApiError(404, "anhang steckt im mail-text")

    # ---- Änderungen
    def apply(self, sy, op):
        st = sy.st
        folders = self.known_folders(st)
        k = op["op"]
        if k == "send":
            return self.send(sy, base64.b64decode(op["raw"]))
        by = {}
        for mid in op.get("ids", []):
            fk, uid = split_id(mid)
            if fk in folders and uid is not None:
                by.setdefault(fk, []).append(uid)
        with self.using() as c:
            if k == "modify":
                add, rem = op.get("add", []), op.get("remove", [])
                flags = []
                if "UNREAD" in add:
                    flags.append(("-FLAGS.SILENT", "\\Seen"))
                if "UNREAD" in rem:
                    flags.append(("+FLAGS.SILENT", "\\Seen"))
                if "STARRED" in add:
                    flags.append(("+FLAGS.SILENT", "\\Flagged"))
                if "STARRED" in rem:
                    flags.append(("-FLAGS.SILENT", "\\Flagged"))
                for fk, uids in by.items():
                    c.select(folders[fk]["name"])
                    for how, fl in flags:
                        c.uid("store", uid_set(uids), how, f"({fl})")
                # Ordnerwechsel: neues Ordner-Label hinzu → dorthin; eigenes Ordner-Label weg → Archiv
                tgt = next((l for l in add if l not in ("UNREAD", "STARRED")), None)
                for fk, uids in by.items():
                    here = folders[fk]["label"]
                    dest = None
                    if tgt and tgt != here:
                        dest = self.folder_of_label(st, tgt)[0]
                        if dest is None:
                            raise gmail.ApiError(400, f"ordner „{tgt}“ gibt es hier nicht")
                    elif not tgt and here in rem:
                        dest = self.special(st, "ARCHIVE")[0]
                        if dest is None:
                            raise gmail.ApiError(400, "dieses postfach hat keinen archiv-ordner")
                    if dest and dest != fk:
                        self._move(st, c, folders, fk, uids, dest)
            elif k == "trash":
                dest = self.special(st, "TRASH")[0]
                for fk, uids in by.items():
                    if dest is None:
                        raise gmail.ApiError(400, "dieses postfach hat keinen papierkorb-ordner")
                    if fk != dest:
                        self._move(st, c, folders, fk, uids, dest)
            elif k == "untrash":
                respam = set(op.get("respam") or [])
                inbox = self.special(st, "INBOX")[0]
                spam = self.special(st, "SPAM")[0]
                for fk, uids in by.items():
                    back_spam = [u for u in uids if f"{fk}.{u}" in respam and spam]
                    back_in = [u for u in uids if u not in back_spam]
                    if back_in and inbox and fk != inbox:
                        self._move(st, c, folders, fk, back_in, inbox)
                    if back_spam and fk != spam:
                        self._move(st, c, folders, fk, back_spam, spam)
            elif k == "delete":
                for fk, uids in by.items():
                    c.select(folders[fk]["name"])
                    c.uid("store", uid_set(uids), "+FLAGS.SILENT", "(\\Deleted)")
                    if c.has("UIDPLUS"):
                        c.uid("expunge", uid_set(uids))
                    else:
                        c.cmd("expunge")
                    st.drop([f"{fk}.{u}" for u in uids])
            c.selected = None

    def _move(self, st, c, folders, fk, uids, dest):
        src, dst = folders[fk], folders[dest]
        c.select(src["name"])
        c.c.untagged_responses.pop("COPYUID", None)
        if c.has("MOVE"):
            c.uid("move", uid_set(uids), _q(dst["name"]))
        else:
            c.uid("copy", uid_set(uids), _q(dst["name"]))
            c.uid("store", uid_set(uids), "+FLAGS.SILENT", "(\\Deleted)")
            if c.has("UIDPLUS"):
                c.uid("expunge", uid_set(uids))
            else:
                c.cmd("expunge")
        cu = c.c.untagged_responses.pop("COPYUID", None)
        mapped = {}
        if cu:
            try:
                parts = (cu[-1].decode() if isinstance(cu[-1], bytes) else str(cu[-1])).split()
                a, b = expand_set(parts[1]), expand_set(parts[2])
                mapped = dict(zip(a, b))
            except (IndexError, ValueError):
                mapped = {}
        for u in uids:
            old = f"{fk}.{u}"
            if u in mapped:
                new = f"{dest}.{mapped[u]}"
                st.alias(old, new)
                r = st.message(new)
                if r:
                    labs = [x for x in r["labels"].split() if x in ("UNREAD", "STARRED")]
                    labs = " " + " ".join([dst["label"]] + labs) + " "
                    with st.lock:
                        st.db.execute("UPDATE messages SET labels=? WHERE id=?", (labs, new))
                        st.db.commit()
            else:
                st.drop([old])                # holt der nächste Abgleich im Zielordner neu
        # Zielordner beim nächsten Abgleich ganz prüfen
        s = self._state(st, dest)
        s.pop("sig", None)
        st.put("imap:f:" + dest, json.dumps(s))

    def resync(self, sy, ids):
        for k in {split_id(i)[0] for i in ids}:
            s = self._state(sy.st, k)
            s.pop("sig", None)
            sy.st.put("imap:f:" + k, json.dumps(s))

    def after_flush(self, sy):
        pass

    # ---- Senden
    def send(self, sy, raw):
        msg = email.message_from_bytes(raw, policy=email.policy.SMTP)
        rcpts = [a for _n, a in email.utils.getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []) +
                                                         msg.get_all("Bcc", [])) if a]
        del msg["Bcc"]                                   # Blindkopie nie im Text mitschicken
        data = msg.as_bytes()
        sender = email.utils.parseaddr(msg.get("From", ""))[1] or self.conf.get("address")
        pw = self.password()
        if pw is None:
            raise gmail.AuthError("passwort fehlt")
        try:
            s = _smtp(self.conf, pw)
            try:
                s.sendmail(sender, rcpts, data)
            finally:
                try:
                    s.quit()
                except Exception:
                    pass
        except smtplib.SMTPAuthenticationError as e:
            raise gmail.AuthError(f"smtp-anmeldung abgelehnt: {e.smtp_error.decode(errors='replace')[:160]}") from None
        except smtplib.SMTPRecipientsRefused as e:
            raise gmail.ApiError(400, "empfänger abgelehnt: " + ", ".join(e.recipients)) from None
        except smtplib.SMTPResponseException as e:
            code = e.smtp_code
            text = e.smtp_error.decode(errors="replace")[:160]
            raise gmail.ApiError(400 if 500 <= code < 600 else 503, f"smtp {code}: {text}") from None
        except smtplib.SMTPException as e:
            raise OSError(f"smtp: {e}") from None
        if self.conf.get("save_sent", True):
            k, f = self.special(sy.st, "SENT")
            if f:
                try:
                    with self.using() as c:
                        c.cmd("append", _q(f["name"]), "(\\Seen)", imaplib.Time2Internaldate(time.time()), data)
                except (gmail.ApiError, OSError) as e:
                    print("mailist: kopie in gesendet:", e, flush=True)


def _smtp(conf, password):
    host, port, tls = conf["smtp_host"], int(conf.get("smtp_port") or 587), conf.get("smtp_tls", "starttls")
    ctx = _context()
    try:
        if tls == "ssl":
            s = smtplib.SMTP_SSL(host, port, context=ctx, timeout=40)
        else:
            s = smtplib.SMTP(host, port, timeout=40)
            s.ehlo()
            if tls == "starttls":
                s.starttls(context=ctx)
                s.ehlo()
            elif security["require_tls"]:
                s.close()
                raise gmail.AuthError("unverschlüsselter versand abgelehnt (einstellungen → sicherheit)")
    except ssl.SSLCertVerificationError as e:
        raise gmail.AuthError(f"zertifikat des smtp-servers ungültig: {e.verify_message}") from None
    s.login(conf.get("smtp_user") or conf.get("user") or conf["address"], password)
    return s


def _display(f):
    names = {"INBOX": "posteingang", "SENT": "gesendet", "DRAFT": "entwürfe", "TRASH": "papierkorb", "SPAM": "spam"}
    if f["label"] in names:
        return names[f["label"]]
    n = f["name"]
    if n.upper().startswith("INBOX/") or n.upper().startswith("INBOX."):
        n = n[6:]
    return n

# Gmail: Anmeldung (OAuth 2.0 für Desktop-Apps, PKCE, Rückleitung an 127.0.0.1) und die Gmail-REST-API –
# ohne Fremdbibliotheken (urllib). Gleiches Verfahren wie datist; der OAuth-Client von datist wird mitbenutzt,
# falls mailist keinen eigenen hat. Das Token liegt getrennt in ~/.config/mailist/auth/token.json (nur für dich).
import base64, contextlib, hashlib, http.server, json, os, secrets, socket, threading, time, uuid
import urllib.error, urllib.parse, urllib.request

_XDG = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
CONFIG = os.path.join(_XDG, "mailist")
CLIENT = os.path.join(CONFIG, "client_secret.json")
CLIENT_SHARED = os.path.join(_XDG, "kalender", "client_secret.json")     # von datist
AUTH_DIR = os.path.join(CONFIG, "auth")
TOKEN = os.path.join(AUTH_DIR, "token.json")
LOGIN_HINT_FILES = (os.path.join(CONFIG, "login_hint"), os.path.join(_XDG, "kalender", "login_hint"))
# volle Berechtigung: nur damit geht „endgültig löschen“ (sonst nur Papierkorb)
SCOPE = "https://mail.google.com/"

API = os.environ.get("MAILIST_API_BASE", "https://gmail.googleapis.com/gmail/v1/users/me")
BATCH_URL = os.environ.get("MAILIST_BATCH_URL", "https://gmail.googleapis.com/batch/gmail/v1")
BATCH_PATH = "/gmail/v1/users/me"
AUTH_URL = os.environ.get("MAILIST_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth")
TOKEN_URL = os.environ.get("MAILIST_TOKEN_URL", "https://oauth2.googleapis.com/token")
LIST_HEADERS = ("From", "To", "Cc", "Subject", "Date", "Reply-To", "Return-Path", "Authentication-Results",
                "List-Unsubscribe", "Message-ID")


class ApiError(Exception):
    def __init__(self, status, msg, reason=""):
        super().__init__(f"{status}: {msg}")
        self.status = status
        self.reason = reason            # z. B. SERVICE_DISABLED / accessNotConfigured


class AuthError(Exception):
    pass


class ExpiredError(AuthError):
    """Google nimmt das Refresh-Token nicht mehr an (Testmodus: nach 7 Tagen, oder Zugriff widerrufen)."""


# ---------------------------------------------------------------- Client & Token
def _read_client(path):
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    c = d.get("installed") or d.get("web") or d
    return c if c.get("client_id") else None


def client():
    return _read_client(CLIENT) or _read_client(CLIENT_SHARED)


def client_source():
    """'eigen', 'datist' oder None – für die Einrichtungsseite."""
    if _read_client(CLIENT):
        return "eigen"
    if _read_client(CLIENT_SHARED):
        return "datist"
    return None


def project_id():
    c = client() or {}
    return c.get("project_id") or ""


def _write_private(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def install_client(path):
    """Heruntergeladene client_secret_….json übernehmen (prüft das Format)."""
    with open(path) as f:
        d = json.load(f)
    c = d.get("installed")
    if not c or not c.get("client_id"):
        if "web" in d:
            raise ValueError("Das ist ein Web-Client – bitte als Typ „Desktop-App“ anlegen")
        raise ValueError("Keine OAuth-Client-Datei (\"installed\".client_id fehlt)")
    _write_private(CLIENT, d)


def install_client_manual(client_id, secret):
    client_id, secret = client_id.strip(), secret.strip()
    if not client_id.endswith(".apps.googleusercontent.com"):
        raise ValueError("Client-ID sieht nicht richtig aus (endet auf .apps.googleusercontent.com)")
    if len(secret) < 10:
        raise ValueError("Clientschlüssel fehlt")
    _write_private(CLIENT, {"installed": {
        "client_id": client_id, "client_secret": secret, "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": ["http://localhost"]}})


_ctx = threading.local()       # mehrere Gmail-Konten: jeder Thread arbeitet mit „seinem“ Token


def token_path():
    return getattr(_ctx, "token", None) or TOKEN


@contextlib.contextmanager
def bound(path):
    """Alles in diesem Block (in diesem Thread) nutzt das Token unter path."""
    old = getattr(_ctx, "token", None)
    _ctx.token = path
    try:
        yield
    finally:
        _ctx.token = old


def token():
    try:
        with open(token_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_token(t):
    d = os.path.dirname(token_path())
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o700)
    _write_private(token_path(), t)


def logged_in():
    t = token()
    return bool(t and t.get("refresh_token"))


def expired():
    t = token()
    return bool(t and t.get("expired"))


def signed_in_at():
    t = token()
    return t.get("created") if t else None


def logout():
    t = token()
    if t and t.get("refresh_token"):
        try:
            urllib.request.urlopen(urllib.request.Request(
                "https://oauth2.googleapis.com/revoke",
                data=urllib.parse.urlencode({"token": t["refresh_token"]}).encode()), timeout=8)
        except (urllib.error.URLError, OSError):
            pass
    try:
        os.remove(token_path())
    except OSError:
        pass


def _post_form(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except ValueError:
            body = {}
        if body.get("error") == "invalid_grant":
            raise ExpiredError("Anmeldung abgelaufen") from None
        raise AuthError(body.get("error_description") or body.get("error") or f"HTTP {e.code}") from None


def access_token():
    t = token()
    if not t or not t.get("refresh_token"):
        raise AuthError("nicht angemeldet")
    if t.get("access_token") and t.get("expires_at", 0) > time.time() + 60:
        return t["access_token"]
    c = client()
    if not c:
        raise AuthError("OAuth-Client fehlt (client_secret.json)")
    try:
        res = _post_form(TOKEN_URL, {"client_id": c["client_id"], "client_secret": c.get("client_secret", ""),
                                     "refresh_token": t["refresh_token"], "grant_type": "refresh_token"})
    except ExpiredError:
        t["expired"] = True
        t.pop("access_token", None)
        save_token(t)
        raise
    t["access_token"] = res["access_token"]
    t["expires_at"] = time.time() + int(res.get("expires_in", 3600))
    t.pop("expired", None)
    save_token(t)
    return t["access_token"]


def login_hint():
    for p in LOGIN_HINT_FILES:
        try:
            with open(p, encoding="utf-8") as f:
                h = f.readline().strip()
            if h:
                return h
        except OSError:
            pass
    return None


class Login:
    """Kleiner Server auf 127.0.0.1, Google-Anmeldung im Standardbrowser, Code → Token. run() blockiert."""

    def __init__(self, open_url, choose=False):
        self.open_url = open_url
        self.choose = choose          # weiteres Konto: Google soll nach dem Konto fragen
        self.cancelled = False
        self.path = token_path()      # Login läuft in eigenem Thread → Ziel jetzt merken

    def run(self, timeout=300):
        c = client()
        if not c:
            raise AuthError("Erst den OAuth-Client einrichten (Schritt 1)")
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        result = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if "state" not in q and "error" not in q:
                    self.send_response(404)
                    self.end_headers()
                    return
                result.update({k: v[0] for k, v in q.items()})
                ok = q.get("state", [""])[0] == state and "code" in q
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                msg = "Angemeldet – du kannst den Tab schließen." if ok else "Anmeldung abgebrochen."
                self.wfile.write(
                    f"<html><body style='background:#111;color:#ddd;font:16px monospace;padding:3em'>"
                    f"<pre>┌──────────────────────────────────────────┐\n│ mailist · {msg:<30}│\n"
                    f"└──────────────────────────────────────────┘</pre></body></html>".encode())

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        srv.timeout = 1
        redirect = f"http://127.0.0.1:{srv.server_address[1]}"
        params = {"client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
                  "access_type": "offline", "prompt": "consent"}
        hint = None if self.choose else login_hint()
        if hint:
            params["login_hint"] = hint
        if self.choose:
            params["prompt"] = "select_account consent"
        self.open_url(AUTH_URL + "?" + urllib.parse.urlencode(params))
        end = time.time() + timeout
        try:
            while not result and time.time() < end and not self.cancelled:
                srv.handle_request()
        finally:
            srv.server_close()
        if self.cancelled:
            raise AuthError("abgebrochen")
        if not result:
            raise AuthError("Zeitüberschreitung – keine Antwort vom Browser")
        if result.get("error") == "access_denied":
            raise AuthError("Google hat den Zugriff verweigert – abgebrochen, oder dein Konto ist nicht als "
                            "Testnutzer eingetragen (Google Auth Platform → Zielgruppe → Testnutzer)")
        if result.get("error"):
            raise AuthError(f"Google meldet: {result['error']}")
        if result.get("state") != state:
            raise AuthError("Antwort passt nicht zur Anfrage (state)")
        res = _post_form(TOKEN_URL, {"client_id": c["client_id"], "client_secret": c.get("client_secret", ""),
                                     "code": result["code"], "code_verifier": verifier,
                                     "redirect_uri": redirect, "grant_type": "authorization_code"})
        if not res.get("refresh_token"):
            raise AuthError("Google hat kein Refresh-Token geschickt – Zugriff unter "
                            "myaccount.google.com/permissions entfernen und erneut anmelden")
        if SCOPE not in (res.get("scope") or SCOPE):
            raise AuthError("Gmail-Zugriff wurde bei der Anmeldung nicht erlaubt (Haken bei Gmail setzen)")
        with bound(self.path):
            save_token({"refresh_token": res["refresh_token"], "access_token": res["access_token"],
                        "created": time.time(), "expires_at": time.time() + int(res.get("expires_in", 3600)),
                        "scope": res.get("scope")})
        return True


# ---------------------------------------------------------------- REST
def _error_of(e):
    try:
        err = json.load(e).get("error", {})
    except (ValueError, AttributeError):
        return "", ""
    reason = ""
    for d in err.get("details", []) or []:
        reason = d.get("reason") or reason
    for d in err.get("errors", []) or []:
        reason = reason or d.get("reason", "")
    return err.get("message", ""), reason


RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "RATE_LIMIT_EXCEEDED"}


def is_rate(status, msg="", reason=""):
    """Gmail drosselt (zu viele Anfragen pro Minute) – kommt als 429 oder als 403 „Quota exceeded“.
    Das ist vorübergehend: später erneut versuchen, nie als „abgelehnt“ behandeln."""
    if status == 429:
        return True
    m = (msg or "").lower()
    return status == 403 and (reason in RATE_REASONS or "quota" in m or "rate limit" in m)


def _rate_of_part(st, js):
    err = (js or {}).get("error") or {}
    reason = next((d.get("reason", "") for d in err.get("errors", []) or []), "")
    return is_rate(st, err.get("message", ""), reason)


def call(method, path, params=None, body=None, timeout=30):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(4):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {access_token()}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            msg, reason = _error_of(e)
            if e.code == 401 and attempt == 0:
                t = token() or {}
                t.pop("access_token", None)
                save_token(t)
                continue
            rate = is_rate(e.code, msg, reason)
            if (rate or e.code in (500, 502, 503)) and attempt < 3:
                time.sleep((4 if rate else 1.5) * (attempt + 1))
                continue
            if rate:
                raise ApiError(429, "gmail bremst gerade (zu viele anfragen) – gleich nochmal", "rate") from None
            raise ApiError(e.code, msg or e.reason, reason) from None
    raise ApiError(0, "keine Antwort")


def batch(requests, chunk=40):
    """Mehrere Anfragen in einer HTTP-Sammelanfrage. requests: [(method, path, body|None)] relativ zu users/me.
    Rückgabe: Liste [(status, json)] in derselben Reihenfolge. Zu viele auf einmal (429) → kleiner + erneut."""
    out = [None] * len(requests)
    todo = list(range(len(requests)))
    delay = 0.0
    for _round in range(6):
        if not todo:
            break
        retry = []
        for i in range(0, len(todo), chunk):
            part = todo[i:i + chunk]
            res = _batch_once([requests[j] for j in part])
            for j, (st, js) in zip(part, res):
                if st in (429, 500, 503) or st is None or (st == 403 and _rate_of_part(st, js)):
                    retry.append(j)
                else:
                    out[j] = (st, js)
            if delay:
                time.sleep(delay)
        todo = retry
        if todo:
            chunk = max(5, chunk // 2)
            delay = min(2.0, delay + 0.5)
            time.sleep(1.0 + delay)
    for j in todo:
        out[j] = (429, {})
    return out


def _batch_once(reqs):
    boundary = "mailist_" + uuid.uuid4().hex
    parts = []
    for n, (method, path, body) in enumerate(reqs):
        head = (f"--{boundary}\r\nContent-Type: application/http\r\nContent-ID: <r{n}>\r\n\r\n"
                f"{method} {BATCH_PATH}{path} HTTP/1.1\r\n")
        if body is not None:
            b = json.dumps(body)
            head += f"Content-Type: application/json\r\nContent-Length: {len(b.encode())}\r\n\r\n{b}\r\n"
        else:
            head += "\r\n"
        parts.append(head)
    data = ("".join(parts) + f"--{boundary}--\r\n").encode()
    for attempt in range(3):
        req = urllib.request.Request(BATCH_URL, data=data, method="POST", headers={
            "Authorization": f"Bearer {access_token()}", "Content-Type": f"multipart/mixed; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                ctype = r.headers.get("Content-Type", "")
                raw = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                t = token() or {}
                t.pop("access_token", None)
                save_token(t)
                continue
            msg, reason = _error_of(e)
            rate = is_rate(e.code, msg, reason)
            if (rate or e.code in (500, 502, 503)) and attempt < 2:
                time.sleep((4 if rate else 2) * (attempt + 1))
                continue
            if rate:
                raise ApiError(429, "gmail bremst gerade (zu viele anfragen) – gleich nochmal", "rate") from None
            raise ApiError(e.code, msg or e.reason, reason) from None
    else:
        return [(None, {})] * len(reqs)
    return _parse_batch(ctype, raw, len(reqs))


def _parse_batch(ctype, raw, n):
    out = [(None, {})] * n
    bnd = None
    for piece in ctype.split(";"):
        piece = piece.strip()
        if piece.lower().startswith("boundary="):
            bnd = piece.split("=", 1)[1].strip('"')
    if not bnd:
        return out
    text = raw.decode("utf-8", "replace")
    for chunk in text.split("--" + bnd):
        chunk = chunk.strip("\r\n")
        if not chunk or chunk == "--":
            continue
        # Kopf des Teils (Content-ID) | HTTP-Statuszeile + Kopf | Körper
        sec = chunk.replace("\r\n", "\n").split("\n\n", 2)
        if len(sec) < 2:
            continue
        cid = None
        for line in sec[0].split("\n"):
            if line.lower().startswith("content-id:"):
                v = line.split(":", 1)[1].strip().strip("<>")
                if "r" in v:
                    try:
                        cid = int(v.rsplit("r", 1)[1])
                    except ValueError:
                        cid = None
        status_line = sec[1].split("\n", 1)[0]
        try:
            st = int(status_line.split()[1])
        except (IndexError, ValueError):
            st = None
        body = sec[2] if len(sec) > 2 else ""
        try:
            js = json.loads(body) if body.strip() else {}
        except ValueError:
            js = {}
        if cid is not None and 0 <= cid < n:
            out[cid] = (st, js)
    return out


def online(host="gmail.googleapis.com"):
    if API.startswith("http://127.0.0.1") or API.startswith("http://localhost"):
        return True
    try:
        socket.create_connection((host, 443), timeout=3).close()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- Bausteine
def profile():
    return call("GET", "/profile")


def labels():
    return call("GET", "/labels").get("labels", [])


def label_counts(ids):
    res = batch([("GET", f"/labels/{urllib.parse.quote(i)}", None) for i in ids])
    return {i: js for i, (st, js) in zip(ids, res) if st == 200}


def list_ids(label=None, q=None, limit=500, page=None, spam_trash=False):
    """→ (ids, nächste Seite). Neueste zuerst."""
    ids = []
    while len(ids) < limit:
        p = {"maxResults": min(500, limit - len(ids))}
        if label:
            p["labelIds"] = label
        if q:
            p["q"] = q
        if page:
            p["pageToken"] = page
        if spam_trash:
            p["includeSpamTrash"] = "true"
        r = call("GET", "/messages", p)
        ids += [m["id"] for m in r.get("messages", [])]
        page = r.get("nextPageToken")
        if not page:
            break
    return ids, page


def metadata(ids):
    """Kopfzeilen + Label vieler Mails per Sammelanfrage → {id: meta}. Fehlende (404) fehlen im Ergebnis."""
    q = "format=metadata&" + "&".join("metadataHeaders=" + h for h in LIST_HEADERS)
    res = batch([("GET", f"/messages/{i}?{q}", None) for i in ids])
    # Gedrosselt/abgebrochen (nicht „gibt's nicht“) → Fehler, damit niemand Lücken für erledigt hält
    if any(st in (429, 500, 503, None) or (st == 403 and _rate_of_part(st, js)) for st, js in res):
        raise ApiError(429, "gmail bremst gerade (zu viele anfragen) – gleich nochmal", "rate")
    return {i: js for i, (st, js) in zip(ids, res) if st == 200 and js.get("id")}


def raw(mid):
    r = call("GET", f"/messages/{mid}", {"format": "raw"}, timeout=60)
    s = r.get("raw", "")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)), r


def history(start):
    """Änderungen seit start → (einträge, neue history-id). ApiError 404 = zu alt → voller Abgleich."""
    out, page, hid = [], None, start
    while True:
        p = {"startHistoryId": start, "maxResults": 500}
        if page:
            p["pageToken"] = page
        r = call("GET", "/history", p)
        out += r.get("history", [])
        hid = r.get("historyId", hid)
        page = r.get("nextPageToken")
        if not page:
            return out, hid


FIXED = {"SENT", "DRAFT", "CHAT"}        # setzt Gmail selbst – in Änderungen „Invalid label“


class PartialError(ApiError):
    """Ein Teil der Mails wurde abgelehnt. failed = {id: Grund}"""

    def __init__(self, failed, msg):
        super().__init__(400, msg, "partial")
        self.failed = failed


def modify(ids, add=(), remove=()):
    add = [l for l in add if l not in FIXED]
    remove = [l for l in remove if l not in FIXED and l not in add]
    if not ids or not (add or remove):
        return
    failed = {}
    for i in range(0, len(ids), 900):
        part = ids[i:i + 900]
        try:
            call("POST", "/messages/batchModify", body={"ids": part, "addLabelIds": add, "removeLabelIds": remove})
        except ApiError as e:
            if e.status not in (400, 404):
                raise
            # eine einzige unpassende Mail lässt die ganze Sammeländerung scheitern → einzeln versuchen
            body = {"addLabelIds": add, "removeLabelIds": remove}
            res = batch([("POST", f"/messages/{m}/modify", body) for m in part])
            for m, (st, js) in zip(part, res):
                if st in (429, 500, 503, None) or (st == 403 and _rate_of_part(st, js)):
                    raise ApiError(429, "gmail bremst gerade (zu viele anfragen) – gleich nochmal", "rate")
                if st not in (200, 204):
                    failed[m] = ((js or {}).get("error") or {}).get("message") or f"status {st}"
    if failed:
        why = sorted(set(failed.values()))
        raise PartialError(failed, f"{len(failed)} von {len(ids)} abgelehnt: {'; '.join(why[:3])}")


def trash(ids, undo=False):
    verb = "untrash" if undo else "trash"
    res = batch([("POST", f"/messages/{i}/{verb}", None) for i in ids])
    if any(st in (429, 500, 503, None) or (st == 403 and _rate_of_part(st, js)) for st, js in res):
        # Teil schon erledigt? Egal – trash/untrash ist wiederholbar
        raise ApiError(429, "gmail bremst gerade (zu viele anfragen) – gleich nochmal", "rate")
    failed = {i: ((js or {}).get("error") or {}).get("message") or f"status {st}"
              for i, (st, js) in zip(ids, res) if st not in (200, 404)}
    if failed:
        raise PartialError(failed, f"{len(failed)} von {len(ids)} abgelehnt: "
                                   f"{'; '.join(sorted(set(failed.values()))[:3])}")
    return []


def delete(ids):
    for i in range(0, len(ids), 900):
        call("POST", "/messages/batchDelete", body={"ids": ids[i:i + 900]})


def send(raw_bytes, thread=None):
    body = {"raw": base64.urlsafe_b64encode(raw_bytes).decode()}
    if thread:
        body["threadId"] = thread
    return call("POST", "/messages/send", body=body, timeout=120)


def attachment(mid, aid):
    r = call("GET", f"/messages/{mid}/attachments/{aid}", timeout=120)
    s = r.get("data", "")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

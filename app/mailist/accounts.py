# Mehrere Postfächer: Gmail (API) und IMAP/SMTP. Jedes Konto hat eigenen Zwischenspeicher, eigenen Abgleich-
# Thread und eigenes Token/Passwort. Liste in ~/.config/mailist/accounts.json; das erste Gmail-Konto heißt „main“
# und nutzt die alten Pfade (auth/token.json, mail.db) – bestehende Installationen laufen einfach weiter.
import json, os, secrets

import gmail
import store as stmod
import vault

FILE = os.path.join(gmail.CONFIG, "accounts.json")


def mask(addr):
    """Eigene Adresse nicht ausgeschrieben zeigen (Bildschirmfotos!): „max@example.com“ → „m…x@example.com“"""
    if not addr or "@" not in addr:
        return addr or ""
    user, dom = addr.split("@", 1)
    return (user[0] + "…" + user[-1] if len(user) > 2 else user[0] + "…") + "@" + dom


def load():
    try:
        with open(FILE) as f:
            d = json.load(f)
        if d.get("list"):
            return d
    except (OSError, ValueError):
        pass
    return {"current": "main", "list": [{"id": "main", "kind": "gmail", "name": "gmail"}]}


def save(d):
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    tmp = FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, FILE)


def new_id():
    return "a" + secrets.token_hex(3)


def db_path(aid):
    return stmod.DB if aid == "main" else os.path.join(stmod.CACHE, f"{aid}.db")


def token_path(aid):
    return gmail.TOKEN if aid == "main" else os.path.join(gmail.AUTH_DIR, f"{aid}.json")


class Account:
    def __init__(self, conf, on_event, limit=2000):
        self.conf = conf
        self.id = conf["id"]
        self.kind = conf.get("kind", "gmail")
        self.store = stmod.Store(db_path(self.id))
        if self.kind == "imap":
            import imapmail
            self.backend = imapmail.ImapBackend(self.id, conf)
            self.backend.st = self.store
        else:
            self.backend = stmod.GmailBackend(token_path(self.id))
        self.store.backend = self.backend          # der Leser holt Texte über den Speicher des Kontos
        self.state = "idle"
        self.syncer = stmod.Syncer(self.store, lambda k, d: on_event(self.id, k, d), limit=limit,
                                   backend=self.backend, name=f"mailist-sync-{self.id}")

    @property
    def label(self):
        return self.conf.get("name") or mask(self.store.me or self.conf.get("address")) or self.kind

    @property
    def address(self):
        return self.store.me or (self.conf.get("address") or "").lower()

    def inbox_unread(self):
        u = self.store.label_unread("INBOX")
        return u if u is not None else self.store.count_local("INBOX", unread=True)

    def remove_data(self):
        """Abmelden + alles Lokale dieses Kontos löschen."""
        try:
            self.backend.logout()
        except Exception:
            pass
        if self.kind == "imap":
            vault.forget(self.id)
        self.store.reset()
        if self.id != "main":
            for suf in ("", "-wal", "-shm"):
                try:
                    os.remove(db_path(self.id) + suf)
                except OSError:
                    pass

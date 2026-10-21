# Passwörter für IMAP/SMTP-Konten. Standard: System-Schlüsselbund (libsecret – GNOME Keyring, KeePassXC …),
# Einstellung „passwörter im schlüsselbund speichern“ aus → nur im Arbeitsspeicher, nach jedem Neustart neu
# eingeben. Nie im Klartext in einer Datei.
import threading

try:
    import gi
    gi.require_version("Secret", "1")
    from gi.repository import Secret
    SCHEMA = Secret.Schema.new("de.invincible.Mailist", Secret.SchemaFlags.NONE,
                               {"account": Secret.SchemaAttributeType.STRING})
except (ImportError, ValueError):
    Secret = SCHEMA = None

_mem = {}
_lock = threading.Lock()
use_keyring = True               # von der App aus den Einstellungen gesetzt


def available():
    return SCHEMA is not None


def get(account):
    with _lock:
        if account in _mem:
            return _mem[account]
    if use_keyring and SCHEMA is not None:
        try:
            pw = Secret.password_lookup_sync(SCHEMA, {"account": account}, None)
        except Exception:        # Schlüsselbund gesperrt/nicht da
            pw = None
        if pw is not None:
            with _lock:
                _mem[account] = pw
            return pw
    return None


def put(account, password, label="mailist"):
    with _lock:
        _mem[account] = password
    if use_keyring and SCHEMA is not None:
        try:
            Secret.password_store_sync(SCHEMA, {"account": account}, Secret.COLLECTION_DEFAULT,
                                       label, password, None)
            return True
        except Exception as e:
            print("mailist: schlüsselbund:", e, flush=True)
    return False


def forget(account, keyring=True):
    with _lock:
        _mem.pop(account, None)
    if keyring and SCHEMA is not None:
        try:
            Secret.password_clear_sync(SCHEMA, {"account": account}, None)
        except Exception:
            pass


def in_keyring(account):
    if SCHEMA is None:
        return False
    try:
        return Secret.password_lookup_sync(SCHEMA, {"account": account}, None) is not None
    except Exception:
        return False

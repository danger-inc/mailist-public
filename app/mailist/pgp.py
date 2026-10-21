# OpenPGP über das installierte gpg (kein eigenes Krypto): PGP/MIME (RFC 3156) signieren/verschlüsseln beim
# Senden, prüfen/entschlüsseln beim Lesen, dazu Inline-PGP und Autocrypt. Passphrasen fragt gpg-agent selbst
# (pinentry-Fenster) – mailist sieht sie nie. Alles hier kann blockieren → nie im GTK-Thread aufrufen.
import base64, email, email.encoders, email.message, email.policy, os, re, secrets, shutil, subprocess, tempfile

GPG = shutil.which("gpg") or shutil.which("gpg2")


def available():
    return GPG is not None


def _run(args, data=None, timeout=120):
    p = subprocess.run([GPG, "--batch", "--yes", "--status-fd", "2", "--no-tty", *args], input=data,
                       capture_output=True, timeout=timeout)
    status = [l[9:].decode(errors="replace") for l in p.stderr.splitlines() if l.startswith(b"[GNUPG:] ")]
    text = b"\n".join(l for l in p.stderr.splitlines() if not l.startswith(b"[GNUPG:] ")).decode(errors="replace")
    return p.returncode, p.stdout, status, text


# ---------------------------------------------------------------- Schlüssel
def _keys(addr, secret=False):
    """Gültige Schlüssel zur Adresse → [(fingerprint, uid, kann_verschlüsseln, kann_signieren)]."""
    if not GPG or not addr:
        return []
    rc, out, _s, _t = _run(["--with-colons", "--list-secret-keys" if secret else "--list-keys", f"<{addr}>"])
    if rc != 0:
        return []
    res, cur = [], None
    for line in out.decode(errors="replace").splitlines():
        f = line.split(":")
        if f[0] in ("pub", "sec"):
            if cur:
                res.append(cur)
            bad = f[1] in ("e", "r", "d", "i", "n")
            caps = f[11] if len(f) > 11 else ""
            cur = None if bad else {"fpr": None, "uid": "", "enc": "E" in caps, "sig": "S" in caps}
        elif f[0] == "fpr" and cur is not None and cur["fpr"] is None:
            cur["fpr"] = f[9]
        elif f[0] == "uid" and cur is not None and not cur["uid"]:
            cur["uid"] = f[9]
    if cur:
        res.append(cur)
    return [k for k in res if k["fpr"]]


def own_key(addr):
    keys = [k for k in _keys(addr, secret=True) if k["sig"]]
    return keys[0] if keys else None


def key_for(addr):
    keys = [k for k in _keys(addr) if k["enc"]]
    return keys[0]["fpr"] if keys else None


def keys_for(addrs):
    """→ ({adresse: fpr}, [fehlende])"""
    found, missing = {}, []
    for a in dict.fromkeys(a.lower() for a in addrs if a):
        f = key_for(a)
        (found.__setitem__(a, f) if f else missing.append(a))
    return found, missing


def generate(name, addr):
    """Neuen Schlüssel erzeugen (Passphrase fragt pinentry). → (fpr|None, fehler|None)"""
    uid = f"{name} <{addr}>" if name else f"<{addr}>"
    rc, _o, status, text = _run(["--quick-generate-key", uid, "future-default", "default", "3y"], timeout=600)
    if rc != 0:
        return None, (text.strip().splitlines() or ["gpg-fehler"])[-1]
    for s in status:
        if s.startswith("KEY_CREATED"):
            return s.split()[-1], None
    k = own_key(addr)
    return (k["fpr"], None) if k else (None, "schlüssel nicht gefunden")


def export(fpr, armor=True, minimal=False):
    args = ["--export"] + (["--armor"] if armor else []) + (["--export-options", "export-minimal"] if minimal else [])
    rc, out, _s, _t = _run(args + [fpr])
    return out if rc == 0 else b""


def import_keys(data):
    """→ (anzahl, text)"""
    rc, _o, status, text = _run(["--import"], data)
    n = sum(1 for s in status if s.startswith("IMPORT_OK"))
    return n, text.strip()


def autocrypt_header(addr, fpr):
    kd = base64.b64encode(export(fpr, armor=False, minimal=True)).decode()
    if not kd:
        return None
    kd = " ".join(kd[i:i + 72] for i in range(0, len(kd), 72))   # Leerzeichen = Faltstellen, werden ignoriert
    return f"addr={addr}; prefer-encrypt=mutual; keydata={kd}"


def import_autocrypt(value, from_addr):
    """Autocrypt-Kopfzeile einer Mail übernehmen (nur wenn addr zum Absender passt)."""
    attrs = {}
    for part in re.split(r";\s*", value or ""):
        k, _, v = part.partition("=")
        attrs[k.strip().lower()] = v.strip()
    if attrs.get("addr", "").lower() != (from_addr or "").lower() or "keydata" not in attrs:
        return 0
    try:
        data = base64.b64decode(re.sub(r"\s+", "", attrs["keydata"]))
    except ValueError:
        return 0
    return import_keys(data)[0]


# ---------------------------------------------------------------- Senden
def _content_entity(msg):
    """Den Inhalt einer Mail (Content-*-Kopf + Körper) als eigene MIME-Einheit in kanonischer Form (CRLF)."""
    inner = email.message.EmailMessage(policy=email.policy.SMTP)
    for k in [k for k in msg.keys()]:
        if k.lower().startswith("content-"):
            for v in msg.get_all(k):
                inner[k] = v
    if msg.is_multipart():
        inner.set_payload(msg.get_payload())
        inner.set_boundary(msg.get_boundary())
        inner.preamble, inner.epilogue = None, ""
    else:
        inner.set_payload(msg.get_payload())
    data = inner.as_bytes(policy=email.policy.SMTP)
    data = re.sub(rb"(?<!\r)\n", b"\r\n", data)
    return data.rstrip(b"\r\n") + b"\r\n"


def _outer_headers(msg):
    out = []
    for k, v in msg.items():
        if k.lower().startswith("content-") or k.lower() == "mime-version":
            continue
        out.append(f"{k}: {v}")
    return "\r\n".join(out)


def wrap(msg, sign_fpr=None, enc_fprs=None, hidden=()):
    """EmailMessage → fertige Bytes (multipart/signed oder multipart/encrypted). Fehler → RuntimeError."""
    # Signaturen brechen, wenn ein Server 8-Bit-Inhalt umkodiert → Inhalt vorher 7-Bit-sicher machen
    for part in msg.walk():
        if not part.is_multipart():
            cte = (part.get("Content-Transfer-Encoding") or "").lower()
            if cte in ("8bit", "binary"):
                payload = part.get_payload(decode=True)
                del part["Content-Transfer-Encoding"]
                part.set_payload(payload)
                email.encoders.encode_quopri(part) if part.get_content_maintype() == "text" \
                    else email.encoders.encode_base64(part)
    entity = _content_entity(msg)
    head = _outer_headers(msg)
    b = "mailist-" + secrets.token_hex(12)
    if enc_fprs:
        args = ["--armor", "--encrypt", "--trust-model", "always"]
        for f in enc_fprs:
            args += ["-r", f]
        for f in hidden:
            args += ["-R", f]
        if sign_fpr:
            args += ["--sign", "--local-user", sign_fpr]
        rc, out, _s, text = _run(args, entity, timeout=600)
        if rc != 0:
            raise RuntimeError("verschlüsseln fehlgeschlagen: " + (text.strip().splitlines() or ["?"])[-1])
        body = (f"MIME-Version: 1.0\r\nContent-Type: multipart/encrypted; protocol=\"application/pgp-encrypted\"; "
                f"boundary=\"{b}\"\r\n\r\nThis is an OpenPGP/MIME encrypted message (RFC 4880 and 3156)\r\n"
                f"--{b}\r\nContent-Type: application/pgp-encrypted\r\nContent-Description: PGP/MIME version "
                f"identification\r\n\r\nVersion: 1\r\n\r\n--{b}\r\nContent-Type: application/octet-stream; "
                f"name=\"encrypted.asc\"\r\nContent-Description: OpenPGP encrypted message\r\nContent-Disposition: "
                f"inline; filename=\"encrypted.asc\"\r\n\r\n").encode() + out.replace(b"\n", b"\r\n").replace(
                    b"\r\r\n", b"\r\n") + f"\r\n--{b}--\r\n".encode()
        return head.encode() + b"\r\n" + body
    if sign_fpr:
        rc, out, status, text = _run(["--armor", "--detach-sign", "--digest-algo", "SHA256", "--local-user",
                                      sign_fpr], entity, timeout=600)
        if rc != 0:
            raise RuntimeError("signieren fehlgeschlagen: " + (text.strip().splitlines() or ["?"])[-1])
        algo = "pgp-sha256"
        for s in status:
            if s.startswith("SIG_CREATED"):
                try:
                    algo = "pgp-" + {"2": "sha1", "8": "sha256", "9": "sha384", "10": "sha512",
                                     "11": "sha224"}[s.split()[3]]
                except (IndexError, KeyError):
                    pass
        body = (f"MIME-Version: 1.0\r\nContent-Type: multipart/signed; micalg={algo}; "
                f"protocol=\"application/pgp-signature\"; boundary=\"{b}\"\r\n\r\n"
                f"This is an OpenPGP/MIME signed message (RFC 4880 and 3156)\r\n--{b}\r\n").encode() + entity + \
            (f"\r\n--{b}\r\nContent-Type: application/pgp-signature; name=\"signature.asc\"\r\n"
             f"Content-Description: OpenPGP digital signature\r\nContent-Disposition: attachment; "
             f"filename=\"signature.asc\"\r\n\r\n").encode() + out.replace(b"\n", b"\r\n").replace(
                 b"\r\r\n", b"\r\n") + f"\r\n--{b}--\r\n".encode()
        return head.encode() + b"\r\n" + body
    return msg.as_bytes(policy=email.policy.SMTP)


# ---------------------------------------------------------------- Lesen
def looks_pgp(raw):
    head = raw[:400000]
    return (b"multipart/encrypted" in head or b"application/pgp-signature" in head
            or b"-----BEGIN PGP MESSAGE-----" in head or b"-----BEGIN PGP SIGNED MESSAGE-----" in head)


def _split_multipart(raw_body, boundary):
    """Teile eines multipart-Körpers byte-genau (für die Signaturprüfung). → [bytes]"""
    delim = b"--" + boundary.encode()
    text = raw_body
    parts, idx = [], []
    for m in re.finditer(rb"(?:^|\r?\n)" + re.escape(delim) + rb"(--)?[ \t]*(?=\r?\n|$)", text):
        idx.append((m.start(), m.end(), bool(m.group(1))))
    for i in range(len(idx) - 1):
        start = idx[i][1]
        if text[start:start + 2] == b"\r\n":
            start += 2
        elif text[start:start + 1] == b"\n":
            start += 1
        end = idx[i + 1][0]
        parts.append(text[start:end])
        if idx[i + 1][2]:
            break
    return parts


def _split_head_body(raw):
    m = re.search(rb"\r?\n\r?\n", raw)
    return (raw[:m.start()], raw[m.end():]) if m else (raw, b"")


def _sig_result(status, text):
    r = {"state": "unknown", "who": "", "fpr": "", "trust": ""}
    for s in status:
        w = s.split(" ", 2)
        if w[0] == "GOODSIG":
            r.update(state="good", who=w[2] if len(w) > 2 else "")
        elif w[0] in ("EXPSIG", "EXPKEYSIG"):
            r.update(state="expired", who=w[2] if len(w) > 2 else "")
        elif w[0] == "REVKEYSIG":
            r.update(state="revoked", who=w[2] if len(w) > 2 else "")
        elif w[0] == "BADSIG":
            r.update(state="bad", who=w[2] if len(w) > 2 else "")
        elif w[0] == "ERRSIG" and r["state"] == "unknown":
            r.update(state="nokey")
        elif w[0] == "NO_PUBKEY":
            r.update(state="nokey", fpr=w[1] if len(w) > 1 else "")
        elif w[0] == "VALIDSIG":
            r["fpr"] = w[1].split()[0] if len(w) > 1 else ""
        elif w[0].startswith("TRUST_"):
            r["trust"] = w[0][6:].lower()
    return r


def verify_detached(data, sig):
    with tempfile.NamedTemporaryFile(suffix=".asc", delete=False) as f:
        f.write(sig)
        path = f.name
    try:
        rc, _o, status, text = _run(["--verify", path, "-"], data)
    finally:
        os.unlink(path)
    return _sig_result(status, text)


def process(raw, decrypt=True, verify=True):
    """Eingehende Mail: entschlüsseln/prüfen. → (bytes zum Anzeigen, status) – status:
    {encrypted, decrypted, error, sig: {...}|None}"""
    st = {"encrypted": False, "decrypted": False, "error": None, "sig": None}
    if not GPG:
        return raw, st
    head, body = _split_head_body(raw)
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    ctype = msg.get_content_type()
    if ctype == "multipart/encrypted":
        st["encrypted"] = True
        if not decrypt:
            return raw, st
        armored = None
        for p in msg.iter_parts():
            if p.get_content_type() == "application/octet-stream":
                armored = p.get_payload(decode=True)
        if not armored:
            st["error"] = "verschlüsselter teil fehlt"
            return raw, st
        rc, out, status, text = _run(["--decrypt"], armored, timeout=300)
        if rc != 0 and not out:
            st["error"] = ("kein passender geheimer schlüssel" if any(s.startswith("NO_SECKEY") for s in status)
                           else "entschlüsseln abgebrochen" if any("CANCELED" in s or "cancel" in s.lower()
                                                                   for s in status)
                           else (text.strip().splitlines() or ["entschlüsseln fehlgeschlagen"])[-1])
            return raw, st
        st["decrypted"] = True
        if any(s.startswith(("GOODSIG", "BADSIG", "ERRSIG", "EXPKEYSIG", "REVKEYSIG")) for s in status):
            st["sig"] = _sig_result(status, text)
        # neue Mail: alte Kopfzeilen (ohne Content-*) + entschlüsselte Einheit
        keep = b"\r\n".join(l for l in re.split(rb"\r?\n(?![ \t])", head)
                            if not l.lower().startswith((b"content-", b"mime-version")))
        inner = out.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        new = keep + b"\r\nMIME-Version: 1.0\r\n" + inner
        inner_raw, inner_st = process(new, decrypt, verify)          # innen evtl. noch signiert
        if inner_st.get("sig"):
            st["sig"] = inner_st["sig"]
        return inner_raw, st
    if ctype == "multipart/signed" and "pgp-signature" in (msg.get_param("protocol") or ""):
        if not verify:
            return raw, st
        parts = _split_multipart(body, msg.get_boundary() or "")
        if len(parts) >= 2:
            data = re.sub(rb"(?<!\r)\n", b"\r\n", parts[0])
            sig = email.message_from_bytes(parts[1], policy=email.policy.default).get_payload(decode=True) or b""
            st["sig"] = verify_detached(data, sig)
        return raw, st
    # Inline-PGP im Text
    if b"-----BEGIN PGP MESSAGE-----" in raw and decrypt:
        changed = False
        for part in msg.walk():
            if part.get_content_type() != "text/plain":
                continue
            try:
                text = part.get_content()
            except Exception:
                continue
            m = re.search(r"-----BEGIN PGP MESSAGE-----.*?-----END PGP MESSAGE-----", text, re.S)
            if not m:
                continue
            st["encrypted"] = True
            rc, out, status, err = _run(["--decrypt"], m.group(0).encode(), timeout=300)
            if rc != 0 and not out:
                st["error"] = (err.strip().splitlines() or ["entschlüsseln fehlgeschlagen"])[-1]
                continue
            st["decrypted"] = True
            if any(s.startswith(("GOODSIG", "BADSIG", "ERRSIG")) for s in status):
                st["sig"] = _sig_result(status, err)
            part.set_content(text[:m.start()] + out.decode("utf-8", "replace") + text[m.end():])
            changed = True
        if changed:
            return msg.as_bytes(policy=email.policy.SMTP), st
    if b"-----BEGIN PGP SIGNED MESSAGE-----" in raw and verify:
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    text = part.get_content()
                except Exception:
                    continue
                m = re.search(r"-----BEGIN PGP SIGNED MESSAGE-----.*?-----END PGP SIGNATURE-----", text, re.S)
                if m:
                    rc, _o, status, err = _run(["--verify"], m.group(0).encode())
                    st["sig"] = _sig_result(status, err)
                    break
    return raw, st

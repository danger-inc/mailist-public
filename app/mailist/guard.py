# Schnelle Sicherheitsprüfung einer Mail – komplett lokal, ohne Netz.
# Stufen: "ok" (nichts auffällig), "info", "mittel", "hoch". Die Liste zeigt nur das Symbol, die Vorschau die Gründe.
import email.utils, json, re

import mime

BRANDS = ("paypal", "amazon", "apple", "icloud", "google", "microsoft", "outlook", "netflix", "dhl", "dpd", "hermes",
          "fedex", "sparkasse", "volksbank", "postbank", "commerzbank", "deutsche bank", "n26", "comdirect",
          "dkb", "telekom", "vodafone", "ebay", "klarna", "visa", "mastercard", "steam", "instagram", "facebook",
          "whatsapp", "linkedin", "elster", "finanzamt", "polizei", "zoll", "bundesagentur", "tu berlin", "isis")
SHORTENERS = {"bit.ly", "t.co", "tinyurl.com", "goo.gl", "ow.ly", "is.gd", "buff.ly", "rebrand.ly", "cutt.ly",
              "rb.gy", "shorturl.at", "t.ly", "s.id", "tiny.cc"}
FREEMAIL = {"gmail.com", "googlemail.com", "outlook.com", "outlook.de", "hotmail.com", "hotmail.de", "live.com",
            "gmx.de", "gmx.net", "web.de", "yahoo.com", "yahoo.de", "icloud.com", "t-online.de", "aol.com",
            "proton.me", "protonmail.com", "mail.ru", "yandex.ru"}
ORDER = {"ok": 0, "info": 1, "mittel": 2, "hoch": 3}
GLYPH = {"ok": "", "info": "", "mittel": "\U000F0026", "hoch": "\U000F0027"}


def auth_results(text):
    res = {}
    for k in ("spf", "dkim", "dmarc"):
        m = re.search(rf"\b{k}=(\w+)", text or "", re.I)
        if m:
            res[k] = m.group(1).lower()
    return res


def _dom(addr):
    return mime.base_domain(addr.rsplit("@", 1)[-1].lower().strip(" >")) if "@" in (addr or "") else ""


def quick(row):
    """Bewertung nur aus den Listen-Metadaten (für das Symbol in der Liste). → (stufe, [(stufe, text)])"""
    out = []
    try:
        h = json.loads(row["hdr"] or "{}")
    except ValueError:
        h = {}
    name, addr = row["from_name"] or "", row["from_addr"] or ""
    fd = _dom(addr)
    labels = row["labels"] or ""
    if " SENT " in labels and " INBOX " not in labels:
        return "ok", []
    a = auth_results(h.get("authentication-results", ""))
    if a:
        bad = [k.upper() for k, v in a.items() if v in ("fail", "softfail", "permerror")]
        if bad:
            out.append(("hoch", f"Absender-Prüfung fehlgeschlagen: {', '.join(bad)} – Absender evtl. gefälscht"))
        elif a.get("dmarc") == "pass" or (a.get("spf") == "pass" and a.get("dkim") == "pass"):
            out.append(("ok", "Absender bestätigt (" + " · ".join(f"{k.upper()} {v}" for k, v in a.items()) + ")"))
        else:
            out.append(("info", "Absender nur teilweise bestätigt (" +
                        " · ".join(f"{k.upper()} {v}" for k, v in a.items()) + ")"))
    rt = h.get("reply-to", "")
    if rt:
        rd = _dom(email.utils.parseaddr(rt)[1])
        if rd and fd and rd != fd:
            out.append(("mittel", f"Antworten gehen an eine andere Domain: {rd} (Absender: {fd})"))
    # Anzeigename täuscht eine Marke oder eine andere Adresse vor
    low = name.lower()
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", name)
    if m and _dom(m.group(0)) != fd:
        out.append(("hoch", f"Anzeigename enthält eine andere Adresse ({m.group(0)}) als der echte Absender ({addr})"))
    else:
        for b in BRANDS:
            if re.search(rf"\b{re.escape(b)}\b", low) and b.replace(" ", "") not in fd.replace("-", ""):
                if fd in FREEMAIL:
                    out.append(("hoch", f"Name „{name}“, aber verschickt von einer privaten {fd}-Adresse"))
                elif a.get("dmarc") != "pass":
                    out.append(("mittel", f"Name „{name}“, aber die Adresse gehört nicht zu {b.title()}: {fd}"))
                break
    if " SPAM " in labels:
        out.append(("mittel", "Gmail hat diese Mail als Spam einsortiert"))
    return level_of(out), out


def full(row, mail, store=None):
    """Bewertung mit geöffnetem Inhalt (Links, Anhänge, neuer Absender). → (stufe, gründe, link_warnungen)"""
    lvl, out = quick(row)
    out = list(out)
    segs, info = mail.render()
    link_warn = {}
    fd = _dom(row["from_addr"] or "")
    for text, href in info["links"]:
        host = mime.host_of(href)
        if not host:
            continue
        why = None
        shown = re.search(r"(?:https?://)?((?:[\w-]+\.)+[a-z]{2,})(?:/|\b)", text or "", re.I)
        if shown and "." in shown.group(1) and " " not in (text or "").strip():
            sd = mime.base_domain(shown.group(1).lower().removeprefix("www."))
            if sd and sd != mime.base_domain(host):
                why = f"Link zeigt „{shown.group(1)}“, führt aber zu {host}"
        if not why and (host.startswith("xn--") or ".xn--" in host):
            why = f"Getarnte Domain (Sonderzeichen): {host}"
        if not why and re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
            why = f"Link auf eine nackte IP-Adresse: {host}"
        if not why and host in SHORTENERS:
            why = f"Kurzlink ({host}) – Ziel unbekannt"
        if not why and "@" in href.split("?")[0].split("//", 1)[-1].split("/")[0]:
            why = "Link mit @ im Servernamen (Täuschung)"
        if why:
            link_warn[href] = why
    if link_warn:
        n = len(link_warn)
        first = next(iter(link_warn.values()))
        out.append(("hoch" if any("führt aber" in w or "Getarnte" in w for w in link_warn.values()) else "mittel",
                    f"{n} verdächtige{'r' if n == 1 else ''} Link{'s' if n > 1 else ''}: {first}"))
    risky = [a[0] for a in mail.attachments if a[0].lower().endswith(mime.RISKY_EXT)]
    if risky:
        out.append(("hoch", "Gefährlicher Anhang-Typ: " + ", ".join(risky[:3]) + " – nicht öffnen, wenn unerwartet"))
    dbl = [a[0] for a in mail.attachments if re.search(r"\.(pdf|docx?|jpe?g|png|txt)\.[a-z0-9]{2,4}$", a[0], re.I)]
    if dbl:
        out.append(("hoch", "Doppelte Dateiendung (Tarnung): " + ", ".join(dbl[:2])))
    if info["trackers"]:
        out.append(("info", f"{info['trackers']} Tracking-Pixel blockiert (Absender hätte gesehen, dass du liest)"))
    if store is not None and row["from_addr"] and " SENT " not in (row["labels"] or ""):
        try:
            seen = store.sent_before(row["from_addr"], row["date"])
        except Exception:
            seen = 1
        if seen == 0:
            out.append(("info", "Erste Mail von dieser Adresse"))
            if any(o[0] in ("mittel", "hoch") for o in out):
                out.append(("mittel", "Neuer Absender + Auffälligkeiten – typisch für Phishing"))
    return level_of(out), out, link_warn, (segs, info)


def level_of(items):
    lv = "ok"
    for l, _ in items:
        if ORDER[l] > ORDER[lv]:
            lv = l
    return lv

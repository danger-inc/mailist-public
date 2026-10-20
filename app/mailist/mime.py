# Mail zerlegen und lesbar machen – ohne Browser-Engine: HTML wird in formatierten Text umgewandelt
# (Überschriften, fett/kursiv, Listen, Zitate, Links). Externe Bilder werden nie geladen (Tracking-Pixel!),
# nur gezählt. Ergebnis: Segmente [(text, {stil…}, link|None)] für einen Gtk.TextBuffer.
import email, email.policy, email.utils, html, html.parser, re, urllib.parse

URL_RX = re.compile(r"(https?://[^\s<>\"')\]]+[^\s<>\"').,;:!?\]])", re.I)
BLOCK = {"p", "div", "section", "article", "header", "footer", "table", "tr", "ul", "ol", "dl", "dd", "dt",
         "center", "form", "main", "aside", "nav", "figure", "address", "h1", "h2", "h3", "h4", "h5", "h6"}
SKIP = {"style", "script", "head", "title", "noscript", "template", "svg", "object", "iframe"}
RISKY_EXT = (".exe", ".scr", ".bat", ".cmd", ".com", ".pif", ".js", ".jse", ".vbs", ".vbe", ".wsf", ".hta",
             ".msi", ".lnk", ".iso", ".img", ".jar", ".ps1", ".reg", ".html", ".htm", ".shtml", ".svg",
             ".docm", ".xlsm", ".pptm", ".dotm", ".xlam", ".one", ".apk", ".dll", ".cpl", ".vhd", ".vhdx")


class Mail:
    def __init__(self, raw):
        self.raw = raw
        self.msg = email.message_from_bytes(raw, policy=email.policy.default)
        self.attachments = []        # [(name, ctype, size, part)]
        self.inline = 0
        self.plain = None
        self.html = None
        self._walk(self.msg)

    def header(self, name):
        try:
            v = self.msg.get(name)
            return str(v) if v is not None else ""
        except Exception:
            try:
                return str(self.msg.get_all(name, [""])[0])
            except Exception:
                return ""

    def headers_text(self):
        """Alle Kopfzeilen wie im „Original anzeigen“."""
        head = self.raw.split(b"\r\n\r\n", 1)[0] if b"\r\n\r\n" in self.raw[:200000] else self.raw.split(b"\n\n", 1)[0]
        return head.decode("utf-8", "replace").replace("\r\n", "\n")

    def _walk(self, part):
        for p in part.walk():
            if p.is_multipart():
                continue
            ctype = p.get_content_type()
            disp = (p.get_content_disposition() or "").lower()
            name = p.get_filename()
            if name or disp == "attachment":
                try:
                    data = p.get_payload(decode=True) or b""
                except Exception:
                    data = b""
                if disp == "inline" and ctype.startswith("image/") and p.get("Content-ID"):
                    self.inline += 1
                    continue
                self.attachments.append((name or "anhang", ctype, len(data), p))
                continue
            if ctype == "text/plain" and self.plain is None:
                self.plain = _text(p)
            elif ctype == "text/html" and self.html is None:
                self.html = _text(p)
            elif ctype in ("message/rfc822",):
                self.attachments.append(("weitergeleitet.eml", ctype, 0, p))

    def date(self):
        try:
            return email.utils.parsedate_to_datetime(self.header("Date"))
        except (TypeError, ValueError):
            return None

    def render(self, prefer_html=True):
        """→ (segmente, info) – info: links [(text, href)], remote_images, trackers"""
        if self.html and (prefer_html or not self.plain):
            r = HtmlText()
            try:
                r.feed(self.html)
                r.close()
            except Exception:
                pass
            segs = r.result()
            if sum(len(s[0]) for s in segs) > 20 or not self.plain:
                return segs, {"links": r.links, "remote_images": r.remote, "trackers": r.trackers, "html": True}
        segs, links = plain_segments(self.plain or "")
        return segs, {"links": links, "remote_images": 0, "trackers": 0, "html": False}

    def payload(self, part):
        try:
            if part.get_content_type() == "message/rfc822":
                inner = part.get_payload()
                inner = inner[0] if isinstance(inner, list) else inner
                return inner.as_bytes()
            return part.get_payload(decode=True) or b""
        except Exception:
            return b""


def _text(part):
    try:
        return part.get_content()
    except Exception:
        raw = part.get_payload(decode=True) or b""
        cs = part.get_content_charset() or "utf-8"
        try:
            return raw.decode(cs, "replace")
        except LookupError:
            return raw.decode("utf-8", "replace")


def plain_segments(text):
    segs, links = [], []
    text = text.replace("\r\n", "\n")
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    for line in text.split("\n"):
        st = {"quote"} if line.startswith(">") else set()
        pos = 0
        for m in URL_RX.finditer(line):
            if m.start() > pos:
                segs.append((line[pos:m.start()], st, None))
            url = m.group(1)
            segs.append((url, st | {"link"}, url))
            links.append((url, url))
            pos = m.end()
        segs.append((line[pos:] + "\n", st, None))
    return segs, links


class HtmlText(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []          # (text, frozenset(stile), href)
        self.stack = []        # offene Stil-Tags
        self.skip = 0
        self.href = None
        self.link_text = ""
        self.links = []
        self.remote = 0
        self.trackers = 0
        self.pre = 0
        self.lists = []        # ["ul"|("ol", n)]
        self.need_space = False
        self.hidden = []       # Tiefe von display:none-Elementen

    # ---- Ausgabe-Hilfen
    def _styles(self):
        s = set()
        for t in self.stack:
            if t in ("b", "strong"):
                s.add("bold")
            elif t in ("i", "em", "cite"):
                s.add("italic")
            elif t in ("h1", "h2"):
                s.add("h1")
            elif t in ("h3", "h4", "h5", "h6"):
                s.add("h2")
            elif t == "blockquote":
                s.add("quote")
            elif t in ("code", "tt", "pre", "kbd"):
                s.add("mono")
            elif t in ("s", "strike", "del"):
                s.add("strike")
            elif t == "small":
                s.add("small")
        if self.href:
            s.add("link")
        return frozenset(s)

    def _emit(self, text):
        if not text:
            return
        self.out.append((text, self._styles(), self.href))

    def _nl(self, n=1):
        """Zeilenumbruch(e), aber höchstens n am Stück (Leerzeilen-Lawinen aus Tabellen-Layouts vermeiden)."""
        tail = 0
        for t, _, _ in reversed(self.out):
            k = len(t) - len(t.rstrip("\n"))
            tail += k
            if t.strip("\n") or k < len(t):
                break
        if not self.out:
            return
        if tail < n:
            self.out.append(("\n" * (n - tail), frozenset(), None))
        self.need_space = False

    # ---- Parser
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        style = (a.get("style") or "").replace(" ", "").lower()
        if self.hidden:
            if tag not in VOID:
                self.hidden[-1] += 1
            return
        if "display:none" in style or "visibility:hidden" in style or a.get("hidden") is not None:
            if tag == "img":
                self._img(a, True)
            if tag not in VOID:
                self.hidden.append(1)
            return
        if tag in SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "br":
            self._emit("\n")
            self.need_space = False
        elif tag == "hr":
            self._nl(1)
            self._emit("─" * 24 + "\n")
        elif tag == "img":
            self._img(a, False)
        elif tag == "a":
            href = (a.get("href") or "").strip()
            if href and not href.lower().startswith(("javascript:", "#")):
                self.href = href
                self.link_text = ""
        elif tag == "li":
            self._nl(1)
            depth = max(0, len(self.lists) - 1)
            if self.lists and isinstance(self.lists[-1], list):
                self.lists[-1][1] += 1
                bullet = f"{self.lists[-1][1]}. "
            else:
                bullet = "• "
            self.out.append(("   " * depth + "  " + bullet, frozenset({"bullet"}), None))
            self.need_space = False
        elif tag in ("ul", "ol"):
            self._nl(1)
            self.lists.append(["ol", 0] if tag == "ol" else "ul")
        elif tag in ("td", "th"):
            if self.out and not self.out[-1][0].endswith(("\n", " ", "\t")):
                self._emit("   ")
        elif tag == "pre":
            self._nl(1)
            self.pre += 1
        elif tag == "blockquote":
            self._nl(2)
        elif tag in BLOCK:
            self._nl(2 if tag in ("p", "h1", "h2", "h3", "table") else 1)
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if self.hidden:
            self.hidden[-1] -= 1
            if self.hidden[-1] <= 0:
                self.hidden.pop()
            return
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "a" and self.href:
            text = self.link_text.strip()
            self.links.append((text, self.href))
            self.href = None
        if tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                if t == tag:
                    break
        if tag in ("ul", "ol") and self.lists:
            self.lists.pop()
            self._nl(1)
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            self._nl(1)
        elif tag in ("p", "h1", "h2", "h3", "blockquote", "table"):
            self._nl(2)
        elif tag in BLOCK or tag == "li":
            self._nl(1)

    def handle_data(self, data):
        if self.skip or self.hidden:
            return
        if self.pre:
            self._emit(data)
            return
        text = re.sub(r"[\s​‌͏­﻿]+", " ", data.replace(" ", " "))
        if not text.strip():
            if text and self.out and not self.out[-1][0].endswith(("\n", " ")):
                self.need_space = True
            return
        if text.startswith(" ") and self.out and not self.out[-1][0].endswith(("\n", " ")):
            self.need_space = True
        text = text.strip()
        if self.need_space and self.out and not self.out[-1][0].endswith(("\n", " ")):
            text = " " + text
        self.need_space = data[-1:].isspace()
        if self.href is not None:
            self.link_text += text
        self._emit(text)

    def _img(self, a, hidden):
        src = (a.get("src") or "").strip()
        if not src.lower().startswith(("http://", "https://", "//")):
            return
        self.remote += 1
        w, h = (a.get("width") or "").strip("px "), (a.get("height") or "").strip("px ")
        tiny = w in ("0", "1", "2") or h in ("0", "1", "2")
        if hidden or tiny or re.search(r"(track|pixel|beacon|open\.|/o/|/open|wf/open|e\.gif)", src, re.I):
            self.trackers += 1
            return
        alt = (a.get("alt") or "").strip()
        if alt and len(alt) < 80:
            self.out.append((f"[{alt}]", frozenset({"img"}), self.href))
        elif self.href:
            self.out.append(("[bild]", frozenset({"img"}), self.href))

    def result(self):
        segs = []
        for text, st, href in self.out:
            if segs and segs[-1][1] == st and segs[-1][2] == href:
                segs[-1] = (segs[-1][0] + text, st, href)
            else:
                segs.append((text, st, href))
        # Anfang/Ende säubern, Leerzeilen begrenzen, Leerzeichen am Zeilenanfang entfernen
        out, nl = [], 2
        for text, st, href in segs:
            if "mono" not in st:
                text = re.sub(r"\n[ \t]+", "\n", text)
                text = re.sub(r"[ \t]+\n", "\n", text)
            buf = []
            for ch in text:
                if ch == "\n":
                    nl += 1
                    if nl > 2:
                        continue
                else:
                    if nl and ch == " " and "bullet" not in st:
                        continue
                    nl = 0
                buf.append(ch)
            if buf:
                out.append(("".join(buf), set(st), href))
        while out and not out[-1][0].strip():
            out.pop()
        return out


VOID = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}


def host_of(url):
    try:
        h = urllib.parse.urlsplit(url if "://" in url else "http://" + url).hostname or ""
    except ValueError:
        return ""
    return h.lower().removeprefix("www.")


def base_domain(host):
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov", "edu") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def safe_filename(name):
    name = re.sub(r"[/\\\x00-\x1f]", "_", name).strip(". ") or "anhang"
    return name[:180]


def html_page(mail):
    """Für „im Browser öffnen“: HTML mit gesperrten externen Inhalten (CSP) – nichts lädt nach."""
    body = mail.html or "<pre>" + html.escape(mail.plain or "") + "</pre>"
    csp = ("<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; "
           "img-src data:; font-src data:\">")
    return f"<!doctype html><html><head><meta charset='utf-8'>{csp}</head><body>{body}</body></html>"

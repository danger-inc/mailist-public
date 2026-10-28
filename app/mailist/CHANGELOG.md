# mailist

## 1.8.0 (2026-10-28)
- **Abgelaufene Anmeldung wird gemeldet**: Läuft der Google-Zugang ab (im Testmodus nach 7 Tagen), kommt eine Benachrichtigung „neu anmelden“ – ein Klick öffnet die Konto-Seite. Schon einen Tag vorher gibt es eine Vorwarnung. Deine Mails bleiben gespeichert.
- **Entwürfe gehen nicht mehr verloren**: Das Schreibfenster sichert Empfänger, Betreff und Text alle 15 Sekunden lokal. Nach Absturz oder Neustart öffnet sich der Entwurf von selbst wieder. Beim Senden oder bewussten Verwerfen wird die Sicherung gelöscht.
- Gemeinsame Module (Changelog-Fenster, Design) auf den neuesten Stand der Suite gebracht.

## 1.7.0 (2026-10-27)
- **Wechsel-Animation**: Der Wechsel zwischen Hell und Dunkel (und zwischen Designs) läuft als Scanbalken über den ganzen Bildschirm. Alle Fenster, die Leiste und das Terminal wechseln darunter im selben Moment, hinter dem Balken steht das neue Design. An/aus und Dauer (Standard 1,5 s) stehen in der Designauswahl der Einstellungen.

## 1.6.2 (2026-10-26)
- **Vorschau springt nicht mehr beim Doppelklick im Fließtext**: Der Textbereich scrollte beim Fokuswechsel von selbst an eine andere Stelle – abgeschaltet.
- **Vorschau beruhigt**: Ist die Vorschau neben der Liste sichtbar, wählt ein Klick auf eine Mail nur aus – die Ansicht springt nicht mehr in die große Leseansicht. Doppelklick auf eine Mail öffnet sie groß; im Text markiert Doppelklick ein Wort. Ein Absturz bei Klicks in der Liste ist behoben.
- **Mehr im Rechtsklick**: Mailliste (neuer Tab, Adresse/Betreff kopieren, .eml speichern, Quelltext, im Browser öffnen) und Mailtext (ganzen Text kopieren, Auswahl suchen, Auswahl im Web suchen).

## 1.6.1 (2026-10-26)
- **Adressen im Mailkopf zum Kopieren**: Absender, „an“ und „cc“ sind jetzt Knöpfe – ein Klick kopiert die Adresse.
- **Rechtsklick im Suite-Stil**: Auf einer Adresse (kopieren, neue Mail, alle von ihr zeigen) und im Mailtext (Link öffnen/kopieren, kopieren, alles auswählen) erscheint das mailist-Menü statt des grauen GTK-Menüs.

## 1.6.0 (2026-10-26)
- **Design wählbar**: In den Einstellungen gibt es die Designauswahl wie in notist (Automatisch + 9 Designs). Die Wahl gilt für alle -ist-Apps zugleich und wirkt sofort. Bei „Automatisch“ (Desktop-Farben) kannst du zwischen **Dunkel** und **Hell** umschalten.

## 1.5.1 (2026-10-25)
- **Update-Knopf**: Neben der Versionsnummer steht „󰑐“ – Klick sucht sofort nach einem Update. Gibt es eine neuere
  Version, wird daraus „󰚰 update“; ein Klick lädt und installiert sie (Linux und Windows), danach heißt er „󰑓 neu starten“.
- **Rückmeldung bei Umschalt-Knöpfen**: „An“ (z. B. „Ton behalten“) ist jetzt auch unter dem Mauszeiger zu sehen,
  Drücken färbt sofort – man muss die Maus nicht mehr wegbewegen, um zu sehen, ob der Klick angekommen ist.
- **Windows: echte Titelleiste** – das Fenster lässt sich verschieben, in der Größe ändern, minimieren und
  maximieren.

## 1.5.0 (2026-10-24)
- **Hover-Übersetzer von linguist**: Ist er in linguist eingeschaltet, zeigt der Knopf „übersetzer ◢“ im Schreibfenster,
  ob er an ist (Klick = an/aus, Rechtsklick = linguist-Einstellungen, Strg+Umschalt+T = Karte mit markiertem Text).
  Ohne linguist (oder mit Hover aus, Standard) bleibt alles wie bisher.

## 1.4.0 (2026-10-24)
- **Tab im Betreff** springt direkt ins Schreibfeld.
- **Bestätigung nach dem Senden**: „✓ gesendet“ in der Statusleiste und – wenn das Fenster nicht vorn ist – als
  Benachrichtigung.
- **Einstellungen** auch als Zahnrad oben rechts.
- **Dateien ins Schreibfenster ziehen** (z. B. aus filist) hängt sie als Anhang an.
- **Rechtschreibprüfung** (Deutsch + Englisch, Rechtsklick auf rote Wörter) und **Übersetzer** aus notist im
  Schreibfenster (`Strg+Umschalt+T`, markierter Text wird übernommen).
- Die Anhangsleiste ist nur noch da, wenn es Anhänge gibt, und so schmal wie nötig.

## 1.3.0 (2026-10-22)
- **Klick öffnet die Mail**: Ein Klick auf eine Mail öffnet sie groß – die Ordner links bleiben, Liste und
  Vorschau machen Platz. Zurück mit `Esc`, dem Zurück-Knopf, der Maustaste „zurück“ oder 2-Finger-Wischen. Mit den
  Pfeiltasten durch die Liste gehen zeigt weiter die Vorschau, `Enter` öffnet. Gilt jetzt auch im großen Fenster.
- **`F3` schaltet wirklich die Vorschau**: Vorher verschwand im großen Fenster die Mail-Liste statt der Vorschau.
- **Externe Bilder laden**: Bei Mails mit Bildern aus dem Netz steht oben „N externe bilder nicht geladen ·
  laden“ – ein Klick holt sie in die Mail. Unter Einstellungen → allgemein lässt sich **„externe bilder automatisch
  laden“** einschalten (Standard: aus). Tracker-Pixel bleiben immer blockiert.

## 1.2.0 (2026-10-21)
- **Neu: mehrere Postfächer.** Unter „konten“ beliebig viele hinzufügen – weitere Gmail-Konten und jedes
  Postfach mit **IMAP/SMTP** (GMX, web.de, Outlook, Posteo, mailbox.org, iCloud werden an der Adresse erkannt).
  Wechseln oben in der Ordnerleiste oder mit `Strg+Shift+A`; alle gleichen im Hintergrund ab und melden neue Mails
- **Neu: OpenPGP** (über gpg): im Schreibfenster signieren und verschlüsseln – verschlüsseln schaltet sich von
  selbst ein, wenn für alle Empfänger ein Schlüssel da ist. Beim Lesen werden verschlüsselte Mails entschlüsselt
  und Signaturen geprüft (gültig / verändert / Schlüssel fehlt). Eigenen Schlüssel erzeugen, kopieren,
  importieren; Schlüssel aus Anhängen übernehmen; Autocrypt
- **Neu: Einstellungsseite** (links unten) mit allen sicherheitsrelevanten Schaltern: Passwörter im
  Schlüsselbund, nur verschlüsselte Verbindungen, Zertifikate streng prüfen, Sicherheits-Check, Rückfrage bei
  verdächtigen Links / jedem Link / gefährlichen Anhängen, alle OpenPGP- und Autocrypt-Optionen
- Passwörter liegen im System-Schlüsselbund – nie im Klartext in einer Datei
- Eigene Adressen werden in Konten und Einstellungen gekürzt angezeigt (sicher für Bildschirmfotos)

## 1.1.0 (2026-10-21)
- **Neu:** Mails formatiert schreiben – Leiste über dem Text: fett, kursiv, unterstrichen, durchgestrichen,
  Schriftgröße, Schriftart, Textfarbe, Textmarker, Aufzählung, nummerierte Liste, Zitat, zentriert, Links;
  `Strg+B/I/U`, `Strg+K` = Link, `Strg+Leertaste` = Formatierung weg. Ohne Formatierung bleibt es reiner Text
- **Neu:** ältere Mails laden sich von selbst, sobald man sich dem Listenende nähert
- „ältere laden“ holt jetzt lückenlos die nächstälteren Mails (vorher sprang es teils um Jahre zurück)
- Pfeiltasten springen nicht mehr nach oben, wenn eine Mail gerade als gelesen markiert oder gelöscht wurde
- Entf im Spam-Ordner wirkt wieder (Mail wandert in den Papierkorb, Strg+Z legt sie zurück in Spam)
- „von Gmail abgelehnt“: Gmail bremst bei vielen Änderungen kurz – das galt fälschlich als Ablehnung;
  jetzt wird automatisch erneut gesendet. Lehnt Gmail wirklich einzelne Mails ab, klappt der Rest trotzdem
- Die Ordnerleiste bleibt bei schmaleren Fenstern stehen (verschwindet erst unter 600 px)

## 1.0.0 (2026-10-20)
- **Neu:** mailist – schlankes Gmail-Programm im Look der Suite, aufgebaut wie filist: links Ordner und Labels,
  in der Mitte die Liste mit Spalten (sortierbar), rechts die Vorschau, oben Tabs (Strg+T, Alt+1–9)
- Gmail-Abgleich über die Gmail-API mit demselben Google-Zugang wie datist; Mails liegen im Zwischenspeicher –
  das Fenster ist sofort da und offline lesbar, Änderungen gehen raus, sobald wieder Netz da ist
- **Leicht löschen:** Entf (Papierkorb), Shift+Entf (endgültig), Papierkorb-Knopf an jeder Zeile, Strg+Z holt
  alles zurück; mehrere auswählen → große Knöpfe; „aufräumen“ zeigt, wer am meisten schickt – alles von einem
  Absender mit einem Klick weg, Newsletter abbestellen
- Sicherheits-Check jeder Mail (lokal): SPF/DKIM/DMARC, gefälschte Absendernamen, andere Antwortadresse,
  getarnte Links (rot, echtes Ziel unten in der Statuszeile), gefährliche Anhänge, Tracking-Pixel blockiert
- Schreiben, antworten, weiterleiten (mit Anhängen), Adressvorschläge, mailto:-Links
- Läuft im Hintergrund weiter, meldet neue Mails; Super+G zeigt/versteckt das Fenster
- 2-Finger-Wischen = zurück/vor, Mails per Ziehen auf Ordner verschieben

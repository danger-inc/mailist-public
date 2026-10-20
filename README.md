# mailist

Schlankes Gmail-Programm im Terminal-Look der Suite (GTK4), aufgebaut wie ein Datei-Explorer:
links Ordner und Labels, in der Mitte die Mail-Liste mit Spalten, rechts die Vorschau, oben Tabs.
Mails liegen in einem lokalen Zwischenspeicher – das Fenster ist sofort da und auch offline lesbar.

## Installieren – Linux

```bash
gh auth login                                   # einmalig (Repo ist privat, Einladung nötig)
gh repo clone invincible-nix/mailist ~/mailist-src
cd ~/mailist-src && ./install.sh
```
```bash
git clone https://github.com/invincible-nix/mailist-public ~/mailist-src
cd ~/mailist-src && ./install.sh
```

Der Installer fragt nach den Systempaketen (GTK4, PyGObject – einmal Passwort), kopiert mailist nach
`~/.local/share/mailist` und legt den Befehl `mailist` und einen Menüeintrag an. Symbole bringt mailist selbst mit.

## Mit Gmail verbinden (einmalig)

mailist spricht direkt mit der Gmail-API – über **deinen eigenen** Google-Zugang, kein fremder Server dazwischen.

1. [Google Cloud Console](https://console.cloud.google.com) → Projekt anlegen → **Google Auth Platform** →
   Client erstellen, Typ **Desktop-App** → JSON herunterladen. Unter „Zielgruppe“ dich als Testnutzer eintragen.
   (Wer datist schon eingerichtet hat: mailist nimmt denselben Zugang automatisch.)
2. Im selben Projekt die **Gmail API** aktivieren (mailist hat dafür einen Knopf).
3. In mailist links auf **konto** → „mit google anmelden“. Google warnt, die App sei nicht überprüft – es ist dein
   eigener Zugang: „Weiter“ und den Haken bei Gmail setzen.

Im Testmodus beendet Google die Anmeldung nach 7 Tagen; mailist zeigt dann oben „⚠ neu anmelden“.

## Bedienung

- **Löschen:** Entf (Papierkorb), Shift+Entf (endgültig), Papierkorb-Knopf an jeder Zeile, Strg+Z macht es
  rückgängig. Mehrere auswählen (Strg/Shift-Klick, Strg+A) → große Knöpfe für alle.
- **aufräumen** (links): wer schickt am meisten? Alle Mails eines Absenders mit einem Klick weg, Newsletter abbestellen.
- **Suche:** tippen filtert sofort lokal, Enter sucht in ganz Gmail (auch `from:` `has:attachment` …).
- Tasten: r antworten · a allen · f weiterleiten · e archivieren · s Stern · u gelesen/ungelesen · j/k nächste/vorige ·
  Strg+N neu · Strg+T Tab · F9 Ordnerleiste · F3 Vorschau · F5 abgleichen. Zwei Finger wischen = zurück/vor.
- Mails auf einen Ordner links ziehen = verschieben (Papierkorb, Spam, Label).
- **Sicherheit:** Jede Mail wird lokal geprüft – SPF/DKIM/DMARC, gefälschte Absendernamen, andere Antwortadresse,
  getarnte Links (rot; das echte Ziel steht beim Drüberfahren unten), gefährliche Anhänge. Externe Bilder und
  Tracking-Pixel werden nie geladen.

`mailist --daemon` hält mailist im Hintergrund bereit (gleicht ab, meldet neue Mails), `mailist --toggle` zeigt/versteckt
das Fenster (gut für ein Tastenkürzel), `mailist mailto:…` schreibt eine neue Mail.

## Daten

- Zwischenspeicher: `~/.cache/mailist/mail.db` (nur eine Kopie – Gmail hat alles; löschen schadet nicht)
- Einstellungen und Anmeldung: `~/.config/mailist` (Token nur für dich lesbar)
- Entfernen: `./uninstall.sh` – in Gmail selbst ändert sich nichts.

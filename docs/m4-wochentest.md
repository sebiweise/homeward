# M4 Wochentest: bidirektionaler Sync auf einem Testordner

Abnahmekriterium für M4: ein Testordner läuft eine Woche lang bidirektional mit
iCloud Drive, ohne Fehler, ohne verlorene Dateien und ohne unerwartete Löschbremse.

> Der Etag-Test aus M3 hat gezeigt, dass iCloud die Datei-ID beim Umbenennen und
> Verschieben behält. Umbenennungen auf beiden Seiten zählen deshalb nicht für die
> Löschbremse.

## 1. Installation auf dem NAS (per SSH, ohne Paket)

Das Wheel liegt in den Projektdateien unter `m4-wochentest/`. Kopier es per File
Station nach `~/homeward-m4/` und installier es in ein eigenes venv:

```sh
PY=/var/packages/python312/target/bin/python3.12
cd ~/homeward-m4
$PY -m venv venv
venv/bin/pip install "./homeward-WHEEL.whl[icloud]"   # WHEEL = Name der Datei
venv/bin/homeward --version
```

## 2. Testordner und Konfiguration

- NAS: `/volume1/homes/<du>/Homeward-Test` anlegen (`<du>` = dein DSM-Benutzer).
- iCloud Drive: Ordner `Homeward-Test` anlegen (am Mac oder in der Dateien-App).
- Ein paar echte Dateien auf beide Seiten legen: Office-Dokumente, Fotos, eine
  große Datei (> 1 GB), Unterordner.

`~/homeward-m4/config.toml`. Der `state_dir` zeigt auf den Ordner aus dem M2-Test,
so wird deine bestehende Anmeldung wiederverwendet:

```toml
state_dir = "/var/services/homes/<du>/homeward-m2/state"

[[pair]]
name = "wochentest"
account = "sebastian"
apple_id = "DEINE@APPLE.ID"
local = "/volume1/homes/<du>/Homeward-Test"
remote = "Homeward-Test"
mode = "bidirectional"
beta = true
remote_provider = "icloud_drive"
```

Falls die Anmeldung abgelaufen ist, meldet der Sync das. Dann einmal neu anmelden,
mit Passwort und 2FA-Code:

```sh
venv/bin/homeward login --config config.toml --account sebastian
```

Erster Lauf, erst trocken, dann echt:

```sh
cd ~/homeward-m4
venv/bin/homeward sync --config config.toml --dry-run
venv/bin/homeward sync --config config.toml
```

Der erste Lauf löscht und überschreibt nie. Gleichnamige Dateien mit
unterschiedlichem Inhalt werden zu Konfliktkopien
(`Name (Konflikt NAS 2026-10-11).ext`).

## 3. Automatisch alle 15 Minuten

DSM → Systemsteuerung → Aufgabenplaner → Erstellen → Geplante Aufgabe →
Benutzerdefiniertes Skript, Benutzer: dein Benutzer (nicht root), täglich, alle
15 Minuten:

```sh
cd /var/services/homes/<du>/homeward-m4 && \
{ date; venv/bin/homeward sync --config config.toml; echo "exit $?"; } >> sync.log 2>&1
```

## 4. Was du in der Woche tust

Jeden Tag ein paar Dinge, mal am NAS (File Station oder SMB), mal am Mac/iPhone:

| Tag | NAS | iCloud |
| --- | --- | --- |
| 1 | Datei bearbeiten, neue Datei anlegen | andere Datei bearbeiten |
| 2 | Datei umbenennen, Datei in Unterordner verschieben | Datei löschen |
| 3 | Ordner mit vielen Dateien umbenennen | neue Fotos ablegen |
| 4 | dieselbe Datei wie in iCloud bearbeiten (Konflikt) | dieselbe Datei bearbeiten |
| 5 | Groß-/Kleinschreibung ändern (`notiz.txt` → `Notiz.txt`) | Datei umbenennen |
| 6 | Datei löschen, während sie in iCloud bearbeitet wird | große Datei ersetzen |
| 7 | nichts | nichts (der Sync muss zur Ruhe kommen) |

## 5. Prüfen

Täglich kurz:

```sh
grep -E "FAILED|Delete brake|exit [^0]" ~/homeward-m4/sync.log
```

Am Ende der Woche:

```sh
cd ~/homeward-m4
venv/bin/homeward sync --config config.toml --dry-run   # muss "nothing to do" zeigen
venv/bin/python - <<'EOF'
import sqlite3
db = sqlite3.connect("/var/services/homes/<du>/homeward-m2/state/sebastian.sqlite3")
runs, errors, blocked = db.execute(
    "SELECT count(*), sum(errors), count(blocked) FROM runs WHERE dry_run = 0").fetchone()
print(f"{runs} Läufe, {errors} Fehler, {blocked} von der Löschbremse gestoppt")
EOF
```

Die Woche war sauber, wenn

- kein `FAILED` und kein `exit 1` im Log steht (`exit 3` nur, wenn du absichtlich
  viel gelöscht hast),
- der Dry Run am Ende nichts mehr zu tun hat,
- beide Seiten dieselben Dateien zeigen und
- alles, was ersetzt oder gelöscht wurde, im iCloud-Papierkorb („Zuletzt gelöscht“)
  oder auf dem NAS im Papierkorb des Freigabeordners (`#recycle`, falls aktiviert)
  bzw. in `Homeward-Test/.homeward-papierkorb/<Datum>/` liegt.

Schick mir am Ende `sync.log` und die Ausgabe der beiden Befehle, dann werte ich
sie aus.

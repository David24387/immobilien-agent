# Weilburg Immobilien-Agent

Automatischer Suchagent für besondere Wohnimmobilien im ungefähr 40-km-Radius um Weilburg:
Mühlen, Höfe, Hofreiten, Resthöfe, Aussiedlerhöfe, Anwesen und ähnliche Objekte mit großen Grundstücken.

## Enthalten

- täglicher Suchlauf für besonders passende neue Treffer
- wöchentlicher Sonntagsreport an `david.t.mueller@gmx.de`
- Preisänderungserkennung und Preisverlauf
- Dubletten-Erkennung
- Grundstücks-, Wohnflächen-, Preis- und Bildauslesung, soweit verfügbar
- kurze Bewertung mit Pluspunkten und Prüfpunkten
- optional bessere KI-Bewertung über einen OpenAI API-Key
- manuell über GitHub Actions startbar

## Einrichtung in etwa 10 Minuten

1. Erstelle auf GitHub ein **privates Repository**.
2. Lade den gesamten Inhalt dieses Ordners hoch.
3. Öffne im Repository: `Settings → Secrets and variables → Actions`.
4. Lege diese Repository Secrets an:
   - `SMTP_USERNAME`: deine vollständige GMX-Absenderadresse
   - `SMTP_PASSWORD`: das Passwort dieser GMX-Adresse
   - optional `OPENAI_API_KEY`: für KI-Zusammenfassungen. Ohne Key läuft eine lokale regelbasierte Bewertung.
5. Aktiviere in GMX den Zugriff für Mailprogramme: `E-Mail-Einstellungen → POP3/IMAP Abruf → POP3 und IMAP Zugriff erlauben`.
6. Öffne `Actions → Weekly property report → Run workflow` für den ersten Test.
7. Prüfe dein GMX-Postfach und gegebenenfalls den Spam-Ordner.

Der Versand nutzt `mail.gmx.net` mit STARTTLS auf Port `587`.

## Suchparameter ändern

Alle wichtigen Einstellungen stehen in `config.yml`:

- `radius_km`: Dokumentation des gewünschten Radius
- `min_plot_sqm`: derzeit 2.000 m²
- `max_price_eur`: `null` bedeutet ohne Preisobergrenze
- `keywords`: gesuchte Objektarten
- `places`: Orte, die den ungefähr 40-km-Radius abbilden
- `domains`: durchsuchte Immobilienportale
- `top_match.min_score`: Schwelle für tägliche Sofortmeldungen
- `top_match.min_plot_sqm`: Mindestgrundstück für Sofortmeldungen

## Wichtige Hinweise

Der Agent nutzt gezielte Suchmaschinenabfragen und liest anschließend öffentlich erreichbare Metadaten der Treffer aus. Das ist robuster als ein direktes aggressives Scraping einzelner Portale. Trotzdem können Portale Inhalte ändern, Suchmaschinen Treffer verzögert indexieren oder Seitenzugriffe blockieren. Angaben und Verfügbarkeit deshalb stets im Originalinserat prüfen.

GitHub-Zeitpläne können einige Minuten verzögert starten. Der tägliche Lauf ist auf 06:17 Uhr, der Wochenreport sonntags auf 08:13 Uhr in `Europe/Berlin` eingestellt.

In öffentlichen GitHub-Repositories können geplante Workflows nach längerer Inaktivität deaktiviert werden. Ein privates Repository ist für Zugangsdaten und Suchzustand ohnehin die richtige Wahl.

## Lokaler Test ohne Mail

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python src/agent.py --mode weekly --dry-run
```

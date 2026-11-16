"""Search engines: guide pages for what people search on Google, sitemap.xml and robots.txt.

Each guide answers one real question (e.g. "XRechnung öffnen"), offers the matching free
tool right on the page and then the account. Content lives in GUIDES so pages stay consistent.
"""
from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from app.api.deps import optional_user, page
from app.core.config import settings

router = APIRouter()
UPDATED = date(2026, 11, 16)

GUIDES: dict[str, dict] = {
    "xrechnung-oeffnen": {
        "title": "XRechnung öffnen und lesen – kostenlos, ohne Installation",
        "short": "XRechnung öffnen",
        "description": "XRechnung bekommen und nur XML-Code gesehen? Hier öffnest du sie kostenlos im Browser: "
                       "lesbar wie eine normale Rechnung, nachgerechnet und offiziell geprüft.",
        "h1": "XRechnung öffnen und lesen",
        "intro": "Du hast eine XRechnung bekommen und siehst nur Code? Zieh die Datei hier hinein: Du siehst sie "
                 "sofort als lesbare Rechnung – kostenlos, ohne Anmeldung, ohne Installation.",
        "tool": "check",
        "sections": [
            ("Warum sich eine XRechnung nicht wie ein PDF öffnen lässt",
             ["Eine XRechnung ist keine Seite zum Anschauen, sondern eine Datei mit Daten (XML) – gemacht für "
              "Programme, nicht für Menschen. Öffnest du sie im Browser oder Editor, siehst du nur Fachbegriffe "
              "und Zahlen zwischen spitzen Klammern.",
              "Ein Anzeigeprogramm übersetzt diese Daten wieder in eine normale Rechnung: Absender, Empfänger, "
              "Positionen, Beträge, Bankverbindung."]),
            ("So öffnest du eine XRechnung",
             ["1. Die XML-Datei aus der E-Mail speichern (meist endet sie auf .xml).",
              "2. Oben in das Feld ziehen oder antippen und auswählen.",
              "3. Die Rechnung erscheint lesbar – mit dem Prüfergebnis darunter."]),
            ("Was zusätzlich geprüft wird",
             ["Das Tool rechnet Positionen, Netto, Umsatzsteuer und Zahlbetrag nach, prüft Pflichtangaben und "
              "die IBAN und lässt die Datei durch das offizielle Prüfprogramm der KoSIT laufen – dasselbe, das "
              "auch Behörden verwenden."]),
            ("Wichtig für die Aufbewahrung",
             ["Bei einer E-Rechnung ist die XML-Datei die eigentliche Rechnung. Ein Ausdruck oder Bildschirmfoto "
              "reicht für die Aufbewahrung nicht: Die Originaldatei muss unverändert aufbewahrt werden (GoBD). "
              "Im Konto von E-Rechnung Agent wird jedes Original automatisch so archiviert."]),
        ],
        "faq": [
            ("Ist das Öffnen wirklich kostenlos?", "Ja. Das Anzeigen und Prüfen einzelner E-Rechnungen ist "
             "kostenlos und ohne Konto möglich."),
            ("Wird meine Rechnung gespeichert?", "Nein. Beim kostenlosen Prüfen wird die Datei nur zur Anzeige "
             "verarbeitet und nicht dauerhaft gespeichert. Die Verarbeitung erfolgt auf Servern in Deutschland."),
            ("Welche Formate gehen?", "XRechnung in beiden Varianten (UBL und CII) sowie ZUGFeRD- und "
             "Factur-X-PDFs mit eingebetteten Rechnungsdaten."),
            ("Ich bekomme viele XRechnungen. Geht das automatisch?", "Ja. Mit einem Konto bekommst du eine eigene "
             "E-Mail-Adresse für Rechnungen. Der Agent liest jede Rechnung, prüft sie und bereitet sie für "
             "DATEV vor – du schaust nur noch auf die Fälle, bei denen etwas nicht stimmt."),
        ],
    },
    "zugferd-pruefen": {
        "title": "ZUGFeRD-Rechnung prüfen und anzeigen – kostenlos online",
        "short": "ZUGFeRD prüfen",
        "description": "ZUGFeRD- oder Factur-X-PDF prüfen: eingebettete Daten auslesen, Beträge nachrechnen und "
                       "feststellen, ob die Rechnung als E-Rechnung gilt. Kostenlos und ohne Anmeldung.",
        "h1": "ZUGFeRD-Rechnung prüfen",
        "intro": "Eine ZUGFeRD-Rechnung sieht aus wie ein normales PDF – die eigentlichen Rechnungsdaten stecken "
                 "aber unsichtbar darin. Hier siehst du, was drinsteht, und ob alles stimmt.",
        "tool": "check",
        "sections": [
            ("Was ZUGFeRD ist",
             ["ZUGFeRD (in Frankreich: Factur-X) ist ein PDF mit eingebetteter XML-Datei. Menschen lesen das PDF, "
              "Programme lesen die XML-Daten. Für die E-Rechnungspflicht zählen die XML-Daten – nicht das, was "
              "im PDF zu sehen ist."]),
            ("Nicht jedes ZUGFeRD ist eine E-Rechnung",
             ["ZUGFeRD gibt es in mehreren Profilen. Die Profile MINIMUM und BASIC WL enthalten zu wenige Angaben "
              "und gelten in Deutschland nicht als E-Rechnung. Die Profile BASIC, EN 16931 (COMFORT), EXTENDED und "
              "XRECHNUNG erfüllen die Anforderungen.",
              "Wichtig ist außerdem, dass PDF und XML zusammenpassen. Weichen sie voneinander ab, gelten die "
              "XML-Daten."]),
            ("Was das Tool prüft",
             ["Ob überhaupt Rechnungsdaten eingebettet sind, ob die Beträge aufgehen, ob Pflichtangaben fehlen und "
              "ob die Daten den Regeln der Norm EN 16931 entsprechen (mit dem offiziellen KoSIT-Prüfprogramm)."]),
        ],
        "faq": [
            ("Mein PDF enthält keine Daten – was nun?", "Dann ist es ein normales PDF und keine E-Rechnung. Bis Ende "
             "2026 dürfen Lieferanten solche Rechnungen in der Regel noch schicken, danach je nach Umsatz nur noch "
             "übergangsweise."),
            ("Kostet die Prüfung etwas?", "Nein, einzelne Rechnungen prüfst du kostenlos und ohne Konto."),
            ("Kann ich ZUGFeRD-Rechnungen auch selbst schreiben?", "Ja – mit dem kostenlosen Rechnungsformular "
             "erstellst du ZUGFeRD-PDFs und XRechnungen."),
        ],
    },
    "xrechnung-erstellen": {
        "title": "XRechnung und ZUGFeRD kostenlos erstellen – online, ohne Software",
        "short": "XRechnung erstellen",
        "description": "E-Rechnung schreiben: XRechnung für Behörden oder ZUGFeRD-PDF für Geschäftskunden. "
                       "Online ausfüllen, Summen und Umsatzsteuer werden berechnet, offiziell geprüft.",
        "h1": "XRechnung und ZUGFeRD erstellen",
        "intro": "Formular ausfüllen, Format wählen, fertig: Du bekommst eine gültige E-Rechnung – als ZUGFeRD-PDF "
                 "für Geschäftskunden oder als XRechnung für Behörden.",
        "tool": "create",
        "sections": [
            ("Welches Format für wen?",
             ["ZUGFeRD-PDF für Unternehmen: Dein Kunde kann die Rechnung wie gewohnt als PDF lesen, sein Programm "
              "liest die eingebetteten Daten.",
              "XRechnung für Behörden und öffentliche Auftraggeber: Diese verlangen meist eine reine XML-Datei und "
              "eine Leitweg-ID, die du von der Behörde bekommst."]),
            ("Was auf eine Rechnung gehört",
             ["Name und Anschrift von dir und deinem Kunden, deine Steuernummer oder USt-IdNr., Rechnungsdatum, "
              "eine fortlaufende Rechnungsnummer, Leistung und Leistungsdatum, Nettobetrag, Steuersatz und "
              "Steuerbetrag (oder der Hinweis auf die Kleinunternehmerregelung) sowie der Gesamtbetrag. Das "
              "Formular fragt alles ab und rechnet selbst."]),
            ("Regelmäßig Rechnungen schreiben?",
             ["Mit einem Konto werden deine Firmendaten gespeichert, Rechnungsnummern laufen automatisch weiter, "
              "und du verschickst die Rechnung direkt per E-Mail an deinen Kunden."]),
        ],
        "faq": [
            ("Ist das wirklich kostenlos?", "Ja, einzelne Rechnungen erstellst du kostenlos. Im Konto sind im Tarif "
             "Free drei Rechnungen pro Monat enthalten, ab dem Tarif Start unbegrenzt."),
            ("Ist die Rechnung gültig?", "Die erzeugten Dateien entsprechen der Norm EN 16931 und werden mit dem "
             "offiziellen KoSIT-Prüfprogramm getestet. Für den Inhalt (Beträge, Steuersatz) bist du verantwortlich."),
            ("Ich bin Kleinunternehmer – geht das?", "Ja. Wähle „Kleinunternehmer nach § 19 UStG“, dann wird keine "
             "Umsatzsteuer berechnet und der Hinweis automatisch auf die Rechnung gesetzt."),
        ],
    },
    "e-rechnungspflicht": {
        "title": "E-Rechnungspflicht 2025, 2027, 2028: Was gilt für wen?",
        "short": "E-Rechnungspflicht",
        "description": "Die E-Rechnungspflicht im Überblick: seit 2025 Empfang, ab 2027 Versand ab 800.000 € "
                       "Umsatz, ab 2028 für alle. Mit Ausnahmen, Übergangsregeln und was du jetzt tun solltest.",
        "h1": "E-Rechnungspflicht: Was gilt wann für wen?",
        "intro": "Die E-Rechnung kommt in Deutschland in Stufen. Hier steht kurz und verständlich, was du wann "
                 "können musst – und was du jetzt tun kannst.",
        "tool": None,
        "sections": [
            ("Seit 1. Januar 2025: alle müssen E-Rechnungen empfangen können",
             ["Jedes Unternehmen in Deutschland muss E-Rechnungen von anderen Unternehmen annehmen und verarbeiten "
              "können. Ein E-Mail-Postfach reicht zum Empfangen, aber du brauchst eine Möglichkeit, die Dateien "
              "zu lesen und unverändert aufzubewahren."]),
            ("Bis Ende 2026: Übergangszeit",
             ["Beim Versand dürfen Unternehmen noch Papierrechnungen und normale PDFs verschicken (PDF mit "
              "Zustimmung des Empfängers)."]),
            ("Ab 1. Januar 2027: Versand für größere Unternehmen",
             ["Wer im Vorjahr mehr als 800.000 € Umsatz hatte, muss an andere Unternehmen E-Rechnungen schicken. "
              "Kleinere Unternehmen dürfen 2027 noch Papier und PDF verwenden."]),
            ("Ab 1. Januar 2028: Versand für alle",
             ["Dann müssen alle Unternehmen im Geschäft mit anderen Unternehmen E-Rechnungen ausstellen."]),
            ("Ausnahmen",
             ["Rechnungen an Privatkunden sind nicht betroffen. Ausgenommen sind außerdem Kleinbetragsrechnungen "
              "bis 250 € und Fahrausweise. Kleinunternehmer nach § 19 UStG müssen keine E-Rechnungen ausstellen – "
              "empfangen müssen sie sie aber auch."]),
            ("Was zählt als E-Rechnung?",
             ["Eine Rechnung in einem strukturierten Format nach der Norm EN 16931 – in Deutschland meist "
              "XRechnung oder ZUGFeRD (ab Profil BASIC). Ein normales PDF oder ein eingescanntes Papier ist keine "
              "E-Rechnung."]),
        ],
        "faq": [
            ("Was muss ich jetzt konkret tun?", "Sicherstellen, dass du E-Rechnungen lesen, prüfen und unverändert "
             "aufbewahren kannst – und spätestens für 2027 bzw. 2028 E-Rechnungen schreiben kannst."),
            ("Reicht ein PDF per E-Mail nicht mehr?", "Beim Empfang musst du E-Rechnungen schon heute annehmen. Beim "
             "Versand reicht ein PDF je nach Umsatz noch bis Ende 2026 bzw. 2027."),
            ("Ist das eine Steuerberatung?", "Nein. Dieser Überblick ersetzt keine Beratung. Sprich Details mit "
             "deinem Steuerberater ab."),
        ],
    },
    "e-rechnung-kleinunternehmer": {
        "title": "E-Rechnung für Kleinunternehmer: empfangen ja, senden nein",
        "short": "E-Rechnung für Kleinunternehmer",
        "description": "Gilt die E-Rechnungspflicht für Kleinunternehmer nach § 19 UStG? Empfangen müssen sie, "
                       "ausstellen nicht. So gehst du einfach und kostenlos damit um.",
        "h1": "E-Rechnung für Kleinunternehmer",
        "intro": "Kurz gesagt: Als Kleinunternehmer musst du E-Rechnungen empfangen können, aber keine ausstellen. "
                 "So erledigst du beides ohne Aufwand.",
        "tool": "check",
        "sections": [
            ("Empfangen: ja, seit 2025",
             ["Lieferanten dürfen dir E-Rechnungen schicken, und du musst sie annehmen und unverändert aufbewahren. "
              "Eine XRechnung öffnest du oben kostenlos im Browser."]),
            ("Ausstellen: nein, aber erlaubt",
             ["Kleinunternehmer sind von der Pflicht ausgenommen, E-Rechnungen zu schreiben. Du darfst aber, wenn "
              "dein Kunde das möchte – zum Beispiel ein größeres Unternehmen oder eine Behörde."]),
            ("Was auf deiner Rechnung stehen muss",
             ["Statt Umsatzsteuer der Hinweis, dass nach § 19 UStG keine Umsatzsteuer berechnet wird. Im "
              "Rechnungsformular genügt ein Häkchen – der Hinweis wird automatisch eingefügt."]),
        ],
        "faq": [
            ("Brauche ich dafür eine Software?", "Zum Lesen einzelner Rechnungen nicht – das geht hier kostenlos. Wer "
             "viele Rechnungen bekommt, spart mit dem Konto Zeit: Der Agent liest, prüft und archiviert sie."),
            ("Muss ich das Original aufbewahren?", "Ja. Bei einer E-Rechnung ist die Datei selbst die Rechnung und "
             "muss unverändert aufbewahrt werden."),
        ],
    },
    "e-rechnung-handwerker": {
        "title": "E-Rechnung für Handwerker – einfach empfangen, prüfen, schreiben",
        "short": "E-Rechnung für Handwerker",
        "description": "Rechnungen vom Großhandel, Papierbelege von der Baustelle, Rechnungen an Kunden und "
                       "Behörden: So erledigen Handwerksbetriebe die E-Rechnung mit wenig Aufwand.",
        "h1": "E-Rechnung im Handwerksbetrieb",
        "intro": "Material vom Großhandel, Tankbelege, Rechnungen an Kunden und manchmal an die Stadt: Im Handwerk "
                 "kommt alles zusammen. So wird daraus kein Papierkram.",
        "tool": None,
        "sections": [
            ("Eingangsrechnungen: eine Adresse für alles",
             ["Gib deinen Lieferanten eine eigene Rechnungsadresse. Jede Rechnung, die dort ankommt, wird gelesen, "
              "nachgerechnet und mit früheren Rechnungen verglichen. Doppelte Rechnungen und geänderte "
              "Bankverbindungen fallen sofort auf."]),
            ("Papierbelege von der Baustelle",
             ["Mit der App fotografierst du Quittungen und Papierrechnungen direkt vor Ort. Der Agent liest Betrag, "
              "Datum und Lieferant aus."]),
            ("Rechnungen an Kunden und öffentliche Auftraggeber",
             ["Für Geschäftskunden schreibst du ZUGFeRD-PDFs, für Behörden XRechnungen mit Leitweg-ID. Beides "
              "aus einem Formular, mit fortlaufender Nummer, direkt per E-Mail versendet."]),
            ("Für den Steuerberater",
             ["Am Monatsende ein Klick: Buchungsstapel im DATEV-Format mit allen Belegen. Dein Steuerberater kann "
              "auch einen eigenen Lesezugang bekommen."]),
        ],
        "faq": [
            ("Funktioniert das auf dem Handy?", "Ja. Die App installierst du direkt aus dem Browser, ohne App Store – "
             "mit Kamera für Papierrechnungen."),
            ("Was kostet das?", "30 Tage kostenlos testen, danach ab 9 € im Monat. Im Tarif Free bleiben 5 "
             "Eingangsrechnungen im Monat dauerhaft kostenlos."),
        ],
    },
    "steuerberater": {
        "title": "Für Steuerberater: E-Rechnungen Ihrer Mandanten als DATEV-Buchungsstapel",
        "short": "Für Steuerberater",
        "description": "Ihre Mandanten empfangen E-Rechnungen, Sie bekommen geprüfte Belege als DATEV-Buchungsstapel. "
                       "Mit kostenlosem Lesezugang für die Kanzlei. Server in Deutschland.",
        "h1": "Für Steuerberater und Kanzleien",
        "intro": "Ihre Mandanten bekommen immer mehr XRechnungen und ZUGFeRD-PDFs – und schicken Ihnen trotzdem "
                 "Schuhkartons. E-Rechnung Agent sorgt dafür, dass bei Ihnen geprüfte Belege im DATEV-Format "
                 "ankommen.",
        "tool": None,
        "sections": [
            ("Was Ihre Mandanten bekommen",
             ["Eine eigene Eingangsadresse für Rechnungen, automatisches Auslesen von XRechnung, ZUGFeRD, PDF und "
              "Fotos, Prüfung von Rechenwegen und Pflichtangaben, Erkennung von Duplikaten und geänderten "
              "Bankverbindungen – und eine revisionssichere Ablage der Originale."]),
            ("Was Sie bekommen",
             ["Einen DATEV-Buchungsstapel (EXTF) mit Kreditoren, Debitoren, Gegenkonten nach SKR03 oder SKR04 und "
              "den Belegen. Beraternummer, Mandantennummer und Konten stellt der Mandant nach Ihren Vorgaben ein."]),
            ("Ihr eigener Zugang",
             ["Mandanten laden Sie als Steuerberater ein. Sie sehen alle Belege und laden den Export herunter, "
              "ändern aber nichts. Der Zugang ist für Sie kostenlos und zählt nicht als Nutzer des Mandanten."]),
            ("Datenschutz",
             ["Verarbeitung ausschließlich auf Servern in Deutschland, Vertrag zur Auftragsverarbeitung inklusive, "
              "verschlüsselte Übertragung, getrennte Daten je Mandant."]),
        ],
        "faq": [
            ("Gibt es Konditionen für Kanzleien?", "Ja – sprechen Sie uns an, wenn Sie mehreren Mandanten "
             "E-Rechnung Agent empfehlen möchten."),
            ("Ersetzt das DATEV Unternehmen online?", "Nein, es liefert die Belege vorbereitet und geprüft an. Wie "
             "Sie sie weiterverarbeiten, entscheiden Sie."),
        ],
        "contact": True,
    },
}


@router.get("/ratgeber", response_class=HTMLResponse)
def guide_index(request: Request, user=Depends(optional_user)):
    return page(request, "ratgeber.html", user, nav="ratgeber", guides=GUIDES)


def _guide_route(slug: str):
    def view(request: Request, user=Depends(optional_user)):
        g = GUIDES[slug]
        faq_ld = json.dumps({"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in g["faq"]]},
            ensure_ascii=False).replace("</", "<\\/")
        return page(request, "guide.html", user, nav="ratgeber", g=g, slug=slug, updated=UPDATED, faq_ld=faq_ld,
                    others=[(s, GUIDES[s]["short"]) for s in GUIDES if s != slug],
                    contact=settings.operator_email)
    view.__name__ = f"guide_{slug.replace('-', '_')}"
    return view


for _slug in GUIDES:
    router.add_api_route(f"/{_slug}", _guide_route(_slug), methods=["GET"], response_class=HTMLResponse)

PUBLIC_PAGES = ["/", "/pruefen", "/erstellen", "/registrieren", "/ratgeber", "/app", "/impressum", "/datenschutz",
                "/agb", "/avv"]


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap():
    base = settings.base_url.rstrip("/")
    urls = PUBLIC_PAGES + [f"/{s}" for s in GUIDES]
    items = "".join(f"<url><loc>{base}{u}</loc><lastmod>{UPDATED.isoformat()}</lastmod></url>" for u in urls)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</urlset>'
    return Response(xml, media_type="application/xml")


@router.get("/robots.txt", include_in_schema=False, response_class=PlainTextResponse)
def robots():
    base = settings.base_url.rstrip("/")
    private = ["/eingang", "/rechnung/", "/rechnungen/", "/ausgang", "/export", "/einstellungen", "/team", "/tarif",
               "/konto", "/eingangsadresse", "/einladung", "/passwort-neu", "/api/", "/stripe/", "/teilen"]
    return "User-agent: *\n" + "".join(f"Disallow: {p}\n" for p in private) + f"\nSitemap: {base}/sitemap.xml\n"


def guide_or_404(slug: str) -> dict:
    if slug not in GUIDES:
        raise HTTPException(404)
    return GUIDES[slug]

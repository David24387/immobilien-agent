from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "data" / "state.json"
CONFIG_PATH = ROOT / "config.yml"
USER_AGENT = "WeilburgImmobilienAgent/1.0 (private property search; contact: configured repository owner)"
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9,en;q=0.7"}

PRICE_RE = re.compile(r"(?<!\d)(\d{2,3}(?:[.\s]\d{3})+|\d{4,7})\s*(?:€|EUR)", re.I)
SQM_RE = re.compile(r"(\d{1,3}(?:[.\s]\d{3})+|\d{3,6})\s*(?:m²|m2|qm)", re.I)


@dataclass
class Listing:
    uid: str
    url: str
    title: str
    snippet: str
    source: str
    price_eur: int | None
    plot_sqm: int | None
    living_sqm: int | None
    location: str | None
    image_url: str | None
    score: int
    assessment: str
    flags: list[str]
    first_seen: str
    last_seen: str


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_config() -> dict[str, Any]:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"listings": {}, "last_weekly": None}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"listings": {}, "last_weekly": None}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def clean_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
    query = [(k, v) for k, v in query if not k.lower().startswith(("utm_", "gclid", "fbclid"))]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), urllib.parse.urlencode(query), ""))


def uid_for(url: str) -> str:
    return hashlib.sha256(clean_url(url).encode("utf-8")).hexdigest()[:20]


def int_de(value: str) -> int:
    return int(re.sub(r"\D", "", value))


def extract_price(text: str) -> int | None:
    vals = [int_de(m.group(1)) for m in PRICE_RE.finditer(text)]
    vals = [v for v in vals if 20_000 <= v <= 20_000_000]
    return vals[0] if vals else None


def extract_sqm_values(text: str) -> list[int]:
    vals = [int_de(m.group(1)) for m in SQM_RE.finditer(text)]
    return [v for v in vals if 20 <= v <= 2_000_000]


def bing_rss(query: str, limit: int) -> list[dict[str, str]]:
    url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "format": "rss", "setlang": "de-DE"})
    response = requests.get(url, headers=HEADERS, timeout=25)
    response.raise_for_status()
    root = ET.fromstring(response.text)
    items: list[dict[str, str]] = []
    for item in root.findall(".//item")[:limit]:
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        description = item.findtext("description") or ""
        items.append({"title": html.unescape(title), "url": link, "snippet": html.unescape(description)})
    return items


def build_queries(config: dict[str, Any]) -> list[str]:
    search = config["search"]
    keyword_group = " OR ".join(f'"{k}"' for k in search["keywords"])
    place_batches = [search["places"][i:i + 8] for i in range(0, len(search["places"]), 8)]
    queries: list[str] = []
    for domain in search["domains"]:
        for batch in place_batches:
            places = " OR ".join(f'"{p}"' for p in batch)
            queries.append(f"site:{domain} ({keyword_group}) ({places}) kaufen")
    return queries


def jsonld_objects(soup: BeautifulSoup) -> Iterable[dict[str, Any]]:
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.get_text(strip=True))
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            obj = stack.pop()
            if isinstance(obj, dict):
                yield obj
                graph = obj.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)


def fetch_metadata(url: str) -> dict[str, Any]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, allow_redirects=True)
        if r.status_code >= 400 or "text/html" not in r.headers.get("content-type", ""):
            return {}
        soup = BeautifulSoup(r.text[:2_000_000], "html.parser")
        title = (soup.title.get_text(" ", strip=True) if soup.title else "")
        description_node = soup.select_one('meta[name="description"], meta[property="og:description"]')
        description = description_node.get("content", "") if description_node else ""
        image_node = soup.select_one('meta[property="og:image"]')
        image = image_node.get("content") if image_node else None
        price = None
        location = None
        living = None
        plot = None
        for obj in jsonld_objects(soup):
            offers = obj.get("offers")
            if isinstance(offers, dict) and price is None:
                raw_price = offers.get("price") or offers.get("lowPrice")
                try:
                    price = int(float(str(raw_price).replace(".", "").replace(",", ".")))
                except (ValueError, TypeError):
                    pass
            addr = obj.get("address")
            if isinstance(addr, dict) and location is None:
                location = " ".join(str(addr.get(k, "")) for k in ("postalCode", "addressLocality")).strip() or None
            floor = obj.get("floorSize")
            if isinstance(floor, dict):
                try:
                    living = int(float(floor.get("value")))
                except (ValueError, TypeError):
                    pass
        page_text = " ".join([title, description, soup.get_text(" ", strip=True)[:60_000]])
        if price is None:
            price = extract_price(page_text)
        sqm = extract_sqm_values(page_text)
        if sqm:
            # Große Werte sind meist Grundstück, kleinere eher Wohnfläche.
            plot_candidates = [v for v in sqm if v >= 500]
            living_candidates = [v for v in sqm if 30 <= v < 1000]
            plot = max(plot_candidates) if plot_candidates else None
            living = living or (living_candidates[0] if living_candidates else None)
        return {
            "url": clean_url(r.url), "title": title, "description": description,
            "image_url": image, "price_eur": price, "plot_sqm": plot,
            "living_sqm": living, "location": location,
        }
    except requests.RequestException:
        return {}


def score_listing(title: str, snippet: str, price: int | None, plot: int | None, config: dict[str, Any]) -> tuple[int, list[str]]:
    text = f"{title} {snippet}".lower()
    flags: list[str] = []
    score = 15
    strong = ["mühle", "wassermühle", "resthof", "hofreite", "alleinlage", "aussiedlerhof", "gutshof"]
    medium = ["bauernhof", "gehöft", "landgut", "anwesen", "scheune", "pferdehof", "vierseithof"]
    for word in strong:
        if word in text:
            score += 16
            flags.append(word.title())
    for word in medium:
        if word in text:
            score += 8
            flags.append(word.title())
    if plot:
        if plot >= 10_000:
            score += 24
            flags.append("Grundstück ≥ 1 ha")
        elif plot >= 5_000:
            score += 18
            flags.append("Grundstück ≥ 5.000 m²")
        elif plot >= config["search"]["min_plot_sqm"]:
            score += 12
            flags.append("Großes Grundstück")
    else:
        score -= 5
    negative_words = ["zwangsversteigerung", "erbbaurecht", "abriss", "nur gewerbe", "keine wohnnutzung"]
    for word in negative_words:
        if word in text:
            score -= 12
            flags.append(f"Prüfen: {word}")
    max_price = config["search"].get("max_price_eur")
    if max_price and price and price > max_price:
        score -= 30
        flags.append("Über Budget")
    return max(0, min(100, score)), list(dict.fromkeys(flags))


def heuristic_assessment(listing: dict[str, Any]) -> str:
    positives: list[str] = []
    checks: list[str] = []
    if listing.get("plot_sqm"):
        positives.append(f"Grundstück ca. {listing['plot_sqm']:,} m²".replace(",", "."))
    for flag in listing.get("flags", []):
        if not flag.startswith("Prüfen:") and "Grundstück" not in flag:
            positives.append(flag)
    text = f"{listing.get('title','')} {listing.get('snippet','')}".lower()
    for term, label in [
        ("denkmalschutz", "Denkmalschutz und Auflagen prüfen"),
        ("sanierungsbedürftig", "Sanierungsumfang und Reserven prüfen"),
        ("hochwasser", "Hochwasserlage genau prüfen"),
        ("ölheizung", "Heizung und Austauschpflicht prüfen"),
        ("pacht", "Pacht- oder Nutzungsverhältnisse prüfen"),
        ("teilvermietet", "Mietverhältnisse prüfen"),
    ]:
        if term in text:
            checks.append(label)
    checks.extend(["Baurecht/Nutzungsänderung der Nebengebäude prüfen", "Hochwasser- und Altlastenkarten vor Besichtigung prüfen"])
    pos = ", ".join(dict.fromkeys(positives[:4])) or "Spezialimmobilie im Suchgebiet"
    return f"Pluspunkte: {pos}. Vorab prüfen: {'; '.join(dict.fromkeys(checks[:4]))}."


def optional_ai_assessment(data: dict[str, Any]) -> str | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    prompt = (
        "Bewerte dieses Immobilieninserat knapp für einen Käufer, der im 40-km-Radius um Weilburg "
        "ehemalige Mühlen, Höfe und besondere Wohnanwesen mit großem Grundstück sucht. "
        "Nenne 2-4 Pluspunkte und 2-4 konkrete Prüfpunkte. Keine erfundenen Fakten. Maximal 90 Wörter.\n\n"
        + json.dumps(data, ensure_ascii=False)
    )
    try:
        r = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": os.getenv("OPENAI_MODEL", "gpt-5-mini"), "input": prompt}, timeout=45,
        )
        r.raise_for_status()
        payload = r.json()
        text = payload.get("output_text")
        if text:
            return text.strip()
        for item in payload.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    return content["text"].strip()
    except requests.RequestException:
        return None
    return None


def collect(config: dict[str, Any]) -> list[Listing]:
    candidates: dict[str, dict[str, str]] = {}
    queries = build_queries(config)
    for index, query in enumerate(queries, start=1):
        try:
            for item in bing_rss(query, config["search"]["result_limit_per_query"]):
                url = clean_url(item["url"])
                if not url.startswith("http"):
                    continue
                domain_ok = any(d in urllib.parse.urlsplit(url).netloc for d in config["search"]["domains"])
                if domain_ok:
                    candidates.setdefault(url, item)
        except (requests.RequestException, ET.ParseError):
            pass
        # Moderate Abfragefrequenz.
        if index % 10 == 0:
            time.sleep(1)

    output: list[Listing] = []
    timestamp = now_iso()
    for url, item in candidates.items():
        metadata = fetch_metadata(url) if config["features"].get("fetch_listing_pages") else {}
        final_url = metadata.get("url") or url
        title = metadata.get("title") or item["title"]
        snippet = metadata.get("description") or item["snippet"]
        combined = f"{title} {snippet}"
        price = metadata.get("price_eur") or extract_price(combined)
        sqm = extract_sqm_values(combined)
        plot = metadata.get("plot_sqm") or (max([x for x in sqm if x >= 500], default=None))
        living = metadata.get("living_sqm")
        score, flags = score_listing(title, snippet, price, plot, config)
        if plot is not None and plot < config["search"]["min_plot_sqm"]:
            continue
        # Ohne auslesbare Grundstücksgröße bleiben stark passende Spezialobjekte dabei.
        if plot is None and score < 40:
            continue
        draft = {
            "title": title, "snippet": snippet, "price_eur": price, "plot_sqm": plot,
            "living_sqm": living, "location": metadata.get("location"), "flags": flags,
            "url": final_url,
        }
        assessment = optional_ai_assessment(draft) if config["features"].get("optional_ai_analysis") else None
        assessment = assessment or heuristic_assessment(draft)
        output.append(Listing(
            uid=uid_for(final_url), url=final_url, title=title[:300], snippet=snippet[:1000],
            source=urllib.parse.urlsplit(final_url).netloc.removeprefix("www."), price_eur=price,
            plot_sqm=plot, living_sqm=living, location=metadata.get("location"),
            image_url=metadata.get("image_url"), score=score, assessment=assessment,
            flags=flags, first_seen=timestamp, last_seen=timestamp,
        ))
    return sorted(output, key=lambda x: x.score, reverse=True)


def eur(value: int | None) -> str:
    return "Preis auf Anfrage" if value is None else f"{value:,.0f} €".replace(",", ".")


def sqm(value: int | None) -> str:
    return "nicht ausgelesen" if value is None else f"{value:,.0f} m²".replace(",", ".")


def card(item: dict[str, Any], price_change: tuple[int | None, int | None] | None = None) -> str:
    image = f'<img src="{html.escape(item["image_url"])}" alt="" style="width:100%;max-height:260px;object-fit:cover;border-radius:10px 10px 0 0">' if item.get("image_url") else ""
    change = ""
    if price_change:
        old, new = price_change
        change = f'<p style="padding:8px 12px;background:#fff3cd"><strong>Preisänderung:</strong> {eur(old)} → {eur(new)}</p>'
    return f"""
    <div style="border:1px solid #ddd;border-radius:10px;margin:18px 0;overflow:hidden;background:#fff">
      {image}
      <div style="padding:16px">
        <div style="font-size:13px;color:#666">Score {item.get('score',0)}/100 · {html.escape(item.get('source',''))}</div>
        <h2 style="font-size:20px;margin:7px 0"><a href="{html.escape(item['url'])}">{html.escape(item['title'])}</a></h2>
        <p><strong>{eur(item.get('price_eur'))}</strong> · Grundstück: {sqm(item.get('plot_sqm'))} · Wohnfläche: {sqm(item.get('living_sqm'))}</p>
        {change}
        <p>{html.escape(item.get('assessment',''))}</p>
        <p style="font-size:13px;color:#666">{html.escape(', '.join(item.get('flags', [])))}</p>
      </div>
    </div>"""


def send_mail(subject: str, intro: str, items: list[dict[str, Any]], changes: dict[str, tuple[int | None, int | None]] | None = None) -> None:
    if not items and not changes:
        return
    user = os.environ["SMTP_USERNAME"]
    password = os.environ["SMTP_PASSWORD"]
    recipient = os.getenv("MAIL_RECIPIENT", "david.t.mueller@gmx.de")
    smtp_host = os.getenv("SMTP_HOST", "mail.gmx.net")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    changes = changes or {}
    body = "".join(card(item, changes.get(item["uid"])) for item in items)
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(intro + "\n\n" + "\n".join(f"- {x['title']}: {x['url']}" for x in items))
    msg.add_alternative(f"""
    <html><body style="font-family:Arial,sans-serif;background:#f5f5f5;padding:24px">
      <div style="max-width:760px;margin:auto">
        <h1 style="font-size:26px">{html.escape(subject)}</h1>
        <p>{html.escape(intro)}</p>{body}
        <p style="font-size:12px;color:#777">Automatisch erstellt. Angaben bitte immer im Originalinserat prüfen.</p>
      </div>
    </body></html>""", subtype="html")
    context = ssl.create_default_context()
    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
        smtp.starttls(context=context)
        smtp.login(user, password)
        smtp.send_message(msg)


def merge_state(state: dict[str, Any], found: list[Listing]) -> tuple[list[dict[str, Any]], dict[str, tuple[int | None, int | None]]]:
    new_items: list[dict[str, Any]] = []
    price_changes: dict[str, tuple[int | None, int | None]] = {}
    store = state.setdefault("listings", {})
    for listing in found:
        data = asdict(listing)
        old = store.get(listing.uid)
        if old is None:
            store[listing.uid] = data
            new_items.append(data)
            continue
        old_price = old.get("price_eur")
        new_price = listing.price_eur
        if old_price and new_price and old_price != new_price:
            price_changes[listing.uid] = (old_price, new_price)
            history = old.setdefault("price_history", [])
            history.append({"at": now_iso(), "old": old_price, "new": new_price})
        first_seen = old.get("first_seen", listing.first_seen)
        history = old.get("price_history", [])
        store[listing.uid] = {**data, "first_seen": first_seen, "price_history": history}
    return new_items, price_changes


def run(mode: str, dry_run: bool) -> None:
    config = load_config()
    state = load_state()
    found = collect(config)
    new_items, changes = merge_state(state, found)
    stored = state["listings"]

    if mode == "daily":
        top_cfg = config["notifications"]["top_match"]
        top = [x for x in new_items if x["score"] >= top_cfg["min_score"] and (x.get("plot_sqm") or 0) >= top_cfg["min_plot_sqm"]]
        if top_cfg.get("enabled") and top and not dry_run:
            send_mail(f"🏡 {len(top)} neuer Top-Treffer rund um Weilburg", "Diese neuen Objekte passen besonders gut zu deiner Suche:", top)
    elif mode == "weekly":
        last_weekly = state.get("last_weekly")
        if last_weekly:
            weekly_new = [x for x in stored.values() if x.get("first_seen", "") > last_weekly]
        else:
            weekly_new = list(stored.values())
        changed_items = [stored[uid] for uid in changes if uid in stored]
        all_items = sorted({x["uid"]: x for x in weekly_new + changed_items}.values(), key=lambda x: x.get("score", 0), reverse=True)
        if not dry_run:
            send_mail(
                f"🏡 Immobilien-Wochenreport: {len(weekly_new)} neu, {len(changes)} Preisänderungen",
                "Neue besondere Wohnhäuser, Höfe, Mühlen und Anwesen im ungefähr 40-km-Radius um Weilburg:",
                all_items, changes,
            )
        state["last_weekly"] = now_iso()
    else:
        raise ValueError(f"Unbekannter Modus: {mode}")

    save_state(state)
    print(json.dumps({"found": len(found), "new": len(new_items), "price_changes": len(changes), "mode": mode, "dry_run": dry_run}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["daily", "weekly"], required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(args.mode, args.dry_run)

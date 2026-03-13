"""
VIGIE ARCHITECTURE — v3.0 RSS
Lecture des flux RSS officiels — jamais bloqué
Sources : SEAO, BidsAndTenders, MERX, NBON
"""

import os
import json
import time
import hashlib
import logging
import smtplib
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

import anthropic

# ─── Configuration ────────────────────────────────────────────────────────────

CONFIG = {
    "budget_threshold_hors_qc": 10_000_000,
    "categories_cibles": [
        "Architecture", "Architecte", "Design", "Aménagement",
        "Urbanisme", "Paysager", "Intérieur", "Conception",
        "Architectural", "Interior", "Landscape",
    ],
    "lookback_hours": 25,
    "recipients": ["pasgob@abcparchitecture.com"],
    "smtp_host": os.getenv("SMTP_HOST", "smtp.office365.com"),
    "smtp_port": int(os.getenv("SMTP_PORT", 587)),
    "smtp_user": os.getenv("SMTP_USER", ""),
    "smtp_pass": os.getenv("SMTP_PASS", ""),
    "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
    "claude_model": "claude-haiku-4-5-20251001",  # Rapide + économique pour le filtrage
    "db_path": "vigie_dedup.db",
}

# Flux RSS officiels — jamais bloqués
RSS_FEEDS = [
    {
        "name": "SEAO",
        "url": "https://www.seao.ca/OpportunityPublication/RssFeeds/Index?typePublication=AO&lang=fr",
        "province": "QC",
    },
    {
        "name": "SEAO-AQ",  # Appels de qualification
        "url": "https://www.seao.ca/OpportunityPublication/RssFeeds/Index?typePublication=AQ&lang=fr",
        "province": "QC",
    },
    {
        "name": "MERX",
        "url": "https://www.merx.com/rss/opportunities?category=architecture",
        "province": None,  # Multi-province
    },
    {
        "name": "BidsAndTenders",
        "url": "https://www.bidsandtenders.ca/rss/opportunities?keywords=architecture",
        "province": None,
    },
    {
        "name": "NBON",
        "url": "https://nbon.gnb.ca/rss/opportunities?lang=fr",
        "province": "NB",
    },
    {
        "name": "NSTenders",
        "url": "https://novascotia.ca/tenders/rss.aspx",
        "province": "NS",
    },
]

PROVINCES_QC = {"QC"}
PROVINCES_HORS_QC = {"NB", "NS", "PE", "ON", "BC", "AB", "MB", "SK", "NL"}

# ─── Déduplication SQLite ─────────────────────────────────────────────────────

class DeduplicationStore:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS seen (
                fingerprint TEXT PRIMARY KEY,
                source TEXT,
                title TEXT,
                seen_at TEXT
            )
        """)
        self.conn.execute(
            "DELETE FROM seen WHERE seen_at < datetime('now', '-90 days')"
        )
        self.conn.commit()

    def is_new(self, fingerprint):
        return self.conn.execute(
            "SELECT 1 FROM seen WHERE fingerprint=?", (fingerprint,)
        ).fetchone() is None

    def mark(self, fingerprint, source, title):
        self.conn.execute(
            "INSERT OR IGNORE INTO seen VALUES (?,?,?,?)",
            (fingerprint, source, title[:100], datetime.now().isoformat()),
        )
        self.conn.commit()

# ─── Lecture RSS ──────────────────────────────────────────────────────────────

def fetch_rss(feed: dict, since: datetime) -> list[dict]:
    """Lit un flux RSS et retourne les items récents."""
    items = []
    headers = {
        "User-Agent": "VigieArchitecture/3.0 (ABCP Architecture; pasgob@abcparchitecture.com)",
        "Accept": "application/rss+xml, application/xml, text/xml",
    }
    try:
        req = Request(feed["url"], headers=headers)
        with urlopen(req, timeout=20) as resp:
            raw = resp.read()

        root = ET.fromstring(raw)
        ns = {"atom": "http://www.w3.org/2005/Atom"}

        # Support RSS 2.0 et Atom
        channel = root.find("channel")
        entries = channel.findall("item") if channel else root.findall("atom:entry", ns)

        for entry in entries:
            def g(tag):
                el = entry.find(tag) or entry.find(f"atom:{tag}", ns)
                return el.text.strip() if el is not None and el.text else ""

            title   = g("title")
            link    = g("link") or g("id")
            summary = g("description") or g("summary") or g("content")
            pub_raw = g("pubDate") or g("published") or g("updated")

            # Parse date
            pub_date = None
            for fmt in ["%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z",
                        "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"]:
                try:
                    pub_date = datetime.strptime(pub_raw[:25], fmt[:len(pub_raw[:25])])
                    pub_date = pub_date.replace(tzinfo=None)
                    break
                except Exception:
                    continue

            # Filtre temporel
            if pub_date and pub_date < since:
                continue

            fp = hashlib.sha256(f"{feed['name']}_{link}_{title[:40]}".encode()).hexdigest()[:16]

            items.append({
                "fingerprint": fp,
                "title":       title,
                "url":         link,
                "summary":     summary[:500],
                "pub_date":    pub_date,
                "source":      feed["name"],
                "province":    feed.get("province"),
            })

        logging.info(f"[{feed['name']}] {len(items)} item(s) récent(s)")

    except URLError as e:
        logging.warning(f"[{feed['name']}] URL inaccessible : {e}")
    except ET.ParseError as e:
        logging.warning(f"[{feed['name']}] XML invalide : {e}")
    except Exception as e:
        logging.error(f"[{feed['name']}] Erreur : {e}")

    return items

# ─── Filtrage par mots-clés ───────────────────────────────────────────────────

def is_architecture_related(title: str, summary: str) -> bool:
    """Filtre lexical rapide — évite les appels Claude inutiles."""
    combined = f"{title} {summary}".lower()
    return any(kw.lower() in combined for kw in CONFIG["categories_cibles"])

# ─── Analyse Claude ───────────────────────────────────────────────────────────

def analyse_with_claude(item: dict) -> Optional[dict]:
    """
    Claude extrait budget, province, date de clôture depuis le titre + résumé.
    Utilisé seulement si l'item passe le filtre lexical.
    """
    client = anthropic.Anthropic(api_key=CONFIG["anthropic_api_key"])

    prompt = f"""Appel d'offres canadien — extrais les données suivantes en JSON strict.

TITRE : {item['title']}
SOURCE : {item['source']}
RÉSUMÉ : {item['summary'][:800]}

Retourne UNIQUEMENT ce JSON (null si inconnu) :
{{
  "province": "Code 2 lettres (QC/NB/NS/PE/ON/BC/AB/MB/SK/NL) ou null",
  "estimated_budget": null ou nombre entier en dollars,
  "closing_date": "YYYY-MM-DD ou null",
  "owner": "Organisation cliente",
  "category": "Architecture / Urbanisme / Design / Génie / Autre"
}}"""

    try:
        resp = client.messages.create(
            model=CONFIG["claude_model"],
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return json.loads(resp.content[0].text.strip())
    except Exception as e:
        logging.warning(f"[Claude] Échec analyse : {e}")
        return {}

# ─── Rapport HTML + Courriel ──────────────────────────────────────────────────

def build_html(projects: list) -> str:
    STYLES = {
        "URGENT":      ("#dc2626", "#fef2f2"),
        "PRIORITAIRE": ("#ea580c", "#fff7ed"),
        "NORMAL":      ("#16a34a", "#f0fdf4"),
    }

    def urgency(closing_str):
        if not closing_str:
            return "NORMAL"
        try:
            days = (datetime.strptime(closing_str, "%Y-%m-%d") - datetime.now()).days
            if days <= 7:  return "URGENT"
            if days <= 14: return "PRIORITAIRE"
        except Exception:
            pass
        return "NORMAL"

    def days_left(closing_str):
        try:
            return max(0, (datetime.strptime(closing_str, "%Y-%m-%d") - datetime.now()).days)
        except Exception:
            return "?"

    groups = {"URGENT": [], "PRIORITAIRE": [], "NORMAL": []}
    for p in projects:
        groups[urgency(p.get("closing_date"))].append(p)

    sections = ""
    for level, group in groups.items():
        if not group:
            continue
        color, bg = STYLES[level]
        rows = ""
        for p in sorted(group, key=lambda x: x.get("closing_date") or "9999"):
            budget = f"${p['estimated_budget']:,.0f}" if p.get("estimated_budget") else "N/D"
            closing = p.get("closing_date") or "N/D"
            rows += f"""<tr>
              <td style="padding:8px;border:1px solid #e5e7eb">
                <a href="{p['url']}" style="color:#1d4ed8;font-weight:600">{p['title']}</a>
                <br><small style="color:#6b7280">{p.get('summary','')[:120]}</small>
              </td>
              <td style="padding:8px;border:1px solid #e5e7eb">{p.get('owner','N/D')}</td>
              <td style="padding:8px;border:1px solid #e5e7eb;text-align:center">
                <b>{p.get('province','?')}</b>
              </td>
              <td style="padding:8px;border:1px solid #e5e7eb;text-align:right">{budget}</td>
              <td style="padding:8px;border:1px solid #e5e7eb;text-align:center">
                {closing}<br><small>({days_left(closing)} j)</small>
              </td>
              <td style="padding:8px;border:1px solid #e5e7eb;text-align:center">
                {p['source']}
              </td>
            </tr>"""

        sections += f"""
        <h3 style="color:{color};border-left:4px solid {color};padding-left:10px">
          {level} — {len(group)} appel(s) d'offres
        </h3>
        <table style="width:100%;border-collapse:collapse;font-size:13px">
          <thead style="background:{bg}">
            <tr>
              <th style="padding:8px;border:1px solid #e5e7eb;text-align:left">Projet</th>
              <th style="padding:8px;border:1px solid #e5e7eb">Client</th>
              <th style="padding:8px;border:1px solid #e5e7eb">Prov.</th>
              <th style="padding:8px;border:1px solid #e5e7eb">Budget</th>
              <th style="padding:8px;border:1px solid #e5e7eb">Clôture</th>
              <th style="padding:8px;border:1px solid #e5e7eb">Source</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>"""

    return f"""
    <html><body style="font-family:Arial,sans-serif;max-width:900px;margin:auto;padding:24px">
      <h2 style="color:#1e3a5f;border-bottom:3px solid #1d4ed8;padding-bottom:10px">
        🏛 Vigie Architecture — {datetime.now().strftime('%Y-%m-%d %H:%M')}
      </h2>
      <p style="color:#374151">
        <b>{len(projects)}</b> nouvel(aux) appel(s) d'offres détecté(s)<br>
        <small style="color:#9ca3af">Sources : SEAO · BidsAndTenders · MERX · NBON · NS Tenders</small>
      </p>
      {sections}
      <hr style="margin-top:32px">
      <p style="font-size:11px;color:#9ca3af">
        Vigie automatique ABCP Architecture · Seuil hors-QC : ≥ 10 M$ · QC : tous projets
      </p>
    </body></html>"""

def send_email(projects: list):
    if not projects:
        logging.info("Aucun projet — courriel non envoyé.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = (
        f"[Vigie AO] {len(projects)} appel(s) Architecture — "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    msg["From"] = CONFIG["smtp_user"]
    msg["To"]   = ", ".join(CONFIG["recipients"])
    msg.attach(MIMEText(build_html(projects), "html", "utf-8"))

    try:
        with smtplib.SMTP(CONFIG["smtp_host"], CONFIG["smtp_port"]) as s:
            s.ehlo()
            s.starttls()
            s.login(CONFIG["smtp_user"], CONFIG["smtp_pass"])
            s.sendmail(CONFIG["smtp_user"], CONFIG["recipients"], msg.as_string())
        logging.info(f"✅ Courriel envoyé — {len(projects)} projet(s)")
    except Exception as e:
        logging.error(f"❌ SMTP : {e}")

# ─── Orchestrateur ────────────────────────────────────────────────────────────

def run():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler("vigie.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

    since = datetime.now() - timedelta(hours=CONFIG["lookback_hours"])
    logging.info(f"Démarrage — depuis {since.strftime('%Y-%m-%d %H:%M')}")

    dedup    = DeduplicationStore(CONFIG["db_path"])
    projects = []

    for feed in RSS_FEEDS:
        items = fetch_rss(feed, since)

        for item in items:

            # 1. Déduplication
            if not dedup.is_new(item["fingerprint"]):
                continue

            # 2. Filtre lexical rapide (sans Claude)
            if not is_architecture_related(item["title"], item["summary"]):
                logging.debug(f"Non pertinent : {item['title'][:60]}")
                continue

            # 3. Analyse Claude (budget, province, date clôture)
            analysis = analyse_with_claude(item)
            time.sleep(0.3)  # Throttle API

            # 4. Province
            province = (
                analysis.get("province")
                or item.get("province")
                or "?"
            )

            # 5. Filtre budget hors-QC
            budget = analysis.get("estimated_budget")
            if province not in PROVINCES_QC and province != "?":
                if budget and budget < CONFIG["budget_threshold_hors_qc"]:
                    logging.debug(f"Hors seuil ({province} {budget:,.0f}$) : {item['title'][:50]}")
                    continue

            # 6. Projet retenu
            project = {
                **item,
                "province":         province,
                "estimated_budget": budget,
                "closing_date":     analysis.get("closing_date"),
                "owner":            analysis.get("owner", "N/D"),
                "category":         analysis.get("category", "Architecture"),
            }
            projects.append(project)
            dedup.mark(item["fingerprint"], item["source"], item["title"])
            logging.info(f"✓ [{item['source']}] {item['title'][:70]}")

    logging.info(f"Total retenus : {len(projects)}")

    # 7. Export JSON pour dashboard GitHub Pages
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump({
            "updated_at": datetime.now().isoformat(),
            "count": len(projects),
            "projects": [
                {k: v for k, v in p.items() if k != "pub_date"}
                for p in projects
            ],
        }, f, ensure_ascii=False, indent=2)
    logging.info("data.json exporté")

    # 8. Courriel
    send_email(projects)
    logging.info("Cycle terminé.")

if __name__ == "__main__":
    run()

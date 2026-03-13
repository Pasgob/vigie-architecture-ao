"""
VIGIE ARCHITECTURE — v4.0
Source principale : API ouverte SEAO via donneesquebec.ca (jamais bloquée)
Source secondaire : buyandsell.gc.ca (marchés fédéraux canadiens)
"""

import os
import json
import time
import hashlib
import logging
import smtplib
import sqlite3
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
        "Architectural", "Interior", "Landscape", "Planning",
    ],
    "lookback_days": 2,  # Chercher les AOs des 2 derniers jours
    "recipients": ["pasgob@abcparchitecture.com"],
    "smtp_host": os.getenv("SMTP_HOST", "smtp.office365.com"),
    "smtp_port": int(os.getenv("SMTP_PORT", 587)),
    "smtp_user": os.getenv("SMTP_USER", ""),
    "smtp_pass": os.getenv("SMTP_PASS", ""),
    "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
    "claude_model": "claude-haiku-4-5-20251001",
    "db_path": "vigie_dedup.db",
}

# ─── API SEAO open data (donneesquebec.ca) ────────────────────────────────────
# Dataset ID officiel SEAO sur Données Québec
SEAO_DATASET_ID = "systeme-electronique-dappel-doffres-seao"
CKAN_API = "https://www.donneesquebec.ca/recherche/api/3/action"

# ─── API buyandsell.gc.ca — marchés fédéraux canadiens ───────────────────────
BUYANDSELL_API = "https://buyandsell.gc.ca/procurement-data/atom/procurement-notices/architecture"

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
        self.conn.execute("DELETE FROM seen WHERE seen_at < datetime('now', '-90 days')")
        self.conn.commit()

    def is_new(self, fp):
        return self.conn.execute("SELECT 1 FROM seen WHERE fingerprint=?", (fp,)).fetchone() is None

    def mark(self, fp, source, title):
        self.conn.execute("INSERT OR IGNORE INTO seen VALUES (?,?,?,?)",
            (fp, source, title[:100], datetime.now().isoformat()))
        self.conn.commit()

# ─── Utilitaires HTTP ─────────────────────────────────────────────────────────

def http_get(url, timeout=30):
    import gzip
    req = Request(url, headers={
        "User-Agent": "VigieArchitecture/4.0 (ABCP Architecture; pasgob@abcparchitecture.com)",
        "Accept": "application/json, application/xml, text/xml, */*",
        "Accept-Encoding": "gzip, deflate",
    })
    with urlopen(req, timeout=timeout) as r:
        raw = r.read()
        encoding = r.headers.get("Content-Encoding", "")
        if encoding == "gzip":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8")

# ─── Source 1 : SEAO via API ouverte donneesquebec.ca ────────────────────────

def get_seao_latest_json_url():
    """
    Utilise l'API CKAN de donneesquebec.ca pour trouver l'URL du fichier
    hebdomadaire le plus récent des avis SEAO.
    """
    try:
        api_url = f"{CKAN_API}/package_show?id={SEAO_DATASET_ID}"
        raw = http_get(api_url)
        data = json.loads(raw)
        resources = data.get("result", {}).get("resources", [])

        # Chercher le fichier "avis_en_cours" ou le JSON hebdo le plus récent
        avis_en_cours = None
        latest_hebdo = None
        latest_date = None

        for r in resources:
            name = r.get("name", "").lower()
            fmt = r.get("format", "").upper()
            url = r.get("url", "")

            if not url or fmt not in ("JSON", ""):
                continue

            # Fichier avis en cours = priorité absolue
            if "avis_en_cours" in name or "en_cours" in name:
                avis_en_cours = url
                break

            # Sinon prendre le fichier hebdomadaire le plus récent
            if "hebdo_" in name:
                # Format : hebdo_YYYYMMDD_YYYYMMDD.json
                parts = name.replace("hebdo_", "").replace(".json", "").split("_")
                if len(parts) >= 1:
                    try:
                        d = datetime.strptime(parts[0], "%Y%m%d")
                        if latest_date is None or d > latest_date:
                            latest_date = d
                            latest_hebdo = url
                    except Exception:
                        pass

        result = avis_en_cours or latest_hebdo
        if result:
            logging.info(f"[SEAO] URL trouvée via API CKAN : {result}")
        return result

    except Exception as e:
        logging.error(f"[SEAO] Erreur API CKAN : {e}")
        return None

def fetch_seao(since: datetime) -> list[dict]:
    """Télécharge et filtre le JSON SEAO open data."""
    projects = []
    url = get_seao_latest_json_url()
    if not url:
        logging.warning("[SEAO] Impossible de trouver l'URL du fichier JSON.")
        return []

    try:
        raw = http_get(url, timeout=60)
        data = json.loads(raw)

        # Format OCDS (Open Contracting Data Standard)
        # Structure : {"releases": [...]} ou liste directe
        releases = data if isinstance(data, list) else data.get("releases", [])
        logging.info(f"[SEAO] {len(releases)} entrées dans le fichier")

        for release in releases:
            try:
                # Extraction des champs OCDS
                tender = release.get("tender", {})
                title = tender.get("title") or release.get("title") or ""
                ref_id = release.get("ocid") or tender.get("id") or ""
                owner = (release.get("buyer", {}) or {}).get("name", "N/D")
                status = tender.get("status", "")

                # Ignorer les AOs terminés
                if status in ("cancelled", "complete", "unsuccessful"):
                    continue

                # Date de clôture
                closing_raw = tender.get("tenderPeriod", {}).get("endDate", "")
                try:
                    closing_date = datetime.strptime(closing_raw[:10], "%Y-%m-%d")
                    if closing_date < datetime.now():
                        continue  # AO expiré
                except Exception:
                    closing_date = datetime.now() + timedelta(days=30)

                # Date de publication — filtre temporel
                pub_raw = release.get("date", "")
                try:
                    pub_date = datetime.strptime(pub_raw[:10], "%Y-%m-%d")
                    if pub_date < since:
                        continue
                except Exception:
                    pass  # Si pas de date, inclure

                # Description / catégorie
                description = tender.get("description", "")
                category = ""
                items = tender.get("items", [])
                if items:
                    category = items[0].get("classification", {}).get("description", "")

                # Lien vers SEAO
                ao_url = f"https://www.seao.ca/OpportunityPublication/AO/Details/{ref_id.split('-')[-1]}"

                fp = hashlib.sha256(f"SEAO_{ref_id}_{title[:40]}".encode()).hexdigest()[:16]

                projects.append({
                    "fingerprint": fp,
                    "title":       title,
                    "url":         ao_url,
                    "summary":     description[:400],
                    "source":      "SEAO",
                    "province":    "QC",
                    "owner":       owner,
                    "category":    category,
                    "closing_date": closing_date.strftime("%Y-%m-%d"),
                    "estimated_budget": None,
                })

            except Exception as e:
                logging.debug(f"[SEAO] Entrée ignorée : {e}")

        logging.info(f"[SEAO] {len(projects)} AOs récents trouvés")

    except Exception as e:
        logging.error(f"[SEAO] Erreur téléchargement/parsing : {e}", exc_info=True)

    return projects

# ─── Source 2 : buyandsell.gc.ca (Atom/RSS officiel fédéral) ─────────────────

def fetch_buyandsell(since: datetime) -> list[dict]:
    """Marchés fédéraux canadiens — flux Atom officiel du gouvernement du Canada."""
    import xml.etree.ElementTree as ET
    projects = []

    urls = [
        "https://buyandsell.gc.ca/procurement-data/atom/procurement-notices/architecture",
        "https://buyandsell.gc.ca/procurement-data/atom/procurement-notices/design",
    ]

    ns = {"atom": "http://www.w3.org/2005/Atom"}

    for url in urls:
        try:
            raw = http_get(url, timeout=30)
            from lxml import etree
            root = etree.fromstring(raw.encode(), parser=etree.XMLParser(recover=True))
            entries = root.findall("atom:entry", ns)
            logging.info(f"[BuySell] {len(entries)} entrées depuis {url}")

            for entry in entries:
                def g(tag):
                    el = entry.find(f"atom:{{tag}}", ns) or entry.find(tag)
                    return (el.text or "").strip() if el is not None else ""

                title   = (entry.find("atom:title", ns) or entry.find("title") or type("", (), {"text": ""})()).text or ""
                link_el = entry.find("atom:link", ns) or entry.find("link")
                link    = link_el.get("href", "") if link_el is not None else ""
                summary = (entry.find("atom:summary", ns) or entry.find("summary") or type("", (), {"text": ""})()).text or ""
                pub_raw = (entry.find("atom:published", ns) or entry.find("atom:updated", ns) or type("", (), {"text": ""})()).text or ""

                try:
                    pub_date = datetime.strptime(pub_raw[:10], "%Y-%m-%d")
                    if pub_date < since:
                        continue
                except Exception:
                    pass

                fp = hashlib.sha256(f"BuySell_{link}_{title[:40]}".encode()).hexdigest()[:16]

                projects.append({
                    "fingerprint":      fp,
                    "title":            title.strip(),
                    "url":              link,
                    "summary":          (summary or "")[:400],
                    "source":           "BuySell-CA",
                    "province":         None,
                    "owner":            "Gouvernement du Canada",
                    "category":         "Architecture",
                    "closing_date":     None,
                    "estimated_budget": None,
                })

        except URLError as e:
            logging.warning(f"[BuySell] Inaccessible : {e}")
        except ET.ParseError as e:
            logging.warning(f"[BuySell] XML invalide : {e}")
        except Exception as e:
            logging.error(f"[BuySell] Erreur : {e}")

    return projects

# ─── Filtrage lexical ─────────────────────────────────────────────────────────

def is_architecture_related(title: str, summary: str) -> bool:
    combined = f"{title} {summary}".lower()
    return any(kw.lower() in combined for kw in CONFIG["categories_cibles"])

# ─── Analyse Claude (budget + validation) ────────────────────────────────────

def analyse_with_claude(item: dict) -> dict:
    client = anthropic.Anthropic(api_key=CONFIG["anthropic_api_key"])
    prompt = f"""Appel d'offres canadien. Extrais en JSON strict (null si inconnu).

TITRE : {item['title']}
RÉSUMÉ : {item.get('summary','')[:600]}
SOURCE : {item['source']}

JSON requis :
{{
  "province": "Code 2 lettres ou null",
  "estimated_budget": null ou nombre entier $,
  "closing_date": "YYYY-MM-DD ou null",
  "owner": "Organisation cliente",
  "pertinent": true/false
}}"""
    try:
        resp = client.messages.create(
            model=CONFIG["claude_model"],
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        return json.loads(resp.content[0].text.strip())
    except Exception as e:
        logging.warning(f"[Claude] {e}")
        return {}

# ─── Rapport HTML ─────────────────────────────────────────────────────────────

def build_html(projects: list) -> str:
    STYLES = {
        "URGENT":      ("#dc2626", "#fef2f2"),
        "PRIORITAIRE": ("#ea580c", "#fff7ed"),
        "NORMAL":      ("#16a34a", "#f0fdf4"),
    }

    def urgency(d):
        if not d: return "NORMAL"
        try:
            days = (datetime.strptime(d, "%Y-%m-%d") - datetime.now()).days
            if days <= 7:  return "URGENT"
            if days <= 14: return "PRIORITAIRE"
        except Exception: pass
        return "NORMAL"

    def days_left(d):
        try: return max(0, (datetime.strptime(d, "%Y-%m-%d") - datetime.now()).days)
        except Exception: return "?"

    groups = {"URGENT": [], "PRIORITAIRE": [], "NORMAL": []}
    for p in projects:
        groups[urgency(p.get("closing_date"))].append(p)

    sections = ""
    for level, group in groups.items():
        if not group: continue
        color, bg = STYLES[level]
        rows = ""
        for p in sorted(group, key=lambda x: x.get("closing_date") or "9999"):
            budget = f"${p['estimated_budget']:,.0f}" if p.get("estimated_budget") else "N/D"
            closing = p.get("closing_date") or "N/D"
            rows += f"""<tr>
              <td style="padding:8px;border:1px solid #e5e7eb">
                <a href="{p['url']}" style="color:#1d4ed8;font-weight:600">{p['title']}</a>
                <br><small style="color:#6b7280">{(p.get('summary') or '')[:120]}</small>
              </td>
              <td style="padding:8px;border:1px solid #e5e7eb">{p.get('owner','N/D')}</td>
              <td style="padding:8px;border:1px solid #e5e7eb;text-align:center"><b>{p.get('province','?')}</b></td>
              <td style="padding:8px;border:1px solid #e5e7eb;text-align:right">{budget}</td>
              <td style="padding:8px;border:1px solid #e5e7eb;text-align:center">
                {closing}<br><small>({days_left(closing)} j)</small>
              </td>
              <td style="padding:8px;border:1px solid #e5e7eb">{p['source']}</td>
            </tr>"""
        sections += f"""
        <h3 style="color:{color};border-left:4px solid {color};padding-left:10px;margin-top:24px">
          {level} — {len(group)} AO
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

    return f"""<html><body style="font-family:Arial,sans-serif;max-width:900px;margin:auto;padding:24px">
      <h2 style="color:#1e3a5f;border-bottom:3px solid #1d4ed8;padding-bottom:10px">
        🏛 Vigie Architecture — {datetime.now().strftime('%Y-%m-%d %H:%M')}
      </h2>
      <p>{len(projects)} nouvel(aux) appel(s) d'offres | Sources : SEAO · BuySell-CA</p>
      {sections}
      <hr><p style="font-size:11px;color:#9ca3af">
        ABCP Architecture · QC : tous projets · Hors-QC : ≥ 10 M$
      </p></body></html>"""

def send_email(projects: list):
    if not projects:
        logging.info("Aucun projet — courriel non envoyé.")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Vigie AO] {len(projects)} appel(s) Architecture — {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    msg["From"] = CONFIG["smtp_user"]
    msg["To"]   = ", ".join(CONFIG["recipients"])
    msg.attach(MIMEText(build_html(projects), "html", "utf-8"))
    try:
        with smtplib.SMTP(CONFIG["smtp_host"], CONFIG["smtp_port"]) as s:
            s.ehlo(); s.starttls()
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

    since = datetime.now() - timedelta(days=CONFIG["lookback_days"])
    logging.info(f"Démarrage — depuis {since.strftime('%Y-%m-%d')}")

    dedup    = DeduplicationStore(CONFIG["db_path"])
    retained = []

    # Collecte toutes les sources
    all_items = []
    all_items += fetch_seao(since)
    all_items += fetch_buyandsell(since)
    logging.info(f"Total brut : {len(all_items)} items")

    for item in all_items:
        # 1. Déduplication
        if not dedup.is_new(item["fingerprint"]):
            continue

        # 2. Filtre lexical rapide
        if not is_architecture_related(item["title"], item.get("summary", "")):
            continue

        # 3. Analyse Claude
        analysis = analyse_with_claude(item)
        time.sleep(0.3)

        # 4. Claude dit non-pertinent → ignorer
        if analysis.get("pertinent") is False:
            logging.debug(f"Claude: non pertinent — {item['title'][:60]}")
            continue

        # 5. Province + budget
        province = analysis.get("province") or item.get("province") or "?"
        budget   = analysis.get("estimated_budget") or item.get("estimated_budget")
        closing  = analysis.get("closing_date") or item.get("closing_date")
        owner    = analysis.get("owner") or item.get("owner", "N/D")

        # 6. Filtre budget hors-QC
        if province not in ("QC", "?", None):
            if budget and budget < CONFIG["budget_threshold_hors_qc"]:
                logging.debug(f"Sous seuil ({province} {budget:,}$) : {item['title'][:50]}")
                continue

        project = {**item, "province": province, "estimated_budget": budget,
                   "closing_date": closing, "owner": owner}
        retained.append(project)
        dedup.mark(item["fingerprint"], item["source"], item["title"])
        logging.info(f"✓ [{item['source']}] {item['title'][:70]}")

    logging.info(f"Retenus : {len(retained)}")

    # Export JSON pour dashboard GitHub Pages
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump({
            "updated_at": datetime.now().isoformat(),
            "count": len(retained),
            "projects": retained,
        }, f, ensure_ascii=False, indent=2, default=str)
    logging.info("data.json exporté")

    send_email(retained)
    logging.info("Cycle terminé.")

if __name__ == "__main__":
    run()

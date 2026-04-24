"""
VIGIE ARCHITECTURE — v5.0
Source principale : API ouverte SEAO via donneesquebec.ca
Source secondaire : CanadaBuys (fédéral canadien)
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
    "lookback_days": 2,
    "recipients": ["pasgob@abcparchitecture.com"],
    "smtp_host": os.getenv("SMTP_HOST", "smtp.gmail.com"),
    "smtp_port": int(os.getenv("SMTP_PORT", 587)),
    "smtp_user": os.getenv("SMTP_USER", ""),
    "smtp_pass": os.getenv("SMTP_PASS", ""),
    "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
    "claude_model": "claude-haiku-4-5-20251001",
    "db_path": "vigie_dedup.db",
}

SEAO_DATASET_ID = "systeme-electronique-dappel-doffres-seao"
CKAN_API = "https://www.donneesquebec.ca/recherche/api/3/action"

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

# ─── HTTP ─────────────────────────────────────────────────────────────────────

def http_get(url, timeout=30):
    import gzip
    req = Request(url, headers={
        "User-Agent": "VigieArchitecture/5.0 (ABCP Architecture; pasgob@abcparchitecture.com)",
        "Accept": "application/json, application/xml, text/xml, */*",
        "Accept-Encoding": "gzip, deflate",
    })
    with urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding", "") == "gzip":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8")

# ─── Source 1 : SEAO ──────────────────────────────────────────────────────────

def get_seao_latest_json_url():
    try:
        raw = http_get(f"{CKAN_API}/package_show?id={SEAO_DATASET_ID}")
        data = json.loads(raw)
        resources = data.get("result", {}).get("resources", [])
        avis_en_cours = None
        latest_hebdo = None
        latest_date = None
        for r in resources:
            name = r.get("name", "").lower()
            fmt = r.get("format", "").upper()
            url = r.get("url", "")
            if not url or fmt not in ("JSON", ""):
                continue
            if "avis_en_cours" in name or "en_cours" in name:
                avis_en_cours = url
                break
            if "hebdo_" in name:
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
            logging.info(f"[SEAO] URL : {result}")
        return result
    except Exception as e:
        logging.error(f"[SEAO] Erreur CKAN : {e}")
        return None


def fetch_seao(since: datetime) -> list:
    projects = []
    url = get_seao_latest_json_url()
    if not url:
        logging.warning("[SEAO] Impossible de trouver l'URL JSON.")
        return []
    try:
        raw = http_get(url, timeout=60)
        data = json.loads(raw)
        releases = data if isinstance(data, list) else data.get("releases", [])
        logging.info(f"[SEAO] {len(releases)} entrées dans le fichier")
        for release in releases:
            try:
                tender = release.get("tender", {})
                title = tender.get("title") or release.get("title") or ""
                ref_id = release.get("ocid") or tender.get("id") or ""
                owner = (release.get("buyer", {}) or {}).get("name", "N/D")
                status = tender.get("status", "")
                if status in ("cancelled", "complete", "unsuccessful"):
                    continue
                closing_raw = tender.get("tenderPeriod", {}).get("endDate", "")
                try:
                    closing_date = datetime.strptime(closing_raw[:10], "%Y-%m-%d")
                    if closing_date < datetime.now():
                        continue
                except Exception:
                    closing_date = datetime.now() + timedelta(days=30)

                description = tender.get("description", "")
                category = ""
                items = tender.get("items", [])
                if items:
                    category = items[0].get("classification", {}).get("description", "")

                pub_date_raw = release.get("date", "")
                pub_date = pub_date_raw[:10] if pub_date_raw else datetime.now().strftime("%Y-%m-%d")

                # Extraire l'identifiant numérique SEAO (ex: ocds-ec9k95-20136571 → 20136571)
                seao_num = ref_id.split("-")[-1]
                ao_url = f"https://www.seao.ca/OpportunityPublication/rechercheAO/Details/Avis?id={seao_num}"

                fp = hashlib.sha256(f"SEAO_{ref_id}_{title[:40]}".encode()).hexdigest()[:16]
                projects.append({
                    "fingerprint":      fp,
                    "title":            title,
                    "url":              ao_url,
                    "summary":          description[:400],
                    "source":           "SEAO",
                    "province":         "QC",
                    "owner":            owner,
                    "category":         category,
                    "closing_date":     closing_date.strftime("%Y-%m-%d"),
                    "pub_date":         pub_date,
                    "ref_id":           seao_num,
                    "estimated_budget": None,
                })
            except Exception as e:
                logging.debug(f"[SEAO] Entrée ignorée : {e}")
        logging.info(f"[SEAO] {len(projects)} AOs trouvés")
    except Exception as e:
        logging.error(f"[SEAO] Erreur : {e}", exc_info=True)
    return projects

# ─── Source 2 : CanadaBuys ────────────────────────────────────────────────────

def fetch_canadabuys(since: datetime) -> list:
    import csv, io
    url = "https://canadabuys.canada.ca/opendata/pub/newTenderNotice-nouvelAvisAppelOffres.csv"
    projects = []
    PROVINCES_CIBLES = {"NB", "NS", "PE", "ON", "NL", "QC"}
    try:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        from urllib.request import urlopen as ssl_urlopen
        with ssl_urlopen(url, timeout=60, context=ctx) as r:
            raw = r.read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(raw))
        for row in reader:
            try:
                title = (row.get("title-titre") or row.get("description-eng") or "")
                province = (row.get("deliveryProvince-livraisonProvince") or
                            row.get("provinceCode-codeProvince") or "?")
                org = row.get("organizationName-nom-eng", "")
                if province not in PROVINCES_CIBLES:
                    continue
                if not is_architecture_related(title, ""):
                    continue
                pub_raw = row.get("publicationDate-datePublication", "")
                try:
                    pub_date = datetime.strptime(pub_raw[:10], "%Y-%m-%d")
                    if pub_date < since:
                        continue
                    pub_date_str = pub_date.strftime("%Y-%m-%d")
                except Exception:
                    pub_date_str = datetime.now().strftime("%Y-%m-%d")
                ref_id = row.get("referenceNumber-numeroReference", "")
                closing_raw = row.get("closingDate-dateCloture", "")
                fp = hashlib.sha256(f"CanadaBuys_{ref_id}_{title[:40]}".encode()).hexdigest()[:16]
                projects.append({
                    "fingerprint":      fp,
                    "title":            title,
                    "url":              f"https://canadabuys.canada.ca/en/tender-opportunities/tender-notice/{ref_id}",
                    "summary":          row.get("description-eng", "")[:400],
                    "source":           "CanadaBuys",
                    "province":         province,
                    "owner":            org or "N/D",
                    "category":         "Architecture",
                    "closing_date":     closing_raw[:10] if closing_raw else None,
                    "pub_date":         pub_date_str,
                    "ref_id":           ref_id,
                    "estimated_budget": None,
                })
            except Exception as e:
                logging.debug(f"[CanadaBuys] Ligne ignorée : {e}")
        logging.info(f"[CanadaBuys] {len(projects)} AOs trouvés")
    except Exception as e:
        logging.error(f"[CanadaBuys] Erreur : {e}")
    return projects

# ─── Filtrage lexical ─────────────────────────────────────────────────────────

def is_architecture_related(title: str, summary: str) -> bool:
    combined = f"{title} {summary}".lower()
    return any(kw.lower() in combined for kw in CONFIG["categories_cibles"])

# ─── Analyse Claude ───────────────────────────────────────────────────────────

def analyse_with_claude(item: dict) -> dict:
    client = anthropic.Anthropic(api_key=CONFIG["anthropic_api_key"])
    prompt = f"""Tu es un agent d'analyse d'appels d'offres pour ABCP Architecture, cabinet d'architecture au Québec.

TITRE : {item['title']}
RÉSUMÉ : {item.get('summary','')[:800]}
SOURCE : {item['source']}
PROVINCE : {item.get('province','?')}
NOTE : Pour SEAO, le résumé est souvent vide. Déduis prix/entrevue/visite/format du titre et du contexte québécois (généralement Prix=Oui, Format lettre).

Extrais en JSON strict (null si inconnu) :
{{
  "province": "Code 2 lettres",
  "estimated_budget": null ou nombre entier en dollars,
  "closing_date": "YYYY-MM-DD",
  "owner": "Organisation cliente",
  "pertinent": true ou false,
  "prix": "Oui" si soumission de prix requise, "Non" si concours ou qualifications seulement,
  "entrevue": "Oui" si entrevue mentionnée, "Non" sinon, null si inconnu,
  "visite_obligatoire": "Oui" ou "Non" ou null,
  "date_visite": "YYYY-MM-DD" ou null,
  "format": "Format lettre" ou "Formulaire imposé" ou "Format libre" ou null,
  "categorie_ao": "OS à discuter" si budget inconnu ou >10M$, "OS à surveiller" si budget <5M$ ou délai >60 jours,
  "cote_strategique": "A" si client institutionnel majeur ou budget >20M$, "B" si client municipal ou budget 5-20M$, "C" si budget <5M$,
  "resume_comite": "1 phrase max résumant l'essentiel pour le comité stratégique ABCP"
}}"""
    try:
        resp = client.messages.create(
            model=CONFIG["claude_model"],
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text.strip()
        if not text:
            return {}
        text = text.replace("```json", "").replace("```", "").strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            text = text[start:end]
        return json.loads(text)
    except Exception as e:
        logging.warning(f"[Claude] {e}")
        return {}

# ─── Rapport HTML (format carte) ─────────────────────────────────────────────

def build_html(projects: list) -> str:
    URGENCY_COLOR = {
        "URGENT":      ("#dc2626", "#fef2f2"),
        "PRIORITAIRE": ("#ea580c", "#fff7ed"),
        "NORMAL":      ("#16a34a", "#f0fdf4"),
    }
    COTE_COLOR = {"A": "#16a34a", "B": "#ea580c", "C": "#94a3b8"}

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

    def fmt_date(d):
        if not d: return "N/D"
        try: return datetime.strptime(d, "%Y-%m-%d").strftime("%d %b %Y")
        except: return d

    # Tri par date de publication (plus récent en premier)
    sorted_projects = sorted(
        projects,
        key=lambda x: x.get("pub_date") or x.get("added_at") or "",
        reverse=True
    )

    groups = {"URGENT": [], "PRIORITAIRE": [], "NORMAL": []}
    for p in sorted_projects:
        groups[urgency(p.get("closing_date"))].append(p)

    sections = ""
    for level, group in groups.items():
        if not group: continue
        color, bg = URGENCY_COLOR[level]
        cards = ""
        for p in group:
            cote = p.get("cote_strategique") or "?"
            cc = COTE_COLOR.get(cote, "#94a3b8")
            budget = f"${p['estimated_budget']:,.0f}" if p.get("estimated_budget") else "N/D"
            dl = days_left(p.get("closing_date"))
            resume = p.get("resume_comite") or p.get("summary") or ""
            ref = p.get("ref_id") or ""
            pub = fmt_date(p.get("pub_date"))

            badges = ""
            if p.get("prix") and p["prix"] != "?":
                badges += f'<span style="background:#e0f2fe;color:#0369a1;padding:2px 7px;border-radius:3px;font-size:10px;margin-right:4px">Prix: {p["prix"]}</span>'
            if p.get("entrevue") and p["entrevue"] not in ("?", None):
                badges += f'<span style="background:#fef9c3;color:#854d0e;padding:2px 7px;border-radius:3px;font-size:10px;margin-right:4px">Entrevue: {p["entrevue"]}</span>'
            if p.get("visite_obligatoire") and p["visite_obligatoire"] not in ("?", None):
                badges += f'<span style="background:#fce7f3;color:#9d174d;padding:2px 7px;border-radius:3px;font-size:10px;margin-right:4px">Visite: {p["visite_obligatoire"]}</span>'
            if p.get("format_ao") and p["format_ao"] not in ("?", None):
                badges += f'<span style="background:#f3e8ff;color:#6b21a8;padding:2px 7px;border-radius:3px;font-size:10px;margin-right:4px">{p["format_ao"]}</span>'
            if p.get("categorie_ao"):
                badges += f'<span style="background:#f1f5f9;color:#475569;padding:2px 7px;border-radius:3px;font-size:10px;margin-right:4px">{p["categorie_ao"]}</span>'

            cards += f"""
            <div style="border:1px solid #e5e7eb;border-left:4px solid {color};border-radius:8px;padding:14px 16px;margin-bottom:10px;background:#ffffff;">
              <table style="width:100%;border-collapse:collapse;"><tr>
                <td style="vertical-align:top;padding:0;">
                  <div style="font-size:10px;color:#94a3b8;margin-bottom:5px;letter-spacing:0.04em">
                    <b style="color:#64748b">{p.get('source','')}</b> &nbsp;·&nbsp; {p.get('province','?')}
                    &nbsp;·&nbsp; Pub. {pub}
                    {f' &nbsp;·&nbsp; <span style="color:#94a3b8">#{ref}</span>' if ref else ''}
                  </div>
                  <a href="{p.get('url','#')}" style="color:#1d4ed8;font-weight:700;font-size:14px;text-decoration:none;line-height:1.4;display:block;margin-bottom:4px">{p.get('title','Sans titre')}</a>
                  <div style="color:#475569;font-size:12px;margin-bottom:6px">{p.get('owner','N/D')}</div>
                  {f'<div style="color:#64748b;font-size:11px;margin-bottom:8px;font-style:italic;line-height:1.5">{resume[:200]}</div>' if resume else ''}
                  {f'<div>{badges}</div>' if badges else ''}
                </td>
                <td style="vertical-align:top;text-align:right;padding:0 0 0 16px;white-space:nowrap;min-width:100px;">
                  <div style="font-size:22px;font-weight:800;color:{color};line-height:1">{dl} j</div>
                  <div style="font-size:10px;color:#94a3b8;margin-bottom:8px">avant clôture</div>
                  <div style="font-size:11px;color:#475569">{fmt_date(p.get('closing_date'))}</div>
                  <div style="font-size:13px;font-weight:700;color:{cc};margin-top:8px;background:{bg};padding:3px 8px;border-radius:4px;display:inline-block">Cote {cote}</div>
                  {f'<div style="font-size:11px;color:#64748b;margin-top:4px">{budget}</div>' if budget != "N/D" else ''}
                </td>
              </tr></table>
            </div>"""

        sections += f"""
        <div style="margin-bottom:28px;">
          <div style="display:inline-block;background:{color};color:#fff;padding:4px 14px;border-radius:20px;font-size:12px;font-weight:700;margin-bottom:10px;letter-spacing:0.06em">
            {level} — {len(group)} appel(s) d'offres
          </div>
          {cards}
        </div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="font-family:Arial,sans-serif;max-width:680px;margin:auto;padding:20px;background:#f8fafc;">
  <div style="background:linear-gradient(135deg,#1e3a5f,#1d4ed8);border-radius:12px;padding:20px 24px;margin-bottom:24px;">
    <div style="color:#93c5fd;font-size:11px;letter-spacing:0.1em;margin-bottom:4px">ABCP ARCHITECTURE · VIGIE AUTOMATIQUE</div>
    <h1 style="color:#fff;margin:0 0 4px;font-size:20px">🏛 Vigie Appels d'offres</h1>
    <p style="color:#bfdbfe;margin:0;font-size:12px">
      {datetime.now().strftime('%d %b %Y, %H:%M')} &nbsp;·&nbsp; {len(projects)} nouveau(x) AO détecté(s)
      &nbsp;·&nbsp; SEAO · CanadaBuys
    </p>
  </div>
  {sections}
  <div style="margin-top:16px;padding-top:14px;border-top:1px solid #e5e7eb;font-size:10px;color:#94a3b8;text-align:center">
    Vigie automatique ABCP Architecture &nbsp;·&nbsp; QC : tous projets &nbsp;·&nbsp; Hors-QC : ≥ 10 M$
  </div>
</body></html>"""


def send_email(projects: list):
    if not projects:
        logging.info("Aucun projet — courriel non envoyé.")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = (f"[Vigie AO] {len(projects)} appel(s) Architecture — "
                      f"{datetime.now().strftime('%Y-%m-%d %H:%M')}")
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

    all_items = []
    all_items += fetch_seao(since)
    all_items += fetch_canadabuys(since)
    logging.info(f"Total brut : {len(all_items)} items")

    for item in all_items:
        if not dedup.is_new(item["fingerprint"]):
            continue
        if not is_architecture_related(item["title"], item.get("summary", "")):
            continue

        analysis = analyse_with_claude(item)
        time.sleep(0.3)

        if analysis.get("pertinent") is False:
            logging.debug(f"Non pertinent : {item['title'][:60]}")
            continue

        province = analysis.get("province") or item.get("province") or "?"
        budget   = analysis.get("estimated_budget") or item.get("estimated_budget")
        closing  = analysis.get("closing_date") or item.get("closing_date")
        owner    = analysis.get("owner") or item.get("owner", "N/D")

        if province not in ("QC", "?", None):
            if budget and budget < CONFIG["budget_threshold_hors_qc"]:
                continue

        project = {
            **item,
            "province":           province,
            "estimated_budget":   budget,
            "closing_date":       closing,
            "owner":              owner,
            "prix":               analysis.get("prix"),
            "entrevue":           analysis.get("entrevue"),
            "visite_obligatoire": analysis.get("visite_obligatoire"),
            "date_visite":        analysis.get("date_visite"),
            "format_ao":          analysis.get("format"),
            "categorie_ao":       analysis.get("categorie_ao"),
            "cote_strategique":   analysis.get("cote_strategique"),
            "resume_comite":      analysis.get("resume_comite"),
        }
        retained.append(project)
        dedup.mark(item["fingerprint"], item["source"], item["title"])
        logging.info(f"✓ [{item['source']}] {item['title'][:70]}")

    logging.info(f"Retenus : {len(retained)}")

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump({
            "updated_at": datetime.now().isoformat(),
            "count":      len(retained),
            "projects":   retained,
        }, f, ensure_ascii=False, indent=2, default=str)
    logging.info("data.json exporté")

    send_email(retained)
    logging.info("Cycle terminé.")


if __name__ == "__main__":
    run()
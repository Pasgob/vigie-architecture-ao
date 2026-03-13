import os
import re
import json
import time
import hashlib
import logging
import smtplib
import asyncio
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum
from typing import Optional

import aiohttp
import anthropic
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

CONFIG = {
    "budget_threshold_hors_qc": 10_000_000,
    "categories_cibles": ["Architecture","Architecte","Design","Aménagement","Urbanisme","Paysager","Intérieur","Conception"],
    "lookback_minutes": 70,
    "recipients": ["pasgob@abcparchitecture.com"],
    "smtp_host": os.getenv("SMTP_HOST", "smtp.office365.com"),
    "smtp_port": int(os.getenv("SMTP_PORT", 587)),
    "smtp_user": os.getenv("SMTP_USER", ""),
    "smtp_pass": os.getenv("SMTP_PASS", ""),
    "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", ""),
    "claude_model": "claude-sonnet-4-20250514",
    "db_path": "vigie_dedup.db",
}

class Province(Enum):
    QC="QC"; NB="NB"; NS="NS"; PE="PE"; ON="ON"
    BC="BC"; AB="AB"; MB="MB"; SK="SK"; OTHER="OTHER"

@dataclass
class Project:
    id: str
    title: str
    owner: str
    closing_date: datetime
    url: str
    source: str
    location: Province
    estimated_budget: Optional[float] = None
    category: Optional[str] = None
    description: Optional[str] = None

    @property
    def fingerprint(self):
        key = f"{self.source}_{self.id}_{self.title[:50]}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    @property
    def is_eligible_geo_budget(self):
        if self.location == Province.QC:
            return True
        if self.estimated_budget is None:
            return True
        return self.estimated_budget >= CONFIG["budget_threshold_hors_qc"]

    @property
    def days_until_closing(self):
        return max(0, (self.closing_date - datetime.now()).days)

    @property
    def urgency(self):
        d = self.days_until_closing
        if d <= 7:  return "URGENT"
        if d <= 14: return "PRIORITAIRE"
        return "NORMAL"

class DeduplicationStore:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS seen_projects
            (fingerprint TEXT PRIMARY KEY, source TEXT, title TEXT, seen_at TEXT)""")
        self.conn.execute("DELETE FROM seen_projects WHERE seen_at < datetime('now', '-90 days')")
        self.conn.commit()

    def is_new(self, fingerprint):
        return self.conn.execute("SELECT 1 FROM seen_projects WHERE fingerprint=?", (fingerprint,)).fetchone() is None

    def mark_seen(self, project):
        self.conn.execute("INSERT OR IGNORE INTO seen_projects VALUES (?,?,?,?)",
            (project.fingerprint, project.source, project.title[:100], datetime.now().isoformat()))
        self.conn.commit()

class ParserAgent:
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=CONFIG["anthropic_api_key"])

    def parse_project(self, raw_html, source_name, url):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw_html, "html.parser")
        text = soup.get_text(separator=" ", strip=True)[:4000]
        prompt = f"""Tu es un agent d'extraction de données pour des appels d'offres publics canadiens.
SOURCE : {source_name} | URL : {url}
TEXTE : {text}
Extrais en JSON strict (null si introuvable) :
{{"title":"...","id":"...","owner":"...","closing_date":"YYYY-MM-DD","estimated_budget":null,"category":"...","province":"QC","description":"..."}}
Réponds UNIQUEMENT avec le JSON."""
        try:
            resp = self.client.messages.create(
                model=CONFIG["claude_model"], max_tokens=500,
                messages=[{"role":"user","content":prompt}])
            return json.loads(resp.content[0].text.strip())
        except Exception as e:
            logging.warning(f"[Parser] {url}: {e}")
            return None

    def is_architecture_related(self, title, description):
        combined = f"{title} {description or ''}".lower()
        return any(kw.lower() in combined for kw in CONFIG["categories_cibles"])

class BaseScraper(ABC):
    name = "Generic"
    def __init__(self):
        self.headers = {"User-Agent":"Mozilla/5.0","Accept-Language":"fr-CA,fr;q=0.9"}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=4, max=30),
           retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)))
    async def _get(self, session, url):
        async with session.get(url, headers=self.headers, timeout=aiohttp.ClientTimeout(total=30)) as r:
            r.raise_for_status()
            return await r.text()

    @abstractmethod
    async def fetch_listings(self, session, since): ...

class SEAOScraper(BaseScraper):
    name = "SEAO"
    async def fetch_listings(self, session, since):
        results = []
        try:
            url = "https://www.seao.ca/OpportunityPublication/rechercheAO/Index?types=AO&categorieId=architecture"
            html = await self._get(session, url)
            soup = BeautifulSoup(html, "html.parser")
            for row in soup.select("table.listingAO tbody tr"):
                try:
                    link = row.select_one("a[href*='Details']")
                    if not link: continue
                    detail_url = "https://www.seao.ca" + link["href"]
                    detail_html = await self._get(session, detail_url)
                    results.append({"url":detail_url,"raw_html":detail_html,"source":self.name})
                    await asyncio.sleep(1)
                except Exception as e:
                    logging.warning(f"[SEAO] {e}")
        except Exception as e:
            logging.error(f"[SEAO] {e}")
        return results

class BidsAndTendersScraper(BaseScraper):
    name = "BidsAndTenders"
    async def fetch_listings(self, session, since):
        results = []
        try:
            url = "https://www.bidsandtenders.ca/page.asp?page=RFP&keywords=architecture&region=NB,NS,PE"
            html = await self._get(session, url)
            soup = BeautifulSoup(html, "html.parser")
            for item in soup.select("div.tender-item, tr.tenderRow"):
                try:
                    link = item.select_one("a[href*='tender']")
                    if not link: continue
                    detail_url = link["href"]
                    if not detail_url.startswith("http"):
                        detail_url = "https://www.bidsandtenders.ca/" + detail_url.lstrip("/")
                    detail_html = await self._get(session, detail_url)
                    results.append({"url":detail_url,"raw_html":detail_html,"source":self.name})
                    await asyncio.sleep(1.5)
                except Exception as e:
                    logging.warning(f"[B&T] {e}")
        except Exception as e:
            logging.error(f"[B&T] {e}")
        return results

class NBONScraper(BaseScraper):
    name = "NBON"
    async def fetch_listings(self, session, since):
        results = []
        try:
            html = await self._get(session, "https://nbon.gnb.ca/opportunities?category=architecture")
            soup = BeautifulSoup(html, "html.parser")
            for card in soup.select(".opportunity-card, .opportunity-row"):
                try:
                    link = card.select_one("a")
                    if not link: continue
                    detail_url = "https://nbon.gnb.ca" + link["href"]
                    detail_html = await self._get(session, detail_url)
                    results.append({"url":detail_url,"raw_html":detail_html,"source":self.name})
                    await asyncio.sleep(1)
                except Exception as e:
                    logging.warning(f"[NBON] {e}")
        except Exception as e:
            logging.error(f"[NBON] {e}")
        return results

class NSTendersScraper(BaseScraper):
    name = "NSTenders"
    async def fetch_listings(self, session, since):
        results = []
        try:
            html = await self._get(session, "https://novascotia.ca/tenders/tenders.aspx")
            soup = BeautifulSoup(html, "html.parser")
            for row in soup.select("table#tblTenders tbody tr"):
                try:
                    cells = row.find_all("td")
                    if len(cells) < 2: continue
                    if not any(kw.lower() in cells[1].get_text().lower() for kw in CONFIG["categories_cibles"]): continue
                    link = cells[1].select_one("a")
                    if not link: continue
                    detail_url = "https://novascotia.ca" + link["href"]
                    detail_html = await self._get(session, detail_url)
                    results.append({"url":detail_url,"raw_html":detail_html,"source":self.name})
                    await asyncio.sleep(1)
                except Exception as e:
                    logging.warning(f"[NS] {e}")
        except Exception as e:
            logging.error(f"[NS] {e}")
        return results

class ReporterAgent:
    def build_html(self, projects, errors):
        groups = {"URGENT":[],"PRIORITAIRE":[],"NORMAL":[]}
        for p in sorted(projects, key=lambda x: x.closing_date):
            groups[p.urgency].append(p)
        styles = {"URGENT":("#c0392b","#fdecea"),"PRIORITAIRE":("#e67e22","#fef5e7"),"NORMAL":("#27ae60","#eafaf1")}
        sections = ""
        for level, group in groups.items():
            if not group: continue
            color, bg = styles[level]
            rows = "".join(f"""<tr>
                <td><a href="{p.url}">{p.title}</a><br><small>{p.description or ''}</small></td>
                <td>{p.owner}</td><td>{p.location.value}</td>
                <td>{"${:,.0f}".format(p.estimated_budget) if p.estimated_budget else "N/D"}</td>
                <td>{p.closing_date.strftime('%Y-%m-%d')} ({p.days_until_closing} j)</td>
                <td>{p.source}</td></tr>""" for p in group)
            sections += f"""<h3 style="color:{color}">{level} — {len(group)} AO</h3>
                <table border="1" cellpadding="6" style="width:100%;border-collapse:collapse;font-size:13px">
                <tr style="background:{bg}"><th>Projet</th><th>Client</th><th>Prov.</th>
                <th>Budget</th><th>Clôture</th><th>Source</th></tr>{rows}</table>"""
        err = f'<p style="color:gray;font-size:11px">⚠️ {len(errors)} avertissement(s)</p>' if errors else ""
        return f"""<html><body style="font-family:Arial,sans-serif;max-width:900px;margin:auto;padding:20px">
            <h2>🏛 Vigie Architecture — {datetime.now().strftime('%Y-%m-%d %H:%M')}</h2>
            <p>{len(projects)} nouveau(x) appel(s) d'offres</p>{sections}{err}</body></html>"""

    def send_email(self, projects, errors):
        if not projects:
            logging.info("Aucun projet — courriel non envoyé.")
            return
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[Vigie AO] {len(projects)} appel(s) Architecture — {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        msg["From"] = CONFIG["smtp_user"]
        msg["To"] = ", ".join(CONFIG["recipients"])
        msg.attach(MIMEText(self.build_html(projects, errors), "html", "utf-8"))
        try:
            with smtplib.SMTP(CONFIG["smtp_host"], CONFIG["smtp_port"]) as s:
                s.starttls()
                s.login(CONFIG["smtp_user"], CONFIG["smtp_pass"])
                s.sendmail(CONFIG["smtp_user"], CONFIG["recipients"], msg.as_string())
            logging.info(f"Courriel envoyé — {len(projects)} projet(s)")
        except Exception as e:
            logging.error(f"SMTP : {e}")

class VigieOrchestrator:
    def __init__(self):
        self.scrapers = [SEAOScraper(), BidsAndTendersScraper(), NBONScraper(), NSTendersScraper()]
        self.dedup    = DeduplicationStore(CONFIG["db_path"])
        self.parser   = ParserAgent()
        self.reporter = ReporterAgent()

    async def _fetch_all(self, since):
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False)) as session:
            results = await asyncio.gather(*[s.fetch_listings(session, since) for s in self.scrapers], return_exceptions=True)
        all_raw = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logging.error(f"{self.scrapers[i].name} : {r}")
            else:
                all_raw.extend(r)
        return all_raw

    def run(self):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
            handlers=[logging.FileHandler("vigie.log", encoding="utf-8"), logging.StreamHandler()])
        since = datetime.now() - timedelta(minutes=CONFIG["lookback_minutes"])
        logging.info(f"Démarrage — depuis {since.strftime('%H:%M')}")
        all_raw = asyncio.run(self._fetch_all(since))
        projects, errors = [], []
        for raw in all_raw:
            try:
                data = self.parser.parse_project(raw["raw_html"], raw["source"], raw["url"])
                if not data: continue
                province_code = (data.get("province") or "OTHER").upper()
                province = Province[province_code] if province_code in Province.__members__ else Province.OTHER
                closing_raw = data.get("closing_date", "")
                closing_date = datetime.strptime(closing_raw, "%Y-%m-%d") if closing_raw else datetime.now() + timedelta(days=30)
                project = Project(
                    id=str(data.get("id") or raw["url"].split("/")[-1]),
                    title=data.get("title","Sans titre"),
                    owner=data.get("owner","Inconnu"),
                    closing_date=closing_date, url=raw["url"], source=raw["source"],
                    location=province,
                    estimated_budget=float(data["estimated_budget"]) if data.get("estimated_budget") else None,
                    category=data.get("category"), description=data.get("description"))
                if not self.dedup.is_new(project.fingerprint): continue
                if not project.is_eligible_geo_budget: continue
                if not self.parser.is_architecture_related(project.title, project.description): continue
                projects.append(project)
                self.dedup.mark_seen(project)
                logging.info(f"✓ [{project.source}] {project.title[:60]}")
                time.sleep(0.5)
            except Exception as e:
                errors.append(str(e))
        self.reporter.send_email(projects, errors)
        logging.info("Cycle terminé.")

if __name__ == "__main__":
    VigieOrchestrator().run()

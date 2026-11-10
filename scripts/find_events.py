#!/usr/bin/env python3
"""
find_events.py
Event finder for the Event Lead Engine. Markets: US, UK, DE.

What it does
  1. Builds Serper queries from your five source types, per market:
       1 venue / trade fair calendars
       2 association, user group and media calendars
       3 vendor and analyst pages
       4 LinkedIn posts that mention your company (names only, via Google snippets; needs COMPANY_NAME)
       5 aggregators (names only, lowest weight)
  2. Turns results into candidate events. Aggregator, LinkedIn, venue and media hits
     are NOT trusted: the script looks for the organiser page and uses that instead.
  3. Opens each organiser page once (polite, robots.txt aware) and reads: date, city,
     speaker / sponsor / exhibitor links, price, audience terms.
     Pages it cannot open are written to manual_needed.csv. Paste the page text into
     manual/<slug>.txt and run again.
  4. Applies your hard filters, scores 0 to 5 (buyer density x3, ICP match x3,
     lead data access x2, timing x1, cost x1) and picks the top events per market.

Nothing is invented: a value the script cannot read stays "?" or empty.
Scores are a draft from page text. Review the top 15 by hand before you decide.

Setup
  pip install requests beautifulsoup4        (beautifulsoup4 is optional)
  Put your Serper key in the environment variable SERPER_API_KEY, or in a .env file
  next to this script (SERPER_API_KEY=your_key). Never paste a key into this file.
  Optional: set COMPANY_NAME to the company you are sourcing for. Source type 4 then
  looks for LinkedIn posts that mention it. Leave it empty to skip that source.

Run
  python find_events.py                       all markets, verify, 5 US / 3 UK / 2 DE
  python find_events.py --markets US UK
  python find_events.py --quota US=5 UK=2 DE=3 --fill
  python find_events.py --no-verify           search only, no page fetching
  python find_events.py --selftest            tests without any network
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html as htmllib
import json
import os
import re
import sys
import time
import urllib.robotparser as robotparser
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import requests

try:
    from bs4 import BeautifulSoup
except ImportError:  # optional
    BeautifulSoup = None

# --------------------------------------------------------------------------------------
# CONFIG (edit here)
# --------------------------------------------------------------------------------------
# Keep this empty. The key is read from SERPER_API_KEY or a .env file (see load_key).
SERPER_KEY = ""
# Company you are sourcing for. Used only by source type 4 (LinkedIn mentions). Empty = skip.
COMPANY_NAME = os.environ.get("COMPANY_NAME", "").strip()

TODAY = date.today()
WINDOW_START = date(2026, 11, 14)   # first day that counts
CORE_END = date(2026, 12, 31)       # core window ends
FALLBACK_END = date(2027, 2, 28)    # fallback window ends (nothing later)
QUOTA = {"US": 5, "UK": 3, "DE": 2}

SRC_LABEL = {
    1: "1 venue/trade fair calendar",
    2: "2 association/media calendar",
    3: "3 vendor/analyst page",
    4: "4 company LinkedIn mentions",
    5: "5 aggregator (names only)",
}

VENUE = {
    "US": ["mccormickplace.com", "javitscenter.com", "lvcva.com", "gwcca.org", "occc.net"],
    "UK": ["excel.london", "thenec.co.uk", "olympia.london", "manchestercentral.co.uk"],
    "DE": ["messe-muenchen.de", "messe-berlin.de", "messe-duesseldorf.de", "messe-stuttgart.de",
           "koelnmesse.de", "messefrankfurt.com", "hamburg-messe.de", "auma.de"],
}
ASSOC = {
    "US": ["ismworld.org", "iofm.com", "asug.com", "sapinsider.org", "cscmp.org", "ascm.org",
           "mhi.org", "ssonetwork.com", "financialprofessionals.org"],
    "UK": ["cips.org", "sapusers.org", "logistics.org.uk", "ciltuk.org.uk", "ssonetwork.com",
           "ifol.org.uk"],
    "DE": ["bme.de", "dsag.de", "bvl.de", "handelsblatt.com", "ssonetwork.com"],
}
MEDIA = {
    "US": ["sdcexec.com", "supplychainbrain.com", "supplychaindive.com"],
    "UK": ["supplychaindigital.com", "procurementmag.com", "logisticsmanager.com"],
    "DE": ["logistik-heute.de", "beschaffung-aktuell.de"],
}
VENDOR = ["gartner.com", "sap.com", "coupa.com", "highradius.com", "basware.com", "esker.com",
          "jaggaer.com", "ivalua.com", "gep.com", "zip.com", "tungstenautomation.com",
          "rossum.ai", "forrester.com", "idc.com", "kinaxis.com", "e2open.com",
          "blueyonder.com", "manh.com", "oracle.com"]
AGG = ["10times.com", "clocate.com", "neventum.com", "ntradeshows.com", "tsnn.com",
       "tradefairdates.com", "luminik.io", "showsbee.com", "cantonfair.net",
       "sharedaudiences.com", "allai.events", "lensmor.com", "conferenceindex.org",
       "internationalconferencealerts.com", "beri.net", "gracker.ai"]
BLOCK = ["eventbrite.com", "eventbrite.co.uk", "eventbrite.de", "cvent.com", "rainfocus.com",
         "facebook.com", "instagram.com", "youtube.com", "x.com", "twitter.com", "reddit.com",
         "quora.com", "wikipedia.org", "indeed.com", "glassdoor.com", "xing.com", "pinterest.com",
         "globenewswire.com", "prnewswire.com", "businesswire.com", "medium.com", "meetup.com",
         "kennedy-center.org", "discoverlosangeles.com", "meetingstoday.com", "tripadvisor.com"]

# Queries are kept short on purpose (Google can ignore very long queries).
KW = {
    "US": '(procurement OR "supply chain" OR "accounts payable" OR "shared services" OR SAP)',
    "UK": '(procurement OR "supply chain" OR "accounts payable" OR "shared services" OR SAP)',
    "DE": '(Einkauf OR Beschaffung OR Logistik OR "Supply Chain" OR "Shared Services" OR SAP)',
}
EVW = {
    "US": '(conference OR summit OR expo OR forum)',
    "UK": '(conference OR summit OR expo OR forum)',
    "DE": '(Kongress OR Messe OR Symposium OR Forum OR Summit)',
}
GEO = {
    "US": '(USA OR "United States")',
    "UK": '(UK OR London OR "United Kingdom")',
    "DE": '(Deutschland OR Germany OR Berlin OR München)',
}
SERPER_LOCALE = {"US": ("us", "en"), "UK": ("gb", "en"), "DE": ("de", "de")}
CITIES = {
    "US": ["Las Vegas", "Orlando", "Chicago", "New York", "Atlanta", "National Harbor", "Washington",
           "Dallas", "Houston", "Nashville", "Miami", "San Francisco", "San Diego", "Boston", "Denver",
           "Phoenix", "Charleston", "Anaheim", "Los Angeles", "Seattle", "Austin", "Philadelphia",
           "Minneapolis", "Columbus", "Indianapolis", "Charlotte", "Tampa", "Fort Worth",
           "Louisville", "Detroit", "Long Beach"],
    "UK": ["London", "Birmingham", "Manchester", "Glasgow", "Edinburgh", "Harrogate", "Coventry",
           "Liverpool", "Leeds", "Bristol", "Newcastle", "Farnborough", "Cardiff", "Belfast",
           "Nottingham", "Telford", "Windsor"],
    "DE": ["Berlin", "München", "Munich", "Hamburg", "Düsseldorf", "Dusseldorf", "Frankfurt",
           "Köln", "Cologne", "Stuttgart", "Nürnberg", "Nuremberg", "Hannover", "Dortmund",
           "Bremen", "Leipzig", "Mannheim", "Karlsruhe", "Dresden", "Hockenheim", "Wiesbaden",
           "Augsburg", "Wolfsburg"],
}
COUNTRY_WORDS = {
    "US": r"united states|\bU\.?S\.?A\.?\b",
    "UK": r"united kingdom|\bU\.?K\.?\b|england|scotland",
    "DE": r"deutschland|germany",
}

BUYER = {
    "procurement": r"procure|purchasing|sourcing|\bcpo\b|einkauf|beschaffung",
    "supply_chain": r"supply chain|logistic|lieferkette|logistik|warehous|fulfil",
    "finance_ops": r"accounts payable|accounts receivable|shared services|finance operations|"
                   r"order[- ]to[- ]cash|procure[- ]to[- ]pay|\binvoice|rechnungswesen|"
                   r"finanzbuchhaltung|\bgbs\b|controlling",
    "sap_erp": r"\bsap\b|s/4hana|\berp\b|dynamics 365|oracle|jd edwards|dsag|ukisug|\basug\b",
    "operations": r"order management|customer service|auftragsabwicklung|business services|"
                  r"operations (leader|director|head)|\bcoo\b",
}
ICP = {
    "p2p_o2c": r"procure[- ]to[- ]pay|order[- ]to[- ]cash|\bp2p\b|\bo2c\b|order (processing|entry)|"
               r"\binvoice|auftragsabwicklung|eingangsrechnung",
    "systems": r"\bsap\b|dynamics 365|oracle|jd edwards|\bjde\b|\bedi\b|\berp\b",
    "industries": r"manufactur|retail|wholesale|distribution|fmcg|consumer goods|\bcpg\b|\bfood\b|"
                  r"beverage|fertigung|\bhandel\b|großhandel",
    "ai_automation": r"\bai\b|artificial intelligence|agentic|automation|\bki\b|"
                     r"künstliche intelligenz|generative",
    "enterprise": r"enterprise|fortune \d+|\bdax\b|konzern|multinational",
}

# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------
def reg_domain(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def dom_in(dom: str, lst) -> bool:
    return any(dom == d or dom.endswith("." + d) for d in lst)


ALL_VENUE = [d for v in VENUE.values() for d in v]
ALL_ASSOC = [d for v in ASSOC.values() for d in v]
ALL_MEDIA = [d for v in MEDIA.values() for d in v]


def classify(dom: str) -> int:
    """-1 ignore, 0 unknown web, 1..5 your source types."""
    if dom_in(dom, BLOCK) or dom.endswith((".gov", ".edu", ".mil", ".gov.uk", ".ac.uk")):
        return -1
    if dom_in(dom, AGG):
        return 5
    if "linkedin.com" in dom:
        return 4
    if dom_in(dom, ALL_VENUE):
        return 1
    if dom_in(dom, ALL_ASSOC) or dom_in(dom, ALL_MEDIA):
        return 2
    if dom_in(dom, VENDOR):
        return 3
    return 0


def slug(url: str) -> str:
    u = urlparse(url)
    return re.sub(r"[^a-z0-9]+", "-", (u.netloc + u.path).lower()).strip("-")[:80]


def chunks(xs, n):
    return [xs[i:i + n] for i in range(0, len(xs), n)]


def sites(ds) -> str:
    return "(" + " OR ".join(f"site:{d}" for d in ds) + ")"


SUBPAGE_PREFIXES = ("agenda", "speaker", "sponsor", "exhibitor", "register", "registration",
                    "ticket", "venue", "program", "schedule", "attend", "about", "faq", "contact",
                    "experience", "session", "hotel", "travel", "partners", "pricing", "visit",
                    "why-", "day-", "floorplan", "floor-plan", "news", "media", "gallery")


def root_url(url: str) -> str:
    """Event home page for a result link. Keeps file-style links (page.aspx?id=1) as they are
    and cuts known sub pages (agenda, speakers, sponsors ...) off event paths."""
    u = urlparse(url)
    dom = reg_domain(url)
    base = f"{u.scheme or 'https'}://{u.netloc}"
    if classify(dom) in (1, 2, 3, 5):  # shared hosts: keep a short path
        segs = [s for s in u.path.split("/") if s]
        if segs and "." in segs[-1]:   # a file such as event.html or Event_Display.aspx
            return base + u.path + (f"?{u.query}" if u.query else "")
        keep = []
        for s in segs:
            if s.lower().startswith(SUBPAGE_PREFIXES):
                break
            keep.append(s)
        return base + ("/" + "/".join(keep[:3]) + "/" if keep else "/")
    return base + "/"


def event_key(url: str) -> str:
    u = urlparse(root_url(url))
    return reg_domain(url) + u.path.rstrip("/") + (f"?{u.query}" if u.query else "")


JUNK_URL = re.compile(r"/(blogs?|news|newsroom|press|press-releases?|news-releases?|careers?|jobs?|"
                      r"podcasts?|webinars?|on-?demand|replay|articles?|videos?|resources?|ebooks?|"
                      r"analysts?|whitepapers?|case-stud(y|ies)|investor|search|support)/|\.pdf($|\?)",
                      re.I)
GENERIC_NAME = re.compile(r"^(events?|industry events?|what'?s on|home|upcoming events?|all events|"
                          r"event guide|calendar|conferences?|in-person conferences?|log|news|"
                          r"veranstaltungen|termine|kalender|startseite|referent(inn)?en|ausstellung|"
                          r"programm|speakers?|agenda)$|"
                          r"\bnews\b|feature articles|upcoming events", re.I)

# Places that show a page is about an event outside the US, UK and Germany.
FOREIGN = re.compile(
    r"\b(india|malaysia|malaysian|kuala lumpur|singapore|indonesia|jakarta|thailand|bangkok|vietnam|"
    r"philippines|china|shanghai|beijing|shenzhen|hong kong|japan|tokyo|korea|seoul|australia|"
    r"sydney|melbourne|gold coast|brisbane|new zealand|auckland|dubai|abu dhabi|saudi|riyadh|"
    r"qatar|doha|mena|middle east|africa|nigeria|kenya|zimbabwe|south africa|islamabad|pakistan|"
    r"karachi|lahore|brazil|mexico|canada|toronto|vancouver|latin america|apac|asia|anz|nz|"
    r"southeast asia|paris|madrid|barcelona|amsterdam|rotterdam|warsaw|milan|brussels|"
    r"stockholm|copenhagen)\b", re.I)
FOREIGN_URL = re.compile(r"india(?!na)|malaysia|singapore|china|japan|apac|mena|asia|australia|"
                         r"zealand|africa|dubai|jakarta|shanghai|islamabad|zimbabwe|pakistan|"
                         r"nigeria|kenya|ghana|egypt|nz(?=[/_.?-]|$)", re.I)
JUNK_TITLE = re.compile(r"\bwebinar\b|on[- ]demand|replay|podcast|stellenanzeige|whitepaper|"
                        r"\brecap\b|highlights|photos|\bjobs?\b", re.I)
LISTICLE = re.compile(r"\b(top|best)\s+\d+|\d+\s+(best|top)\b|events?\s+calendar|"
                      r"veranstaltungskalender|messekalender|trade shows? in\b", re.I)
EVENT_WORD = re.compile(r"conference|summit|symposium|forum|expo\b|congress|kongress|messe\b|tagung|"
                        r"gipfel|\bweek\b|\bevent|inspire|sapphire|radiance|connect|\blive\b|"
                        r"\bshow\b|\bfair\b|exchange|konferenz|treffen|tage\b", re.I)


def clean_title(t: str) -> str:
    t = re.split(r"\s[|•]\s|\s[-–—]\s", t)[0]
    t = re.sub(r"\b20\d\d\b", "", t)
    t = re.sub(r"\b(official (site|website)|tickets?|register now)\b", "", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" -|:,")


def name_tokens(name: str) -> Set[str]:
    return {w for w in re.findall(r"[a-zäöüß0-9]{4,}", name.lower())
            if w not in {"conference", "summit", "expo", "forum", "week", "annual", "event", "the",
                         "kongress", "messe", "with", "from", "and", "und"}}


# --------------------------------------------------------------------------------------
# dates
# --------------------------------------------------------------------------------------
_MONTHS = [("jan", "january", "januar"), ("feb", "february", "februar"),
           ("mar", "march", "märz", "maerz", "mär", "mrz"), ("apr", "april"),
           ("may", "mai"), ("jun", "june", "juni"), ("jul", "july", "juli"), ("aug", "august"),
           ("sep", "sept", "september"), ("oct", "october", "oktober", "okt"),
           ("nov", "november"), ("dec", "december", "dezember", "dez")]
MONTHS: Dict[str, int] = {}
for _i, _names in enumerate(_MONTHS, 1):
    for _n in _names:
        MONTHS[_n] = _i
MON = "|".join(sorted(MONTHS, key=len, reverse=True))
_D = r"(\d{1,2})(?:st|nd|rd|th)?\.?"
_SEP = r"\s*(?:-|–|—|to|bis|until)\s*"
_FLAGS = re.I
_Y = r"(20\d\d)"
DATE_PATTERNS = [
    # full date to full date: "15 Feb 2027 - 17 Feb 2027", "Feb 15, 2027 - Feb 17, 2027",
    # "Feb 15 - Feb 17, 2027"
    ("q1", re.compile(rf"\b{_D}\s+({MON})\.?,?\s+{_Y}{_SEP}{_D}\s+({MON})\.?,?\s+{_Y}\b", _FLAGS)),
    ("q2", re.compile(rf"\b({MON})\.?\s+{_D},?\s+{_Y}{_SEP}({MON})\.?\s+{_D},?\s+{_Y}\b", _FLAGS)),
    ("q3", re.compile(rf"\b({MON})\.?\s+{_D}{_SEP}({MON})\.?\s+{_D},?\s+{_Y}\b", _FLAGS)),
    ("p1", re.compile(rf"\b{_D}{_SEP}{_D}\s+({MON})\.?,?\s+(20\d\d)\b", _FLAGS)),
    ("p2", re.compile(rf"\b{_D}\s+({MON})\.?{_SEP}{_D}\s+({MON})\.?,?\s+(20\d\d)\b", _FLAGS)),
    ("p3", re.compile(rf"\b({MON})\.?\s+{_D}(?:{_SEP}{_D})?,?\s+(20\d\d)\b", _FLAGS)),
    ("p4", re.compile(rf"\b{_D}\s+({MON})\.?,?\s+(20\d\d)\b", _FLAGS)),
    ("p5", re.compile(r"\b(\d{1,2})\.\s*(?:-|–)\s*(\d{1,2})\.(\d{1,2})\.(20\d\d)\b")),
    ("p6", re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(20\d\d)\b")),
    ("p7", re.compile(r"\b(20\d\d)-(\d{2})-(\d{2})\b")),
]


def _mk(y, m, d) -> Optional[date]:
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


BAD_DATE_CTX = re.compile(
    r"early[- ]?bird|deadline|register by|registration (closes|ends)|prices? (increase|rise)|"
    r"\buntil\b|\bbis zum\b|submit|submission|posted|published|updated|©|copyright|call for|"
    r"offer ends|ends on|expires|valid (until|through)|save .{0,20}by", re.I)


def parse_dates_ctx(text: str):
    """All dates in the text as (position, start, end, looks_like_deadline_or_stamp)."""
    found, covered = [], []
    for name, rx in DATE_PATTERNS:
        for m in rx.finditer(text):
            s, e = m.span()
            if any(not (e <= a or s >= b) for a, b in covered):
                continue
            g = m.groups()
            try:
                if name == "q1":
                    a = _mk(g[2], MONTHS[g[1].lower()], g[0])
                    b = _mk(g[5], MONTHS[g[4].lower()], g[3])
                elif name == "q2":
                    a = _mk(g[2], MONTHS[g[0].lower()], g[1])
                    b = _mk(g[5], MONTHS[g[3].lower()], g[4])
                elif name == "q3":
                    a = _mk(g[4], MONTHS[g[0].lower()], g[1])
                    b = _mk(g[4], MONTHS[g[2].lower()], g[3])
                elif name == "p1":
                    mo = MONTHS[g[2].lower()]
                    a, b = _mk(g[3], mo, g[0]), _mk(g[3], mo, g[1])
                elif name == "p2":
                    a = _mk(g[4], MONTHS[g[1].lower()], g[0])
                    b = _mk(g[4], MONTHS[g[3].lower()], g[2])
                elif name == "p3":
                    mo = MONTHS[g[0].lower()]
                    a = _mk(g[3], mo, g[1])
                    b = _mk(g[3], mo, g[2]) if g[2] else a
                elif name == "p4":
                    a = b = _mk(g[2], MONTHS[g[1].lower()], g[0])
                elif name == "p5":
                    a, b = _mk(g[3], g[2], g[0]), _mk(g[3], g[2], g[1])
                elif name == "p6":
                    a = b = _mk(g[2], g[1], g[0])
                else:
                    a = b = _mk(g[0], g[1], g[2])
            except KeyError:
                continue
            if a and b and b >= a and (b - a).days <= 14:   # events are short; longer = two dates glued
                covered.append((s, e))
                found.append((s, e, a, b))
    found.sort(key=lambda x: x[0])
    out, prev_end = [], 0
    for s, e, a, b in found:
        # look only at the words just before this date: stop at the previous date or sentence
        ctx = text[max(prev_end, s - 50):s]
        ctx = re.split(r"[.;\n]", ctx)[-1]
        out.append((s, a, b, bool(BAD_DATE_CTX.search(ctx))))
        prev_end = e
    return out


def parse_dates(text: str) -> List[Tuple[date, date]]:
    return [(a, b) for _, a, b, _ in parse_dates_ctx(text)]


def pick_date(text: str):
    """Best guess for the event date. Returns (start, end, alternatives).
    Skips past dates, deadlines and 'updated on' stamps, prefers a multi-day range near the top."""
    items = [x for x in parse_dates_ctx(text)
             if x[2] >= TODAY and x[1].year <= FALLBACK_END.year + 1]
    if not items:
        return None, None, []
    good = [x for x in items if not x[3]] or items
    # The real event date is usually repeated on the page (header, buttons, footer).
    # So take the range that appears most often. Ties: multi-day first, then earliest on the page.
    counts: Dict[Tuple[date, date], int] = {}
    first_pos: Dict[Tuple[date, date], int] = {}
    for pos, x, y, _ in good:
        counts[(x, y)] = counts.get((x, y), 0) + 1
        first_pos.setdefault((x, y), pos)
    a, b = sorted(counts, key=lambda k: (-counts[k], k[0] == k[1], first_pos[k]))[0]
    alts: List[str] = []
    for _, x, y, _ in good:
        s = fmt_range(x, y)
        if (x, y) != (a, b) and s not in alts and len(alts) < 3:
            alts.append(s)
    return a, b, alts


def fmt_range(a: date, b: date) -> str:
    if a == b:
        return f"{a.day} {a.strftime('%b %Y')}"
    if a.year != b.year:
        return f"{a.day} {a.strftime('%b %Y')}-{b.day} {b.strftime('%b %Y')}"
    if a.month == b.month:
        return f"{a.day}-{b.day} {a.strftime('%b %Y')}"
    return f"{a.day} {a.strftime('%b')}-{b.day} {b.strftime('%b %Y')}"


def window_of(a: Optional[date]) -> str:
    if a is None:
        return "unknown"
    if a < TODAY:
        return "past"
    if a < WINDOW_START:
        return "before"
    if a <= CORE_END:
        return "core"
    if a <= FALLBACK_END:
        return "fallback"
    return "late"


# --------------------------------------------------------------------------------------
# page reading
# --------------------------------------------------------------------------------------
def page_title(raw: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    if not m:
        return ""
    return clean_title(re.sub(r"\s+", " ", htmllib.unescape(m.group(1))).strip())


def head_text(raw: str) -> str:
    """Title, meta descriptions and headings: where a page states its own event date.
    Menus and footers (which advertise other events) are not included."""
    parts = []
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    if m:
        parts.append(m.group(1))
    for tag in re.findall(r"<meta\b[^>]*>", raw, re.I):
        if re.search(r"(?:name|property)=[\"'](?:description|og:title|og:description|"
                     r"twitter:title|twitter:description)[\"']", tag, re.I):
            c = re.search(r"content=[\"'](.*?)[\"']", tag, re.S | re.I)
            if c:
                parts.append(c.group(1))
    for h in re.findall(r"<h1[^>]*>(.*?)</h1>", raw, re.S | re.I):
        parts.append(re.sub(r"<[^>]+>", " ", h))
    return re.sub(r"\s+", " ", htmllib.unescape(" . ".join(parts)))


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def json_ld_dates(raw: str) -> List[Tuple[date, date]]:
    """Future event dates from schema.org Event data (startDate / endDate) in the page."""
    out: List[Tuple[date, date]] = []
    for block in re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", raw, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for node in _walk(data):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if not any(isinstance(x, str) and x.endswith("Event") for x in types):
                continue
            s, e = str(node.get("startDate", ""))[:10], str(node.get("endDate", ""))[:10]
            m1 = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
            if not m1:
                continue
            a = _mk(*m1.groups())
            m2 = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", e)
            b = _mk(*m2.groups()) if m2 else a
            if a and b and b >= a and b >= TODAY and (a, b) not in out:
                out.append((a, b))
    return out


def links_under(links, base_url: str):
    """Keep only links that sit inside this event's own path (drops site-wide menus)."""
    bu = urlparse(base_url)
    if bu.path in ("", "/"):
        return links
    keep = []
    for label, href in links:
        full = urlparse(urljoin(base_url, href or ""))
        if full.netloc == bu.netloc and full.path.startswith(bu.path):
            keep.append((label, href))
    return keep


def html_to_text_links(raw: str):
    if BeautifulSoup:
        soup = BeautifulSoup(raw, "html.parser")
        for t in soup(["script", "style", "noscript", "svg"]):
            t.decompose()
        links = [(a.get_text(" ", strip=True), a.get("href", "")) for a in soup.find_all("a")]
        return soup.get_text("\n", strip=True), links
    links = [(re.sub(r"<[^>]+>", " ", t).strip(), h) for h, t in
             re.findall(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', raw, re.S | re.I)]
    body = re.sub(r"<(script|style|noscript)[\s\S]*?</\1>", " ", raw, flags=re.I)
    body = re.sub(r"<[^>]+>", "\n", body)
    body = htmllib.unescape(body)
    return re.sub(r"\n\s*\n+", "\n", body), links


PITCH = re.compile(r"become|why |prospectus|opportunit|book (a )?(stand|booth)|werden|exhibit at|"
                   r"sponsor(ing)? (us|packages?)|apply|call for|submit|media kit|mediadaten|"
                   r"standbuchung|ausstellen|sponsorship|partner with|"
                   r"exhibitor (services|info|information|manual|kit|resources|portal|login|faq)|"
                   r"sponsor (info|information|login|portal|faq|services)", re.I)
KINDS = {
    "speakers": re.compile(r"speaker|referent|sprecher|faculty", re.I),
    "exhibitors": re.compile(r"exhibitor|aussteller|expo (hall|floor)|floor ?plan|hallenplan", re.I),
    "sponsors": re.compile(r"sponsor|\bpartners?\b|partner\b", re.I),
}
TBA = re.compile(r"(speakers?|sponsors?|exhibitors?|agenda|referenten|aussteller).{0,80}"
                 r"(to be announced|coming soon|\btba\b|will be announced|wird bekannt|in kürze|"
                 r"stay tuned)", re.I | re.S)
INVITE = re.compile(r"by invitation only|invitation[- ]only|invite[- ]only|nur auf einladung|"
                    r"geschlossene veranstaltung", re.I)


def detect_lists(links, text):
    """Returns (kinds dict kind->evidence, invite_only, tba)."""
    kinds: Dict[str, str] = {}
    for label, href in links:
        href = (href or "").strip()
        if not href or href.startswith(("#", "javascript", "mailto:", "tel:")):
            continue   # not a real page
        blob = f"{label} {href}"
        if PITCH.search(blob):
            continue
        for k, rx in KINDS.items():
            if k not in kinds and (rx.search(label) or rx.search(href.split("?")[0])):
                kinds[k] = (label or href)[:60] + " -> " + href[:90]
    if not links:  # pasted text: look for the words, ignoring lines that are sales pitches
        clean = "\n".join(l for l in text.splitlines() if not PITCH.search(l))
        for k, rx in KINDS.items():
            if rx.search(clean):
                kinds[k] = "word in pasted text"
    return kinds, bool(INVITE.search(text[:6000])), bool(TBA.search(text))


def find_city(text: str, market: str) -> Tuple[str, str]:
    """Returns (city, market_detected)."""
    head = text[:6000]
    best = ("", 0, market)
    for m, cities in CITIES.items():
        hits = []
        for c in cities:
            i = head.find(c)
            if i >= 0:
                hits.append((i, c))
        score = len(hits) + (2 if re.search(COUNTRY_WORDS[m], head, re.I) else 0)
        if hits and score > best[1]:
            best = (min(hits)[1], score, m)
    return best[0], best[2]


NUM = r"\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?|\d+"
PRICE = re.compile(rf"(?:[€$£]\s?({NUM})|({NUM})\s?(?:EUR|USD|GBP|[€$£]))")
FREE = re.compile(r"free (entry|admission|to attend|registration)|kostenlos|eintritt frei", re.I)


def to_num(s: str) -> Optional[float]:
    s = re.sub(r"[^\d.,]", "", s)
    if not s:
        return None
    m = re.search(r"[.,](\d{1,2})$", s)
    if m:
        head, dec = s[:m.start()], m.group(1)
    else:
        head, dec = s, "0"
    head = re.sub(r"[.,]", "", head)
    return float(f"{head}.{dec}") if head else None


def find_price(text: str) -> Tuple[Optional[float], bool]:
    if FREE.search(text):
        return 0.0, True
    prices = []
    for m in PRICE.finditer(text):
        ctx = text[max(0, m.start() - 120): m.end() + 60].lower()
        if re.search(r"sponsor|exhibit|stand|booth|aussteller|prospectus", ctx):
            continue
        if re.search(r"ticket|registration|pass|price|fee|early bird|teilnahme|preis|delegate", ctx):
            v = to_num(m.group(1) or m.group(2))
            if v and v >= 50:
                prices.append(v)
    return (min(prices), False) if prices else (None, False)


# --------------------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------------------
def group_hits(text: str, groups: Dict[str, str]) -> List[str]:
    t = text[:30000]
    return [g for g, rx in groups.items() if re.search(rx, t, re.I)]


def score_cand(c: "Cand") -> None:
    b = group_hits(c.text, BUYER)
    i = group_hits(c.text, ICP)
    # lead data access
    if c.invite_only or c.list_flag == "N":
        lead = 0
    elif c.list_flag == "Y":
        has_s, has_x = "speakers" in c.kinds, bool({"exhibitors", "sponsors"} & c.kinds)
        lead = 5 if (has_s and has_x) else (4 if "exhibitors" in c.kinds else 3)
    else:
        lead = 1
    # timing
    days = (c.start - TODAY).days if c.start else None
    win = window_of(c.start)
    if win == "core":
        timing = 5 if days >= 21 else (3 if days >= 14 else 1)
    elif win == "fallback":
        timing = 3 if c.start.month == 1 else 2
    else:
        timing = 0
    # cost
    if c.cost is None:
        cost, c.cost_known = 2, False
    else:
        c.cost_known = True
        p = c.cost
        cost = 5 if p <= 250 else 4 if p <= 750 else 3 if p <= 1500 else 2 if p <= 3000 else 1
    c.scores = {"buyer_density": min(5, len(b)), "icp_match": min(5, len(i)),
                "lead_access": lead, "timing": timing, "cost": cost}
    c.total = (3 * c.scores["buyer_density"] + 3 * c.scores["icp_match"] + 2 * lead + timing + cost)
    c.evidence = f"buyer:{'+'.join(b) or '-'} | icp:{'+'.join(i) or '-'}"
    c.window = win


# --------------------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------------------
@dataclass
class Hit:
    market: str
    qtype: int
    title: str
    link: str
    snippet: str
    query: str


@dataclass
class Cand:
    market: str
    name: str
    url: str
    domain: str
    types: Set[int] = field(default_factory=set)
    snippets: List[str] = field(default_factory=list)
    official_url: str = ""
    conf: str = "organiser"           # organiser | resolved | listing
    listing_url: str = ""
    start: Optional[date] = None
    end: Optional[date] = None
    date_alt: List[str] = field(default_factory=list)
    date_src: str = ""
    city: str = ""
    list_flag: str = "?"
    list_evidence: str = ""
    kinds: Set[str] = field(default_factory=set)
    invite_only: bool = False
    tba: bool = False
    fetched: bool = False
    manual: bool = False
    foreign: bool = False
    blocked_why: str = ""
    text: str = ""
    cost: Optional[float] = None
    cost_known: bool = False
    scores: Dict[str, int] = field(default_factory=dict)
    total: int = 0
    evidence: str = ""
    window: str = ""
    eligible: bool = False
    reasons: List[str] = field(default_factory=list)
    status: str = ""


# --------------------------------------------------------------------------------------
# Serper
# --------------------------------------------------------------------------------------
class BudgetExceeded(Exception):
    pass


class Serper:
    URL = "https://google.serper.dev/search"

    def __init__(self, key: str, cache_dir: Path, max_queries: int, use_cache: bool = True):
        self.key, self.cache_dir, self.max, self.use_cache = key, cache_dir, max_queries, use_cache
        self.used = 0
        self.log: List[dict] = []
        cache_dir.mkdir(parents=True, exist_ok=True)

    def search(self, q: str, gl: str, hl: str, page: int = 1, num: int = 10) -> dict:
        ck = hashlib.sha1(json.dumps([q, gl, hl, page, num]).encode()).hexdigest()
        cf = self.cache_dir / f"{ck}.json"
        if self.use_cache and cf.exists():
            self.log.append({"q": q, "cached": True})
            return json.loads(cf.read_text(encoding="utf-8"))
        if self.used >= self.max:
            raise BudgetExceeded(f"query budget {self.max} reached")
        payload = {"q": q, "gl": gl, "hl": hl, "num": num, "page": page, "autocorrect": False}
        data, last = None, ""
        for attempt in range(4):
            try:
                r = requests.post(self.URL, json=payload, timeout=30,
                                  headers={"X-API-KEY": self.key, "Content-Type": "application/json"})
            except requests.RequestException as e:
                last = type(e).__name__
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (401, 403):
                sys.exit(f"Serper rejected the key (HTTP {r.status_code}). "
                         f"Check SERPER_API_KEY.")
            if r.status_code >= 400:
                body = r.text[:200]
                if "credit" in body.lower():
                    sys.exit(f"Serper says you are out of credits: {body}")
                print(f"[warn] Serper HTTP {r.status_code}, query skipped: {body}")
                self.log.append({"q": q, "failed": f"HTTP {r.status_code}"})
                return {}
            data = r.json()
            break
        if data is None:
            print(f"[warn] Serper query failed after retries ({last}), skipped")
            self.log.append({"q": q, "failed": last})
            return {}
        self.used += 1
        cf.write_text(json.dumps(data), encoding="utf-8")
        self.log.append({"q": q, "cached": False, "results": len(data.get("organic", []))})
        return data


def build_queries(markets: List[str]):
    out = []
    yr = "(2026 OR 2027)"
    for m in markets:
        # one site per query for venues and associations: busy sites then cannot crowd out the rest
        for ch in chunks(VENUE[m], 1):
            out.append((m, 1, f"{sites(ch)} {KW[m]} {EVW[m]} {yr}"))
        for ch in chunks(ASSOC[m], 1) + chunks(MEDIA[m], 3):
            out.append((m, 2, f"{sites(ch)} {KW[m]} {EVW[m]} {yr}"))
        for ch in chunks(VENDOR, 4):
            out.append((m, 3, f"{sites(ch)} (conference OR summit OR \"customer event\" OR Kongress) "
                              f"{GEO[m]} {yr}"))
        out.append((m, 5, f"{sites(AGG[:3])} {KW[m]} {GEO[m]} {yr}"))
        out.append((m, 5, f"{sites(AGG[3:6])} {KW[m]} {GEO[m]} {yr}"))
    if COMPANY_NAME:
        out.append(("ALL", 4, f'"{COMPANY_NAME}" (event OR conference OR summit OR Kongress) site:linkedin.com'))
    return out


def gather(serper, markets, pages: int) -> List[Hit]:
    hits: List[Hit] = []
    qs = build_queries(markets)
    for i, (m, qt, q) in enumerate(qs, 1):
        print(f"[search {i}/{len(qs)}] {m} source type {qt}", flush=True)
        gl, hl = SERPER_LOCALE.get(m, ("us", "en"))
        for p in range(1, pages + 1):
            try:
                data = serper.search(q, gl, hl, page=p)
            except BudgetExceeded as e:
                print(f"[stop] {e}")
                return hits
            for r in data.get("organic", []):
                hits.append(Hit(m, qt, r.get("title", ""), r.get("link", ""),
                                r.get("snippet", ""), q))
    return hits


# --------------------------------------------------------------------------------------
# candidates
# --------------------------------------------------------------------------------------
def build_cands(hits: List[Hit]):
    cands: Dict[str, Cand] = {}
    hints: List[Tuple[str, str, int]] = []  # name, market, source type (aggregators only)
    li: List[dict] = []                      # LinkedIn results that mention COMPANY_NAME, for you to read
    for h in hits:
        dom = reg_domain(h.link)
        t = classify(dom)
        if t == -1 or not h.link:
            continue
        title = clean_title(h.title)
        blob = f"{h.title} {h.snippet} {h.link}"
        if JUNK_URL.search(h.link) or JUNK_TITLE.search(h.title) or LISTICLE.search(h.title):
            continue
        if t == 4:
            if COMPANY_NAME and COMPANY_NAME.lower() in blob.lower() and h.link not in {x["URL"] for x in li}:
                li.append({"Title": h.title, "Snippet": h.snippet, "URL": h.link})
            continue
        if not re.search(r"2026|2027", blob) and t not in (2, 3):
            continue
        if not EVENT_WORD.search(blob):
            continue
        if t == 5:
            hints.append((title, h.market, t))
            continue
        key = event_key(h.link)
        c = cands.get(key)
        if not c:
            c = cands[key] = Cand(h.market, title, h.link, dom)
        c.types.add(t or h.qtype)
        c.snippets.append(f"{h.title}. {h.snippet}")
        if len(title) > len(c.name) and len(title) < 90:
            c.name = title
        if c.market == "ALL" and h.market != "ALL":
            c.market = h.market
    for c in cands.values():
        c.official_url = root_url(c.url)
        if classify(c.domain) == 1 or dom_in(c.domain, ALL_MEDIA):
            c.conf, c.listing_url = "listing", c.url
    return cands, hints, li


def dedupe(cands: Dict[str, Cand]) -> List[Cand]:
    """Merge candidates that point to the same organiser page."""
    merged: Dict[str, Cand] = {}
    for c in cands.values():
        key = event_key(c.official_url)
        m = merged.get(key)
        if m is None:
            merged[key] = c
            continue
        m.types |= c.types
        m.snippets += c.snippets
        if m.conf == "listing" and c.conf != "listing":
            m.conf = c.conf
        m.listing_url = m.listing_url or c.listing_url
    return list(merged.values())


def priority(c: Cand) -> int:
    """Higher = open first (matters when --max-fetch is smaller than the candidate list)."""
    blob = " ".join(c.snippets)
    s = len(c.types)
    if re.search(r"2026|2027", blob):
        s += 1
    if re.search(f"{BUYER['procurement']}|{BUYER['supply_chain']}|{BUYER['finance_ops']}", blob, re.I):
        s += 1
    if c.conf != "listing":
        s += 1
    return s


def resolve(serper, cands: Dict[str, Cand], hints, max_resolve: int):
    """Find organiser pages for venue/media listings and for aggregator/LinkedIn names."""
    todo: List[Tuple[str, str, str, Optional[Cand]]] = []
    for c in cands.values():
        if c.conf == "listing" and name_tokens(c.name) and not GENERIC_NAME.search(c.name):
            todo.append((c.name, c.market, c.domain, c))
    seen_names = [name_tokens(c.name) for c in cands.values()]
    for name, m, t in hints:
        tk = name_tokens(name)
        if not tk or GENERIC_NAME.search(name):
            continue
        if any(len(tk & s) / max(1, len(tk | s)) >= 0.5 for s in seen_names):
            continue  # already have this event
        seen_names.append(tk)
        todo.append((name, m, "", None))
    done = 0
    for name, m, dom, cand in todo:
        if done >= max_resolve:
            break
        gl, hl = SERPER_LOCALE.get(m if m in SERPER_LOCALE else "US")
        q = f'"{name}" (2026 OR 2027) official' + (f" -site:{dom}" if dom else "")
        try:
            data = serper.search(q, gl, hl)
        except BudgetExceeded:
            break
        done += 1
        tk = name_tokens(name)
        for r in data.get("organic", []):
            d = reg_domain(r.get("link", ""))
            t = classify(d)
            if t in (-1, 1, 4, 5) or dom_in(d, ALL_MEDIA) or d == dom:
                continue
            blob = f"{r.get('title', '')} {r.get('link', '')}".lower()
            if tk and sum(1 for w in tk if w in blob) / len(tk) < 0.6:
                continue   # result must share most of the event name, not just one word
            if JUNK_URL.search(r["link"]):
                continue
            key = event_key(r["link"])
            target = cand or cands.get(key)
            if target is None:
                target = cands[key] = Cand(m, clean_title(r.get("title", "")) or name, r["link"], d)
                target.snippets.append(f"{r.get('title', '')}. {r.get('snippet', '')}")
            target.official_url = root_url(r["link"])
            target.conf = "resolved"
            target.types.add(5 if cand is None else 1)
            break
    return cands


# --------------------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------------------
NOT_OPENED = "not opened (--no-verify or fetch limit reached)"
BLOCK_TEXT = re.compile(r"just a moment|cf-chl|enable javascript and cookies|verify you are human|"
                        r"access denied|captcha|performing security verification", re.I)


class Fetcher:
    UA = "Mozilla/5.0 (compatible; EventResearch/0.1; single-page check for a case study)"

    def __init__(self, manual_dir: Path, delay: float = 1.5):
        self.manual_dir, self.delay = manual_dir, delay
        self.session = requests.Session()
        self.session.headers["User-Agent"] = self.UA
        self.robots: Dict[str, Optional[robotparser.RobotFileParser]] = {}
        self.last: Dict[str, float] = {}
        self.memo: Dict[str, dict] = {}
        manual_dir.mkdir(parents=True, exist_ok=True)

    def _allowed(self, url: str) -> bool:
        u = urlparse(url)
        base = f"{u.scheme}://{u.netloc}"
        if base not in self.robots:
            rp = robotparser.RobotFileParser()
            try:
                r = self.session.get(base + "/robots.txt", timeout=10)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except requests.RequestException:
                rp.parse([])
            self.robots[base] = rp
        return self.robots[base].can_fetch(self.UA, url)

    def get(self, url: str) -> dict:
        if url not in self.memo:
            self.memo[url] = self._get(url)
        return self.memo[url]

    def _get(self, url: str) -> dict:
        mp = self.manual_dir / f"{slug(url)}.txt"
        if mp.exists():
            return {"ok": True, "manual": True, "status": 200, "why": "",
                    "html": mp.read_text(encoding="utf-8-sig", errors="replace")}
        dom = reg_domain(url)
        wait = self.delay - (time.time() - self.last.get(dom, 0))
        if wait > 0:
            time.sleep(wait)
        self.last[dom] = time.time()
        if not self._allowed(url):
            return {"ok": False, "why": "robots.txt disallows"}
        try:
            r = self.session.get(url, timeout=20, allow_redirects=True)
        except requests.RequestException as e:
            return {"ok": False, "why": f"request failed: {type(e).__name__}"}
        if r.status_code in (401, 403, 429, 503):
            return {"ok": False, "why": f"HTTP {r.status_code} (bot protection or login)"}
        if r.status_code >= 400:
            return {"ok": False, "why": f"HTTP {r.status_code}"}
        ctype = r.headers.get("Content-Type", "").lower()
        if ctype and "html" not in ctype and "text" not in ctype:
            return {"ok": False, "why": f"not a web page ({ctype.split(';')[0]})"}
        if r.encoding is None or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding or "utf-8"   # keeps German umlauts intact
        txt = r.text
        if len(txt) < 800 or (len(txt) < 30000 and BLOCK_TEXT.search(txt[:3000])):
            return {"ok": False, "why": "bot check or empty page"}
        return {"ok": True, "manual": False, "html": txt, "status": r.status_code, "why": ""}


def enrich(c: Cand, fetcher: Optional[Fetcher]) -> None:
    snippet_text = " ".join(c.snippets)
    page = fetcher.get(c.official_url) if fetcher else {"ok": False, "why": NOT_OPENED}
    if page["ok"]:
        raw = page["html"]
        text, links = (raw, []) if page.get("manual") else html_to_text_links(raw)
        c.fetched, c.manual, c.text = True, bool(page.get("manual")), text
        n_links = len(links)
        links = links_under(links, c.official_url)   # ignore site-wide menus on shared hosts
        kinds, c.invite_only, c.tba = detect_lists(links, text)
        c.kinds = set(kinds)
        c.list_evidence = "; ".join(f"{k}: {v}" for k, v in kinds.items())
        if c.invite_only:
            c.list_flag, c.list_evidence = "N", "invitation only"
        elif kinds and not c.tba:
            c.list_flag = "Y"
        elif kinds and c.tba:
            c.list_flag, c.list_evidence = "?", "list link present but page says TBA; " + c.list_evidence
        elif c.tba:
            c.list_flag, c.list_evidence = "N", "page says speakers/sponsors to be announced"
        elif not page.get("manual") and n_links < 12:
            c.list_flag = "?"
            c.list_evidence = f"only {n_links} links in the page HTML (JavaScript menu?), check by hand"
        else:
            c.list_flag, c.list_evidence = "N", "no speaker, sponsor or exhibitor link found"
        # Date: trust the page's own statement first (structured data, title, headings).
        # Dates found only in body text can belong to other events in menus, so they stay unverified.
        body_alts: List[str] = []
        for _, x, y, bad in parse_dates_ctx(text):
            s = fmt_range(x, y)
            if y >= TODAY and not bad and s not in body_alts:
                body_alts.append(s)
        body_alts = body_alts[:5]
        a = b = None
        c.date_src = ""
        ld = [] if page.get("manual") else json_ld_dates(raw)
        if len(ld) == 1:
            (a, b), c.date_src = ld[0], "structured"
        if a is None and not page.get("manual"):
            a, b, _ = pick_date(head_text(raw))
            c.date_src = "title" if a else ""
        if a is None:
            a, b, _ = pick_date(text)
            c.date_src = ("pasted" if page.get("manual") else "body") if a else ""
        if a is None:  # last resort: the search snippet
            a, b, _ = pick_date(snippet_text)
            c.date_src = "snippet" if a else ""
        alts = [s for s in body_alts if not a or s != fmt_range(a, b)]
        c.start, c.end, c.date_alt = a, b, alts
        c.cost, _ = find_price(text)
        c.city, mk = find_city(text, c.market)
        if mk != c.market and c.market != "ALL":
            c.reasons.append(f"market looks like {mk}, searched as {c.market}")
        c.market = mk if mk in QUOTA else c.market
        if not page.get("manual"):   # search-result titles are often generic ("Home", "Events")
            t = page_title(raw)
            if t and 6 <= len(t) <= 90 and not GENERIC_NAME.search(t) and \
                    (GENERIC_NAME.search(c.name) or len(c.name) < 6):
                c.name = t
        # only the page's own title and headings count here, not menus that list other countries
        own = c.name + " " + (head_text(raw) if not page.get("manual") else "")
        c.foreign = bool(FOREIGN.search(own) or FOREIGN_URL.search(c.official_url))
    else:
        c.blocked_why = page["why"]
        c.text = snippet_text
        a, b, alts = pick_date(snippet_text)
        c.start, c.end, c.date_alt, c.date_src = a, b, alts, ("snippet" if a else "")
        c.city, mk = find_city(snippet_text, c.market)
        c.market = mk if mk in QUOTA else c.market
        c.foreign = bool(FOREIGN.search(c.name) or FOREIGN_URL.search(c.official_url)
                         or (not c.city and FOREIGN.search(snippet_text)))


def apply_filters(c: Cand, markets: List[str]) -> None:
    c.reasons = [r for r in c.reasons if r.startswith("market looks")]
    if c.market not in markets:
        c.reasons.append("market not targeted")
    if c.foreign:
        c.reasons.append("location looks outside US, UK and Germany")
    if c.start is None:
        c.reasons.append("no future date found")
    elif c.window not in ("core", "fallback"):
        c.reasons.append(f"date outside window ({c.window})")
    need = 2 if c.fetched else 1
    if c.scores.get("buyer_density", 0) < need:
        c.reasons.append("audience not clearly procurement/supply chain/finance ops/SAP")
    if c.list_flag == "N":
        c.reasons.append("no public speaker/sponsor/exhibitor list")
    hard = [r for r in c.reasons if not r.startswith("market looks")]
    c.eligible = not hard
    # VERIFIED only when the page itself states the date (structured data, title or heading),
    # a list link exists, the URL is the organiser page and the name is not a generic page title.
    verified = (c.fetched and c.date_src in ("structured", "title") and c.list_flag == "Y"
                and c.conf != "listing" and not GENERIC_NAME.search(c.name))
    c.status = "VERIFIED" if verified else "NEEDS_CHECK"
    if c.eligible and not verified:
        why = []
        if not c.fetched:
            why.append(f"page not opened: {c.blocked_why}")
        if c.fetched and c.date_src not in ("structured", "title"):
            why.append(f"date came from {c.date_src or 'nowhere'}, not the page title or data: "
                       f"check the date by hand")
        if GENERIC_NAME.search(c.name):
            why.append("event name is a generic page title")
        if c.list_flag == "?":
            why.append("list not confirmed")
        if c.conf == "listing":
            why.append("organiser page not found, URL is a venue/media listing")
        c.reasons.append("; ".join(why))


# --------------------------------------------------------------------------------------
# select + write
# --------------------------------------------------------------------------------------
def select(cands: List[Cand], quota: Dict[str, int], fill: bool):
    elig = sorted([c for c in cands if c.eligible], key=lambda c: (-c.total, c.start or date.max))
    picked: List[Cand] = []
    for m, n in quota.items():
        picked += [c for c in elig if c.market == m][:n]
    short = {m: n - sum(1 for c in picked if c.market == m) for m, n in quota.items()}
    if fill:
        need = sum(v for v in short.values() if v > 0)
        rest = [c for c in elig if c not in picked]
        picked += rest[:need]
    return picked, short


def foundvia(c: Cand) -> str:
    return "; ".join(SRC_LABEL[t] for t in sorted(c.types) if t in SRC_LABEL) or "web"


def row(c: Cand) -> dict:
    return {
        "Event": c.name,
        "Date": fmt_range(c.start, c.end) if c.start else "",
        "City": c.city,
        "Official URL": c.official_url,
        "Public speaker or exhibitor list (Y/N)": c.list_flag,
        "Found via": foundvia(c),
        "Market": c.market, "Window": c.window, "Score (max 50)": c.total,
        "Status": c.status,
        "Buyer / ICP / Lead / Timing / Cost":
            "/".join(str(c.scores.get(k, "")) for k in
                     ("buyer_density", "icp_match", "lead_access", "timing", "cost")),
        "Cost seen": ("" if c.cost is None else ("free" if c.cost == 0 else f"from {c.cost:g}")),
        "List evidence": c.list_evidence,
        "Audience evidence": c.evidence,
        "Other dates seen": " | ".join(c.date_alt),
        "Date source": c.date_src, "Official URL confidence": c.conf,
        "Listing / source page": c.listing_url or c.url,
        "Notes": " ; ".join(c.reasons),
    }


def write_csv(path: Path, rows: List[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def run_pipeline(args, serper, fetcher):
    markets = args.markets
    hits = gather(serper, markets, args.pages)
    print(f"[search done] {len(hits)} results", flush=True)
    cands, hints, li = build_cands(hits)
    cands = resolve(serper, cands, hints, args.max_resolve)
    lst = sorted(dedupe(cands), key=lambda c: (-priority(c), c.official_url))
    to_open = min(len(lst), args.max_fetch) if fetcher else 0
    print(f"[candidates] {len(lst)}; opening up to {to_open} organiser pages "
          f"(about {to_open * 2 // 60 + 1} min)", flush=True)
    opened = 0
    for c in lst:
        if fetcher and opened < args.max_fetch:
            opened += 1
            print(f"[open {opened}/{to_open}] {c.official_url}", flush=True)
            enrich(c, fetcher)
        else:
            enrich(c, None)
        score_cand(c)
        apply_filters(c, markets)
    picked, short = select(lst, args.quota, args.fill)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "shortlist.csv", [row(c) for c in picked])
    allrows = [row(c) for c in sorted(lst, key=lambda c: (-int(c.eligible), -c.total))]
    write_csv(out / "candidates_all.csv", allrows)
    manual = [{"Official URL": c.official_url, "Why": c.blocked_why,
               "Create file": f"manual/{slug(c.official_url)}.txt"}
              for c in lst if c.blocked_why and c.blocked_why != NOT_OPENED]
    write_csv(out / "manual_needed.csv", manual)
    write_csv(out / "linkedin_hints.csv", li)
    (out / "queries_log.json").write_text(json.dumps(serper.log, indent=1), encoding="utf-8")
    return lst, picked, short, manual


def report(lst, picked, short, manual, serper, out_dir="out") -> None:
    unopened = sum(1 for c in lst if c.blocked_why == NOT_OPENED)
    print(f"\nSerper queries used this run: {serper.used} (cached results are free)")
    print(f"Candidates: {len(lst)} | eligible: {sum(c.eligible for c in lst)} | "
          f"pages needing your pasted text: {len(manual)} | not opened: {unopened}\n")
    print(f"{'Mkt':<4}{'Score':<6}{'Date':<18}{'City':<14}{'List':<5}{'Status':<12}Event / URL")
    for c in sorted(picked, key=lambda c: (c.market, -c.total)):
        d = fmt_range(c.start, c.end) if c.start else ""
        print(f"{c.market:<4}{c.total:<6}{d:<18}{c.city[:12]:<14}{c.list_flag:<5}{c.status:<12}"
              f"{c.name[:50]}")
        print(f"{'':<49}{c.official_url}")
    for m, n in short.items():
        if n > 0:
            print(f"\n[note] {m}: {n} short of quota. Use --fill to backfill from other markets, "
                  f"or widen the window at the top of the file.")
    folder = Path(out_dir).resolve()
    print(f"\nFiles are in: {folder}")
    print("  shortlist.csv, candidates_all.csv, manual_needed.csv, linkedin_hints.csv")
    print("Scores are a draft from page text. Open the top URLs and check date and lists by hand.")


# --------------------------------------------------------------------------------------
# main + selftest
# --------------------------------------------------------------------------------------
def load_key() -> str:
    """Order: SERPER_KEY above (keep it empty), then environment variable, then .env file."""
    key = SERPER_KEY.strip()
    if key and not key.startswith("PASTE_YOUR"):
        return key
    key = os.environ.get("SERPER_API_KEY", "").strip()
    envf = Path(".env")
    if not key and envf.exists():
        for line in envf.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("SERPER_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"\'')
    return key


def parse_quota(items: List[str]) -> Dict[str, int]:
    q = dict(QUOTA)
    for it in items or []:
        k, v = it.split("=")
        q[k.upper()] = int(v)
    return q


def selftest() -> None:
    global TODAY
    TODAY = date(2026, 10, 31)
    cases = {
        "Feb 15 – Feb 17, 2027": (date(2027, 2, 15), date(2027, 2, 17)),
        "15 Feb 2027 - 17 Feb 2027": (date(2027, 2, 15), date(2027, 2, 17)),
        "February 15, 2027 – February 17, 2027": (date(2027, 2, 15), date(2027, 2, 17)),
        "30 Nov - 2 Dec 2026": (date(2026, 11, 30), date(2026, 12, 2)),
        "Radiance 15–17 Feb 2027": (date(2027, 2, 15), date(2027, 2, 17)),
        "March 8 - 11, 2027": (date(2027, 3, 8), date(2027, 3, 11)),
        "29 Nov - 1 Dec 2026": (date(2026, 11, 29), date(2026, 12, 1)),
        "16.-18. März 2027": (date(2027, 3, 16), date(2027, 3, 18)),
        "17. – 19. November 2026": (date(2026, 11, 17), date(2026, 11, 19)),
        "am 18.11.2026 in Berlin": (date(2026, 11, 18), date(2026, 11, 18)),
        "10th January 2027": (date(2027, 1, 10), date(2027, 1, 10)),
        "2027-02-08": (date(2027, 2, 8), date(2027, 2, 8)),
    }
    for text, want in cases.items():
        got = parse_dates(text)
        assert got and got[0] == want, (text, got)
    a, b, alts = pick_date("Last year 8-10 Sep 2025. Next: 8-10 Feb 2027 and 15 Mar 2027")
    assert a == date(2027, 2, 8) and alts == ["15 Mar 2027"], (a, alts)
    # deadlines and "updated" stamps must not be taken as the event date
    a, b, _ = pick_date("Last updated 2 Nov 2026. Early bird until 13 Nov 2026. SSOW: 8-11 Mar 2027")
    assert (a, b) == (date(2027, 3, 8), date(2027, 3, 11)), (a, b)
    a, b, _ = pick_date("Register by 20 Nov 2026 for the one-day expo on 3 Dec 2026")
    assert a == date(2026, 12, 3), a
    assert to_num("2,199.00") == 2199.0 and to_num("2.400") == 2400.0 and to_num("49,50") == 49.5
    assert find_price("Main Conference pass registration $2,199.00 early bird")[0] == 2199.0
    assert find_price("Become a sponsor from $25,000. Free entry for members")[0] == 0.0
    assert fmt_range(date(2026, 11, 29), date(2026, 12, 1)) == "29 Nov-1 Dec 2026"
    assert fmt_range(date(2027, 2, 15), date(2027, 2, 17)) == "15-17 Feb 2027"
    assert fmt_range(date(2027, 2, 15), date(2027, 2, 15)) == "15 Feb 2027"
    assert window_of(date(2026, 11, 18)) == "core" and window_of(date(2027, 1, 12)) == "fallback"
    assert window_of(date(2027, 3, 8)) == "late" and window_of(date(2026, 11, 3)) == "before"
    html = ('<nav><a href="/speakers">Speakers</a><a href="/sponsors">Sponsors</a>'
            '<a href="/become-a-sponsor">Become a sponsor</a></nav>'
            '<p>Procurement and supply chain leaders. SAP. Registration $1,200.</p>')
    text, links = html_to_text_links(html)
    kinds, inv, tba = detect_lists(links, text)
    assert set(kinds) == {"speakers", "sponsors"} and not inv and not tba, kinds
    # same page parsed without BeautifulSoup must give the same links
    global BeautifulSoup
    saved_bs, BeautifulSoup = BeautifulSoup, None
    try:
        _, links2 = html_to_text_links(html)
    finally:
        BeautifulSoup = saved_bs
    assert [h for _, h in links2] == [h for _, h in links], (links, links2)
    # pasted text: sales pitch lines must not count as a list
    k2, _, _ = detect_lists([], "Become a sponsor today\nProcurement leaders meet here")
    assert not k2, k2
    k3, _, _ = detect_lists([], "Confirmed speakers\nJane Doe, CPO")
    assert set(k3) == {"speakers"}, k3
    assert detect_lists([], "This is an invitation-only summit.")[1] is True
    assert detect_lists([], "Follow us. Sponsors and speakers folgen bald.")[2] is False
    assert find_city("Join us in Orlando, FL. United States", "UK")[1] == "US"
    assert classify("events.iofm.com") == 0 or classify("events.iofm.com") == 2
    assert classify("10times.com") == 5 and classify("www.excel.london") == 1
    assert root_url("https://www.ssonetwork.com/events-shared-services-week/agenda") == \
        "https://www.ssonetwork.com/events-shared-services-week/"
    assert root_url("https://www.ssonetwork.com/events-shared-services-week/agenda-mc/") == \
        "https://www.ssonetwork.com/events-shared-services-week/"
    # file-style links must not get a trailing slash and must keep their query
    assert root_url("https://messe-muenchen.de/de/veranstaltungen/transport-logistic-india-2026.html") \
        == "https://messe-muenchen.de/de/veranstaltungen/transport-logistic-india-2026.html"
    assert root_url("https://cscmp.org/CSCMP/CSCMP/Event_Display.aspx?EventKey=CHR260917") == \
        "https://cscmp.org/CSCMP/CSCMP/Event_Display.aspx?EventKey=CHR260917"
    assert classify("eventbrite.co.uk") == -1 and classify("sandiego.gov") == -1
    # the date repeated most often on a page wins over a one-off earlier range
    a, b, _ = pick_date("GBS Select 22-23 Feb 2027. SSOW 8-11 Mar 2027. Join SSOW 8-11 Mar 2027. "
                        "Register: 8-11 Mar 2027")
    assert (a, b) == (date(2027, 3, 8), date(2027, 3, 11)), (a, b)
    # two dates glued together are not one event range
    assert all(x == y for x, y in parse_dates("17 Sep - 3 Dec 2026"))
    # bogus links are not lists
    k4, _, _ = detect_lists([("Partners", "#"), ("speakers@x.com", "mailto:speakers@x.com"),
                             ("Sponsors", "javascript:;")], "")
    assert not k4, k4
    # generic names, foreign places, page title
    assert GENERIC_NAME.search("Events") and GENERIC_NAME.search("Home")
    assert not GENERIC_NAME.search("Shared Services & Outsourcing Week")
    assert FOREIGN.search("Malaysian Shared Services and Outsourcing Week")
    assert FOREIGN.search("Mastering SAP Connect Gold Coast")
    assert not FOREIGN.search("Shared Services Week in Orlando, FL")
    assert FOREIGN_URL.search("https://www.ssonetwork.com/events-ssoindia/")
    assert not FOREIGN_URL.search("https://events.example.com/indianapolis-summit")
    assert page_title("<title>Foo Procurement Summit 2027 | Org</title>") == "Foo Procurement Summit"
    # the page's own title wins over other events advertised in the menu
    html2 = ('<html><head><title>SSOW Orlando | March 8-11, 2027</title>'
             '<meta name="description" content="Join us in Orlando"></head><body>'
             + '<a href="/other">GBS Select 22-23 Feb 2027</a>' * 3 + '</body></html>')
    assert pick_date(head_text(html2))[0] == date(2027, 3, 8)
    assert pick_date(html_to_text_links(html2)[0])[0] == date(2027, 2, 22)   # body alone gets it wrong
    ld = ('<script type="application/ld+json">{"@context":"https://schema.org","@type":"Event",'
          '"name":"X","startDate":"2027-03-08T09:00","endDate":"2027-03-11"}</script>')
    assert json_ld_dates(ld) == [(date(2027, 3, 8), date(2027, 3, 11))]
    nav = [("Speakers", "/ev/x/speakers"), ("Partner Portal", "https://other.com/p"), ("Menu", "/about")]
    assert links_under(nav, "https://a.com/ev/x/") == [("Speakers", "/ev/x/speakers")]
    assert links_under(nav, "https://a.com/") == nav

    # end to end with fakes
    class FakeSerper:
        used, log = 0, []

        def search(self, q, gl, hl, page=1, num=10):
            self.used += 1
            if "official" in q:
                return {"organic": [{"title": "Retail Supply Chain Summit 2027",
                                     "link": "https://rscsummit.example/", "snippet": "Official site"}]}
            return {"organic": [
                {"title": "Shared Services & Outsourcing Week 2027 in Orlando, FL | SSON",
                 "link": "https://www.ssonetwork.com/events-shared-services-week",
                 "snippet": "March 8 - 11, 2027 Orlando. Shared services and GBS leaders."},
                {"title": "Retail Supply Chain Summit 2027 | 10times",
                 "link": "https://10times.com/retail-supply-chain-summit",
                 "snippet": "Retail Supply Chain Summit 2027 Chicago"},
                {"title": "Procurement Leaders Forum 2026 | Example",
                 "link": "https://plforum.example/", "snippet": "Procurement forum, 10-11 Dec 2026"},
                {"title": "Top 10 supply chain events 2027",
                 "link": "https://supplychaindigital.com/top-10", "snippet": "2027"}]}

    class FakeFetcher:
        def get(self, url):
            if "plforum" in url:   # JavaScript-style page: few links, no list links
                h = ('<a href="/">Home</a><a href="/login">Login</a>'
                     '<p>Early bird until 13 Nov 2026. 10-11 December 2026, Chicago, Illinois, USA. '
                     'Procurement, supply chain, accounts payable, SAP. AI automation for retail '
                     'and manufacturing. Enterprise buyers. Ticket price $490.</p>' + "x" * 900)
                return {"ok": True, "manual": False, "html": h, "status": 200, "why": ""}
            if "ssonetwork" in url:
                h = ('<html><head><title>Shared Services &amp; Outsourcing Week | March 8 - 11, 2027 | '
                     'Orlando</title></head><body>'
                     '<a href="/events-shared-services-week/speakers">Speakers</a>'
                     '<a href="/events-shared-services-week/sponsors">Sponsors</a>'
                     '<a href="/events-ssoindia/">Other event: India Week 20-23 Apr 2027</a>'
                     '<p>March 8 - 11, 2027 Orlando, FL United States. Shared services, accounts '
                     'payable, procure-to-pay, SAP, AI automation, manufacturing, enterprise. '
                     'Pass $2,199.00 registration.</p>' + "x" * 900 + '</body></html>')
            else:
                h = ('<html><head><script type="application/ld+json">{"@type":"Event",'
                     '"startDate":"2027-04-20","endDate":"2027-04-21"}</script></head><body>'
                     '<a href="/exhibitors">Exhibitors</a><a href="/speakers">Speakers</a>'
                     '<p>20-21 April 2027, Chicago, Illinois, USA. Retail supply chain, logistics, '
                     'procurement, SAP, order management, distribution, AI. Ticket price $950.</p>'
                     + "x" * 900 + '</body></html>')
            return {"ok": True, "manual": False, "html": h, "status": 200, "why": ""}

    ns = argparse.Namespace(markets=["US"], pages=1, max_resolve=5, max_fetch=10, out="out_selftest",
                            quota={"US": 3}, fill=False)
    global WINDOW_START, CORE_END, FALLBACK_END
    WINDOW_START, CORE_END, FALLBACK_END = date(2026, 11, 14), date(2026, 12, 31), date(2027, 4, 30)
    lst, picked, short, manual = run_pipeline(ns, FakeSerper(), FakeFetcher())
    names = sorted(c.name for c in picked)
    assert len(picked) == 3, (names, short)
    by = {c.official_url: c for c in picked}
    sson = by["https://www.ssonetwork.com/events-shared-services-week/"]
    assert sson.status == "VERIFIED" and sson.list_flag == "Y" and sson.start == date(2027, 3, 8)
    assert sson.cost == 2199.0 and sson.city == "Orlando" and sson.market == "US"
    assert sson.date_src == "title" and "20-23 Apr 2027" in sson.date_alt, (sson.date_src, sson.date_alt)
    rsc = by["https://rscsummit.example/"]
    assert rsc.date_src == "structured" and rsc.start == date(2027, 4, 20), (rsc.date_src, rsc.start)
    pl = by["https://plforum.example/"]
    assert pl.list_flag == "?" and pl.status == "NEEDS_CHECK" and pl.start == date(2026, 12, 10), \
        (pl.list_flag, pl.status, pl.start)
    assert pl.cost == 490.0
    assert sum(1 for c in picked if c.status == "VERIFIED") == 2
    import shutil
    shutil.rmtree("out_selftest", ignore_errors=True)
    print("selftest ok:", names)


def main() -> None:
    global WINDOW_START, CORE_END, FALLBACK_END
    for stream in (sys.stdout, sys.stderr):  # safe printing of umlauts on Windows
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Event finder (Serper)")
    ap.add_argument("--markets", nargs="+", default=["US", "UK", "DE"], choices=["US", "UK", "DE"])
    ap.add_argument("--window-start", default=WINDOW_START.isoformat())
    ap.add_argument("--core-end", default=CORE_END.isoformat())
    ap.add_argument("--fallback-end", default=FALLBACK_END.isoformat())
    ap.add_argument("--quota", nargs="*", help="e.g. US=5 UK=3 DE=2")
    ap.add_argument("--fill", action="store_true", help="backfill missing quota from other markets")
    ap.add_argument("--no-verify", action="store_true", help="do not open organiser pages")
    ap.add_argument("--max-queries", type=int, default=150, help="Serper query budget (cache is free)")
    ap.add_argument("--max-fetch", type=int, default=100)
    ap.add_argument("--max-resolve", type=int, default=30)
    ap.add_argument("--pages", type=int, default=1, help="Serper result pages per query")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--out", default="out")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return
    WINDOW_START = date.fromisoformat(args.window_start)
    CORE_END = date.fromisoformat(args.core_end)
    FALLBACK_END = date.fromisoformat(args.fallback_end)
    args.quota = {m: n for m, n in parse_quota(args.quota).items() if m in args.markets}
    key = load_key()
    if not key:
        sys.exit("No Serper key found. Set the environment variable SERPER_API_KEY, "
                 "or create a .env file with the line SERPER_API_KEY=your_key.")
    serper = Serper(key, Path(args.out) / "cache", args.max_queries, not args.no_cache)
    fetcher = None if args.no_verify else Fetcher(Path("manual"))
    lst, picked, short, manual = run_pipeline(args, serper, fetcher)
    report(lst, picked, short, manual, serper, args.out)


if __name__ == "__main__":
    main()

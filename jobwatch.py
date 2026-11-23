#!/usr/bin/env python3
"""
jobwatch — alerts you when matching jobs appear on company careers sites.

Usage:
  python jobwatch.py            # normal run: fetch, diff, alert on new matches
  python jobwatch.py --check    # test every source and show what matches (no alerts, no state change)
  python jobwatch.py --test-alert   # send a test notification to your phone/email
  python jobwatch.py --only Nvidia,Groq   # limit to some sources (any mode)
  python jobwatch.py --digest   # send one list of every open job that matches right now

Config: companies.yaml (sources + filters). Secrets: environment variables or a .env file.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import time
import traceback
from dataclasses import dataclass
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote, urldefrag

import requests
import yaml

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "companies.yaml"
STATE_PATH = HERE / "seen.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TIMEOUT = 30


# ───────────────────────────── helpers ─────────────────────────────

@dataclass
class Job:
    id: str
    title: str
    url: str
    location: str = ""
    age_days: float | None = None   # how long ago the company posted it, when the site says
    loc_reliable: bool = True       # False when the location was guessed from page text
    note: str = ""                  # extra line shown in the alert, e.g. why it's alerting now


# Query-string parameters that only describe the search, not the job. Stripped from job IDs so the
# same posting found by two different searches (or on page 2) counts once.
NOISE_PARAMS = {"q", "query", "keywords", "keyword", "search", "k", "src", "source", "page", "start",
                "sort", "sort_by", "sortby", "descending", "location", "locations", "gh_src", "ref",
                "lever-source", "lever-origin", "site", "jobindex", "from", "trk"}


def norm_id(job_id: str) -> str:
    """Normalise URL-style job IDs: drop the fragment, search-only parameters and trailing slash."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    if not str(job_id).startswith("http"):
        return str(job_id)
    p = urlsplit(job_id)
    keep = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
            if k.lower() not in NOISE_PARAMS and not k.lower().startswith("utm_")]
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/") or "/", urlencode(keep), ""))


def age_from(value) -> float | None:
    """Days since a timestamp given as ISO text, epoch seconds/ms, 'November 18, 2026',
    or Workday's 'Posted 3 Days Ago'. None when unknown."""
    from datetime import datetime, timezone
    if value in (None, "", 0):
        return None
    now = datetime.now(timezone.utc)
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            v = float(value)
            v = v / 1000 if v > 1e11 else v
            return max(0.0, (now.timestamp() - v) / 86400)
        s = str(value).strip()
        low = s.lower()
        if low.startswith("posted"):
            if "today" in low:
                return 0.0
            if "yesterday" in low:
                return 1.0
            m = re.search(r"(\d+)\+?\s*day", low)
            return float(m.group(1)) if m else None
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                return max(0.0, (now - datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)).total_seconds() / 86400)
            except ValueError:
                pass
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt).total_seconds() / 86400)
    except (ValueError, OverflowError):
        return None


def age_text(days: float | None) -> str:
    if days is None:
        return ""
    d = int(days)
    return "posted today" if d == 0 else f"posted {d} day{'s' if d != 1 else ''} ago"


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


RATE_LIMIT_WAITS = [5, 15, 30]  # seconds to back off after a 429 "Too Many Requests"


def http_request(method: str, url: str, session: requests.Session | None = None, **kw) -> requests.Response:
    """GET/POST with one retry on network errors and polite back-off on 429/503."""
    kw.setdefault("timeout", TIMEOUT)
    kw.setdefault("headers", {"User-Agent": UA, "Accept": "application/json"})
    send = (session or requests).request
    waits = list(RATE_LIMIT_WAITS)
    net_retries = 1
    while True:
        try:
            r = send(method, url, **kw)
        except requests.RequestException:
            if net_retries <= 0:
                raise
            net_retries -= 1
            time.sleep(3)
            continue
        if r.status_code in (429, 503) and waits:
            ra = r.headers.get("Retry-After", "")
            wait = int(ra) if ra.isdigit() and int(ra) <= 60 else waits[0]
            waits.pop(0)
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r


def http_get(url: str, **kw) -> requests.Response:
    return http_request("GET", url, **kw)


def http_post(url: str, payload: dict, **kw) -> requests.Response:
    kw.setdefault("headers", {"User-Agent": UA, "Accept": "application/json",
                              "Content-Type": "application/json"})
    return http_request("POST", url, json=payload, **kw)


def word_regex(terms: list[str]) -> re.Pattern | None:
    """Case-insensitive whole-word match for any of the terms."""
    terms = [t for t in (terms or []) if str(t).strip()]
    if not terms:
        return None
    alts = [r"(?<![A-Za-z0-9])" + re.escape(str(t).strip()) + r"(?![A-Za-z0-9])" for t in terms]
    return re.compile("|".join(alts), re.IGNORECASE)


# Text that clearly is a place (used to decide whether a location guessed from page text is real).
LOOKS_LIKE_PLACE = re.compile(
    r",|\b(India|Taiwan|China|Israel|Germany|Japan|Korea|Singapore|Poland|Ireland|France|Netherlands|"
    r"Mexico|Malaysia|Vietnam|Philippines|Romania|Spain|Italy|Sweden|Finland|Denmark|Switzerland|Austria|"
    r"Hungary|Czech|Serbia|Brazil|Costa Rica|Argentina|Australia|United Kingdom|UK|England|Scotland|"
    r"Bangalore|Bengaluru|Hyderabad|Pune|Shanghai|Beijing|Shenzhen|Hsinchu|Taipei|Tel Aviv|Haifa|"
    r"Yokneam|Munich|Dresden|Tokyo|Seoul|Penang|Cork|Cambridge|London|Belfast)\b", re.I)


class JobFilter:
    def __init__(self, cfg: dict):
        self.include = word_regex(cfg.get("include", []))
        self.exclude = word_regex(cfg.get("exclude", []))
        self.exceptions = word_regex(cfg.get("exclude_exceptions", []))
        self.locations = word_regex(cfg.get("locations", []))

    def matches(self, job: Job) -> bool:
        title = job.title
        if self.include and not self.include.search(title):
            return False
        if self.exclude:
            # Phrases like "Member of Technical Staff" are blanked out before the exclude check,
            # so "staff" there doesn't knock the job out.
            check = self.exceptions.sub(" ", title) if self.exceptions else title
            if self.exclude.search(check):
                return False
        loc = job.location.strip()
        if self.locations and loc:
            if not job.loc_reliable and not LOOKS_LIKE_PLACE.search(loc):
                return True  # page text that isn't clearly a place — don't judge on it
            # "3 Locations", "Multiple Locations", "Various" etc. — let those through.
            if re.search(r"\d+\s+locations|multiple locations|various|flexible|anywhere", loc, re.I):
                return True
            if not self.locations.search(loc):
                return False
        return True


def queries_for(src: dict, cfg: dict, key: str) -> list[str]:
    return src.get("queries") or cfg.get(key) or [""]


# ───────────────────────────── adapters ─────────────────────────────
# Each returns a list[Job] for one source. Raise on failure.

def fetch_greenhouse(src: dict, cfg: dict) -> list[Job]:
    data = http_get(f"https://boards-api.greenhouse.io/v1/boards/{src['board']}/jobs").json()
    return [Job(str(j["id"]), j.get("title", ""), j.get("absolute_url", ""),
                (j.get("location") or {}).get("name", ""),
                age_from(j.get("first_published") or j.get("updated_at"))) for j in data.get("jobs", [])]


def fetch_ashby(src: dict, cfg: dict) -> list[Job]:
    data = http_get(f"https://api.ashbyhq.com/posting-api/job-board/{src['board']}").json()
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        loc = j.get("location") or ""
        if j.get("isRemote"):
            loc = f"{loc} Remote".strip()
        out.append(Job(str(j["id"]), j.get("title", ""), j.get("jobUrl", ""), loc,
                       age_from(j.get("publishedAt") or j.get("publishedDate"))))
    return out


def fetch_lever(src: dict, cfg: dict) -> list[Job]:
    data = http_get(f"https://api.lever.co/v0/postings/{src['board']}?mode=json").json()
    return [Job(str(j["id"]), j.get("text", ""), j.get("hostedUrl", ""),
                (j.get("categories") or {}).get("location", ""), age_from(j.get("createdAt"))) for j in data]


def fetch_workday(src: dict, cfg: dict) -> list[Job]:
    """Workday careers API, made to look like a normal visit (cookies, Origin/Referer) and
    paced to avoid 429s. If Workday still rate-limits us, render the search page instead."""
    host, tenant, site = src["host"], src["tenant"], src["site"]
    home = f"https://{host}/en-US/{site}"
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    pages = int(src.get("pages", 1))
    pause = float(src.get("pause_s", 2.5))
    qs = queries_for(src, cfg, "search_queries")
    jobs: dict[str, Job] = {}
    try:
        s = requests.Session()
        base_headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
        http_get(home, session=s, headers={**base_headers, "Accept": "text/html"})  # sets cookies
        api_headers = {**base_headers, "Accept": "application/json", "Content-Type": "application/json",
                       "Origin": f"https://{host}", "Referer": home}
        # A blank search returns the site's newest postings first; read those, then the keyword searches.
        passes = [("", int(src.get("latest_pages", 3)))] + [(q, pages) for q in qs if q]
        for q, n_pages in passes:
            for p in range(n_pages):
                time.sleep(pause)
                data = http_post(api, {"appliedFacets": {}, "limit": 20, "offset": p * 20,
                                       "searchText": q}, session=s, headers=api_headers).json()
                posts = data.get("jobPostings", [])
                for j in posts:
                    path = j.get("externalPath", "")
                    if path:
                        jobs[path] = Job(path, j.get("title", ""), home + path, j.get("locationsText", ""),
                                         age_from(j.get("postedOn")))
                if len(posts) < 20:
                    break
        return list(jobs.values())
    except requests.HTTPError as e:
        if e.response is None or e.response.status_code not in (401, 403, 429):
            raise
        api_err = f"API {e.response.status_code}"

    # Fallback: let a real browser load the search page (it calls the same API with full browser cookies).
    try:
        found = fetch_browser({"url": f"{home}?q={{query}}", "link": r"/job/", "wait_ms": 8000},
                              {"browser_queries": qs[:3]})
    except Exception as e:
        raise RuntimeError(f"{api_err}; browser fallback: {e}")
    for j in found:  # use the same ID format as the API so switching methods never re-alerts
        m = re.search(r"(/job/[^?#]+)", j.url)
        if m:
            jobs[m.group(1)] = Job(m.group(1), j.title, j.url, j.location)
    return list(jobs.values())


def fetch_tesla(src: dict, cfg: dict) -> list[Job]:
    """Tesla's careers site loads every listing from one JSON feed. It's bot-protected, so
    try it directly, then from inside a real browser page, then by scraping the rendered page."""
    feed = "https://www.tesla.com/cua-api/apps/careers/state"
    search = src.get("url", "https://www.tesla.com/careers/search/?site=US")

    def parse(data: dict) -> list[Job]:
        lookup = data.get("lookup") or {}
        locs = lookup.get("locations") or {}
        out = []
        for j in data.get("listings") or []:
            jid = str(j.get("id") or "")
            title = j.get("t") or j.get("title") or ""
            if not jid or not title:
                continue
            loc = locs.get(str(j.get("l")), "") if isinstance(locs, dict) else ""
            if isinstance(loc, dict):
                loc = loc.get("name") or loc.get("label") or ""
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            out.append(Job(jid, title, f"https://www.tesla.com/careers/search/job/{slug}-{jid}", str(loc)))
        if not out:
            raise ValueError("feed had no listings")
        return out

    errors = []
    try:
        return parse(http_get(feed, headers={"User-Agent": UA, "Accept": "application/json",
                                             "Accept-Language": "en-US,en;q=0.9",
                                             "Referer": search}).json())
    except Exception as e:
        errors.append(f"feed: {type(e).__name__}")

    page = Browser.page()
    try:
        page.goto(search, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)
        try:
            data = page.evaluate("() => fetch('/cua-api/apps/careers/state', "
                                 "{headers: {accept: 'application/json'}}).then(r => r.json())")
            return parse(data)
        except Exception as e:
            errors.append(f"in-page feed: {type(e).__name__}")
        links = page.eval_on_selector_all(
            "a[href*='/careers/search/job/']", "els => els.map(e => [e.href, e.innerText || ''])")
    finally:
        page.context.close()
    out = {}
    for href, text in links:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        out[href] = Job(href.rstrip("/").rsplit("-", 1)[-1] if re.search(r"-\d+/?$", href) else href,
                        lines[0] if lines else "", href, " · ".join(lines[1:3]))
    if not out:
        errors.append("page: 0 job links (blocked by Tesla's bot protection)")
        raise RuntimeError("; ".join(errors))
    return list(out.values())


def fetch_amazon(src: dict, cfg: dict) -> list[Job]:
    jobs: dict[str, Job] = {}
    for q in queries_for(src, cfg, "search_queries"):
        params = {"base_query": q, "result_limit": 100, "sort": "recent", "offset": 0,
                  "normalized_country_code[]": src.get("countries", ["USA", "CAN"])}
        data = http_get("https://www.amazon.jobs/en/search.json", params=params).json()
        for j in data.get("jobs", []):
            jid = str(j.get("id_icims") or j.get("id"))
            jobs[jid] = Job(jid, j.get("title", ""), "https://www.amazon.jobs" + j.get("job_path", ""),
                            j.get("location", ""), age_from(j.get("posted_date")))
    return list(jobs.values())


def fetch_eightfold(src: dict, cfg: dict) -> list[Job]:
    """Eightfold career sites (Nvidia, Qualcomm, Microsoft). Tries Eightfold's JSON APIs,
    and falls back to loading the search page in the browser if both are blocked."""
    host, domain = src["host"], src["domain"]
    qs = queries_for(src, cfg, "search_queries")
    jobs: dict[str, Job] = {}
    errors = []

    pages = int(src.get("pages", 3))

    def add(p: dict) -> None:
        jid = str(p.get("id") or p.get("positionId") or "")
        if not jid:
            return
        locs = p.get("locations") or p.get("standardizedLocations") or p.get("location") or ""
        if isinstance(locs, list):  # keep every location so the location filter sees all of them
            locs = " | ".join(str(l.get("name", l) if isinstance(l, dict) else l) for l in locs)
        url = p.get("canonicalPositionUrl") or p.get("positionUrl") or f"/careers/job/{jid}"
        if url.startswith("/"):
            url = f"https://{host}{url}"
        jobs[jid] = Job(jid, p.get("name") or p.get("title") or "", url, str(locs),
                        age_from(p.get("t_create") or p.get("postedTs") or p.get("creationTs")))

    endpoints = [
        (f"https://{host}/api/pcsx/search",
         lambda q, start: {"domain": domain, "query": q, "location": "", "start": start, "sort_by": "timestamp"},
         lambda d: (d.get("data") or {}).get("positions")),
        (f"https://{host}/api/apply/v2/jobs",
         lambda q, start: {"domain": domain, "query": q, "start": start, "num": 50, "sort_by": "timestamp"},
         lambda d: d.get("positions")),
    ]
    for url, params, extract in endpoints:
        try:
            for q in qs:
                start = 0
                for _ in range(pages):  # read several result pages, not just the first ~10
                    positions = extract(http_get(url, params=params(q, start)).json())
                    if positions is None:
                        raise ValueError("unexpected response shape")
                    for p in positions:
                        add(p)
                    if not positions:
                        break
                    start += len(positions)
            return list(jobs.values())
        except Exception as e:
            errors.append(f"{url.rsplit('/api/', 1)[1]}: {type(e).__name__}")
            jobs.clear()

    # Both APIs failed — render the real search page instead.
    try:
        return fetch_browser({"url": f"https://{host}/careers?query={{query}}&sort_by=timestamp",
                              "link": r"/careers/job/\d+", "wait_ms": src.get("wait_ms", 7000)}, cfg)
    except Exception as e:
        errors.append(f"browser: {e}")
        raise RuntimeError("; ".join(errors))


class Browser:
    """Lazily started headless Chromium shared by all 'browser' sources in a run."""
    _pw = _browser = None

    @classmethod
    def page(cls):
        if cls._browser is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                raise RuntimeError("playwright not installed: pip install playwright && "
                                   "python -m playwright install chromium")
            cls._pw = sync_playwright().start()
            cls._browser = cls._pw.chromium.launch(headless=True)
        ctx = cls._browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 900},
                                       locale="en-US")
        return ctx.new_page()

    @classmethod
    def close(cls):
        try:
            if cls._browser:
                cls._browser.close()
            if cls._pw:
                cls._pw.stop()
        except Exception:
            pass
        cls._pw = cls._browser = None


# Link text that is a button label, not a job title.
GENERIC_LINK_TEXT = re.compile(r"^(learn more|apply( now)?|view( job| details| role)?|see (details|more|job)|"
                               r"details|more|read more|open|go|job details|share|save|→|>)$", re.I)

# For each link: [href, its own text, aria-label, heading of the card it sits in, the card's text].
LINKS_JS = """els => els.map(e => {
  const card = e.closest('li, article, [role=listitem], [class*=card], [class*=Card], [class*=job], [class*=result]');
  const h = card ? card.querySelector('h1, h2, h3, h4, [class*=title], [class*=Title]') : null;
  return [e.href, e.innerText || '', e.getAttribute('aria-label') || '', h ? (h.innerText || '') : '',
          card ? (card.innerText || '').slice(0, 600) : ''];
})"""


def _title_and_location(text: str, aria: str, heading: str, card: str = "") -> tuple[str, str]:
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    first = lines[0] if lines else ""
    if first and not GENERIC_LINK_TEXT.match(first) and len(first) > 3:
        return first[:160], " · ".join(lines[1:3])[:200]
    heading = (heading or "").strip().splitlines()[0].strip() if (heading or "").strip() else ""
    if heading and not GENERIC_LINK_TEXT.match(heading):
        title = heading
    else:
        title = re.sub(r"^(learn more|read more|apply|view)( about| for)?\s*", "", (aria or "").strip(), flags=re.I)
    # The link itself had no useful text, so look for a place name elsewhere on the job card.
    loc = ""
    for line in (l.strip() for l in (card or "").splitlines()):
        if line and line != title and not GENERIC_LINK_TEXT.match(line) and LOOKS_LIKE_PLACE.search(line):
            loc = re.sub(r"\s*(learn more|apply now|apply|view job|view details|read more)\s*$", "", line, flags=re.I)
            break
    return title[:160], loc[:200]


def fetch_browser(src: dict, cfg: dict) -> list[Job]:
    """Load a careers search page in a real browser and collect job links matching a regex.
    Optional per-source settings: pages + page_param (read result pages 2..N), wait_ms (max wait)."""
    link_re = re.compile(src["link"], re.I)
    wait_ms = int(src.get("wait_ms", 8000))
    url_tpl = src["url"]
    qs = queries_for(src, cfg, "browser_queries") if "{query}" in url_tpl else [""]
    pages = int(src.get("pages", 1)) if src.get("page_param") else 1
    jobs: dict[str, Job] = {}

    def count_links(page) -> int:
        hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        return sum(1 for h in hrefs if link_re.search(h))

    for q in qs:
        for n in range(1, pages + 1):
            url = url_tpl.replace("{query}", quote(q))
            if n > 1:
                url += ("&" if "?" in url else "?") + f"{src['page_param']}={n}"
            page = Browser.page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                # Wait until job links appear and stop changing, instead of a fixed sleep.
                waited, last, stable = 0, -1, 0
                while waited < wait_ms:
                    page.wait_for_timeout(500)
                    waited += 500
                    c = count_links(page)
                    stable = stable + 1 if (c == last and c > 0) else 0
                    last = c
                    if stable >= 2:
                        break
                for _ in range(2):  # nudge lazy-loading lists
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(600)
                links = page.eval_on_selector_all("a[href]", LINKS_JS)
            finally:
                page.context.close()
            found_here = 0
            for href, text, aria, heading, card in links:
                if not link_re.search(href):
                    continue
                jid = norm_id(href)
                title, loc = _title_and_location(text, aria, heading, card)
                found_here += 1
                old = jobs.get(jid)
                if old is None or (title and (not old.title or GENERIC_LINK_TEXT.match(old.title))):
                    jobs[jid] = Job(jid, title, urldefrag(href)[0], loc, loc_reliable=False)
            if found_here == 0:
                break  # no more result pages
    if not jobs:
        raise RuntimeError("found 0 job links — the page layout or URL may have changed")
    return list(jobs.values())


ADAPTERS = {
    "greenhouse": fetch_greenhouse,
    "ashby": fetch_ashby,
    "lever": fetch_lever,
    "workday": fetch_workday,
    "amazon": fetch_amazon,
    "eightfold": fetch_eightfold,
    "tesla": fetch_tesla,
    "browser": fetch_browser,
}


# ───────────────────────────── notifications ─────────────────────────────

def notify(new: list[tuple[str, Job]], warnings: list[str], dry: bool = False) -> None:
    lines = []
    for company, j in new:
        extra = "".join(f" · {x}" for x in (j.location[:120], age_text(j.age_days)) if x)
        tag = f"[{j.note}] " if j.note else ""
        lines.append(f"{tag}{company}: {j.title}{extra}\n{j.url}")
    if dry:
        for l in lines + warnings:
            print("[dry-run alert]", l.replace("\n", "  "))
        return
    send_ntfy(new, warnings)
    send_email(lines, warnings)


def send_ntfy(new: list[tuple[str, Job]], warnings: list[str]) -> None:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return
    base = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    url = f"{base}/{topic}"

    def post(title: str, body: str, click: str = "", tags: str = "briefcase", prio: str = "high"):
        h = {"Title": title.encode("utf-8"), "Tags": tags, "Priority": prio}
        if click:
            h["Click"] = click
            h["Actions"] = f"view, Open job, {click}"  # visible button in the app and on ntfy.sh/app
        try:
            requests.post(url, data=body.encode("utf-8"), headers=h, timeout=TIMEOUT).raise_for_status()
        except requests.RequestException as e:
            print(f"ntfy failed: {e}", file=sys.stderr)

    per_job = int(os.environ.get("NTFY_MAX_PER_JOB", "20"))
    for company, j in new[:per_job]:  # one tappable push (with an Open job button) per job
        loc = j.location if len(j.location) <= 90 else j.location[:87] + "…"
        post(f"{j.note or 'New'}: {company}",
             "\n".join(x for x in (j.title, loc, age_text(j.age_days)) if x), click=j.url)
        time.sleep(0.5)  # stay well under ntfy.sh's burst limit
    rest = new[per_job:]
    if rest:  # anything beyond that goes into list pushes that still carry every link
        lines = [f"• {c}: {j.title}\n  {j.url}" for c, j in rest]
        chunk, chunks = "", []
        for line in lines:
            if len(chunk) + len(line) > 3500:
                chunks.append(chunk)
                chunk = ""
            chunk += line + "\n"
        chunks.append(chunk)
        for i, body in enumerate(chunks, 1):
            post(f"{len(rest)} more new jobs ({i}/{len(chunks)})", body.strip(), prio="default")
    for w in warnings:
        post("jobwatch warning", w, tags="warning", prio="default")


def send_email(lines: list[str], warnings: list[str]) -> None:
    user, pw, to = (os.environ.get(k, "").strip() for k in ("SMTP_USER", "SMTP_PASS", "EMAIL_TO"))
    if not (user and pw and to):
        return
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    n = len(lines)
    subject = (f"{n} new job{'s' if n != 1 else ''} posted" if n else "jobwatch warning")
    body = "\n\n".join(lines)
    if warnings:
        body += "\n\n⚠️ Warnings:\n" + "\n".join(warnings)
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    try:
        with smtplib.SMTP_SSL(host, port, timeout=TIMEOUT) as s:
            s.login(user, pw)
            s.sendmail(user, [a.strip() for a in to.split(",")], msg.as_string())
    except Exception as e:
        print(f"email failed: {e}", file=sys.stderr)


def send_digest(heading: str, items: list[tuple[str, Job]], warnings: list[str], dry: bool = False) -> None:
    lines = [f"• {c}: {j.title}" + (f" ({age_text(j.age_days)})" if j.age_days is not None else "")
             + f"\n  {j.url}" for c, j in items]
    if dry:
        print(f"[dry-run digest] {heading}: {len(items)} jobs")
        for l in lines + warnings:
            print("   ", l.replace("\n", "  "))
        return
    send_email([f"{c}: {j.title}\n{j.url}" for c, j in items], warnings)
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return
    url = f"{os.environ.get('NTFY_SERVER', 'https://ntfy.sh').rstrip('/')}/{topic}"
    chunks, chunk = [], ""
    for line in lines or ["Nothing open matches your filters right now."]:
        if len(chunk) + len(line) > 3500:
            chunks.append(chunk)
            chunk = ""
        chunk += line + "\n"
    chunks.append(chunk)
    for i, body in enumerate(chunks, 1):
        title = f"{heading} — {len(items)} jobs" + (f" ({i}/{len(chunks)})" if len(chunks) > 1 else "")
        try:
            requests.post(url, data=body.strip().encode("utf-8"), timeout=TIMEOUT,
                          headers={"Title": title.encode("utf-8"), "Tags": "clipboard", "Priority": "default"}
                          ).raise_for_status()
        except requests.RequestException as e:
            print(f"ntfy failed: {e}", file=sys.stderr)
        time.sleep(0.5)
    for w in warnings:
        try:
            requests.post(url, data=w.encode("utf-8"), timeout=TIMEOUT,
                          headers={"Title": b"jobwatch warning", "Tags": "warning"}).raise_for_status()
        except requests.RequestException as e:
            print(f"ntfy failed: {e}", file=sys.stderr)


# ───────────────────────────── main ─────────────────────────────

STATE_VERSION = 2


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"seen": {}, "failures": {}}


def merge_state(local: dict, remote: dict) -> dict:
    """Combine this run's state with a newer copy another run saved meanwhile. Seen job IDs are
    unioned so neither run's jobs get re-alerted; everything else keeps this run's (newer) values."""
    out = dict(remote)
    out.update(local)
    seen = {k: set(v) for k, v in (remote.get("seen") or {}).items()}
    for k, v in (local.get("seen") or {}).items():
        seen[k] = seen.get(k, set()) | set(v)
    out["seen"] = {k: list(v) for k, v in seen.items()}
    titles = {k: dict(v) for k, v in (remote.get("titles") or {}).items()}
    for k, v in (local.get("titles") or {}).items():
        titles.setdefault(k, {}).update(v)
    out["titles"] = titles
    out["version"] = max(local.get("version", 1), remote.get("version", 1))
    return out


def save_state(state: dict) -> None:
    for k in state["seen"]:
        state["seen"][k] = sorted(set(state["seen"][k]))
    STATE_PATH.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="test sources and show matches; no alerts, no state change")
    ap.add_argument("--dry-run", action="store_true", help="normal run but print alerts instead of sending")
    ap.add_argument("--test-alert", action="store_true", help="send a test notification and exit")
    ap.add_argument("--only", default="", help="comma-separated source names to run")
    ap.add_argument("--notify-existing", action="store_true",
                    help="on a source's first run, alert on all current matches instead of silently recording them")
    ap.add_argument("--merge-state", metavar="FILE",
                    help="merge another copy of seen.json into this one (used by the workflow) and exit")
    ap.add_argument("--digest", action="store_true",
                    help="send one list of every currently open matching job, with links")
    args = ap.parse_args()

    if args.merge_state:
        other = Path(args.merge_state)
        if other.exists() and other.stat().st_size and STATE_PATH.exists():
            save_state(merge_state(load_state(), json.loads(other.read_text(encoding="utf-8"))))
            print("Merged newer saved state into seen.json")
        return 0

    load_dotenv(HERE / ".env")
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    jf = JobFilter(cfg.get("filters", {}))
    fail_alert_after = int(cfg.get("alert_after_failures", 3))
    max_age = cfg.get("max_age_days")  # skip listings the company posted longer ago than this
    max_age = float(max_age) if max_age not in (None, "", 0) else None

    if args.test_alert:
        if not (os.environ.get("NTFY_TOPIC") or os.environ.get("SMTP_USER")):
            print("No NTFY_TOPIC or SMTP_* settings found (.env or environment).")
            return 1
        notify([("jobwatch", Job("test", "Test alert — setup works 🎉", "https://ntfy.sh", "Everywhere"))], [])
        print("Test alert sent.")
        return 0

    only = {s.strip().lower() for s in args.only.split(",") if s.strip()}
    sources = [s for s in cfg["sources"] if s.get("enabled", True)
               and (not only or s["name"].lower() in only)]

    state = load_state()
    state.setdefault("seen", {})
    state.setdefault("failures", {})
    state.setdefault("titles", {})
    if "warned" not in state:  # upgrading: sources already past the threshold were warned before
        state["warned"] = {n: True for n, c in state["failures"].items() if c >= fail_alert_after}
    upgrading = state.get("version", 1) < STATE_VERSION and bool(state["seen"]) and not only
    digest = args.digest or upgrading

    filters_now = cfg.get("filters", {})
    old_filters = state.get("filters")
    old_jf = JobFilter(old_filters) if (old_filters is not None and old_filters != filters_now) else None

    budget_s = float(cfg.get("run_budget_minutes", 12)) * 60
    run_start = time.time()
    new_jobs: list[tuple[str, Job]] = []
    all_matches: list[tuple[str, Job]] = []   # every currently-open match (for digests)
    warnings: list[str] = []
    rc = 0

    def record_failure(name: str, err: str) -> None:
        # Failures count up, successes count down (instead of resetting), so a site that
        # keeps flapping between working and broken still reaches the warning threshold.
        n = min(state["failures"].get(name, 0) + 1, fail_alert_after * 2)
        state["failures"][name] = n
        print(f"✗ {name}: {err[:200]} (failure score {n}/{fail_alert_after})")
        if n >= fail_alert_after and not state["warned"].get(name):
            state["warned"][name] = True
            warnings.append(f"{name} keeps failing: {err[:200]}. "
                            f"Run `python jobwatch.py --check --only \"{name}\"` and fix companies.yaml.")

    def record_success(name: str) -> None:
        n = state["failures"].get(name, 0) - 1
        if n <= 0:
            state["failures"].pop(name, None)
            state["warned"].pop(name, None)
        else:
            state["failures"][name] = n

    try:
        for src in sources:
            name, typ = src["name"], src["type"]
            if not args.check and time.time() - run_start > budget_s:
                # Out of time: skip the rest so this run still sends its alerts and saves.
                record_failure(name, "skipped — the run hit its time budget (run_budget_minutes)")
                continue
            t0 = time.time()
            try:
                jobs = ADAPTERS[typ](src, cfg)
                err = None
            except Exception as e:  # keep going; one broken site shouldn't stop the rest
                jobs, err = [], f"{type(e).__name__}: {e}"
                if os.environ.get("JOBWATCH_DEBUG"):
                    traceback.print_exc()
            dt = time.time() - t0
            for j in jobs:
                j.id = norm_id(j.id)
            prev_seen = state["seen"].get(name, [])
            if not err and not jobs and prev_seen:
                err = f"returned 0 jobs (it listed {len(prev_seen)} before) — the site may have changed"

            if args.check:
                if err:
                    print(f"✗ {name:<20} {typ:<10} FAILED  {err[:140]}")
                    rc = 1
                else:
                    m = [j for j in jobs if jf.matches(j)]
                    print(f"✓ {name:<20} {typ:<10} {len(jobs):>4} jobs, {len(m):>3} match  ({dt:.0f}s)")
                    for j in m[:4]:
                        print(f"      · {j.title[:90]}  [{j.location[:40]}]")
                continue

            if err:
                record_failure(name, err)
                continue
            record_success(name)

            first_run = name not in state["seen"]
            seen = {norm_id(x) for x in prev_seen}
            prev_titles = state["titles"].get(name, {})
            fresh = [j for j in jobs if j.id not in seen]
            state["seen"][name] = list(seen | {j.id for j in jobs})
            state["titles"][name] = {j.id: j.title for j in jobs if j.title}
            matching = [j for j in fresh if jf.matches(j)]
            all_matches += [(name, j) for j in jobs if jf.matches(j)]
            if first_run and not args.notify_existing:
                print(f"• {name}: first run, recorded {len(jobs)} existing jobs ({len(matching)} match) silently")
                continue

            # Jobs seen before that match now but didn't then: the title was edited to something
            # that matches, or you changed the filters in companies.yaml.
            for j in jobs:
                if j.id not in seen or not jf.matches(j):
                    continue
                old_title = prev_titles.get(j.id, "")
                if (old_title and old_title != j.title and not GENERIC_LINK_TEXT.match(old_title)
                        and not jf.matches(Job(j.id, old_title, j.url, j.location, loc_reliable=j.loc_reliable))):
                    j.note = "Updated"
                    matching.append(j)
                elif old_jf is not None and not old_jf.matches(j):
                    j.note = "Now matches"
                    matching.append(j)

            stale = [j for j in matching if max_age is not None and j.age_days is not None and j.age_days > max_age]
            for j in stale:
                print(f"    skipped (old listing, {age_text(j.age_days)}): {j.title[:80]}")
            matching = [j for j in matching if j not in stale]
            print(f"✓ {name}: {len(jobs)} jobs, {len(fresh)} new, {len(matching)} new matches ({dt:.0f}s)")
            new_jobs += [(name, j) for j in matching]
    finally:
        Browser.close()

    if args.check:
        return rc

    def newest_first(items):
        return sorted(items, key=lambda cj: (cj[1].age_days is None, cj[1].age_days or 0))

    new_jobs = newest_first(new_jobs)
    if digest:
        # One list of every job that matches right now (with links), instead of one push each.
        heading = ("Catch-up: open jobs matching your filters" if upgrading and not args.digest
                   else "Digest: open jobs matching your filters")
        send_digest(heading, newest_first(all_matches), warnings, dry=args.dry_run)
    elif new_jobs or warnings:
        notify(new_jobs, warnings, dry=args.dry_run)
    if not only:
        state["filters"] = filters_now
        state["version"] = STATE_VERSION
    save_state(state)
    print(f"Done: {len(new_jobs)} new matching job(s), {len(all_matches)} open matches, "
          f"{len(warnings)} warning(s){' (sent as digest)' if digest else ''}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

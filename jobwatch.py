#!/usr/bin/env python3
"""
jobwatch — alerts you when matching jobs appear on company careers sites.

Usage:
  python jobwatch.py            # normal run: fetch, diff, alert on new matches
  python jobwatch.py --check    # test every source and show what matches (no alerts, no state change)
  python jobwatch.py --test-alert   # send a test notification to your phone/email
  python jobwatch.py --only Nvidia,Groq   # limit to some sources (any mode)

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


class JobFilter:
    def __init__(self, cfg: dict):
        self.include = word_regex(cfg.get("include", []))
        self.exclude = word_regex(cfg.get("exclude", []))
        self.locations = word_regex(cfg.get("locations", []))

    def matches(self, job: Job) -> bool:
        if self.include and not self.include.search(job.title):
            return False
        if self.exclude and self.exclude.search(job.title):
            return False
        if self.locations and job.location.strip():
            loc = job.location
            # Workday shows "3 Locations" when there are several — let those through.
            if not re.search(r"\d+\s+locations", loc, re.I) and not self.locations.search(loc):
                return False
        return True


def queries_for(src: dict, cfg: dict, key: str) -> list[str]:
    return src.get("queries") or cfg.get(key) or [""]


# ───────────────────────────── adapters ─────────────────────────────
# Each returns a list[Job] for one source. Raise on failure.

def fetch_greenhouse(src: dict, cfg: dict) -> list[Job]:
    data = http_get(f"https://boards-api.greenhouse.io/v1/boards/{src['board']}/jobs").json()
    return [Job(str(j["id"]), j.get("title", ""), j.get("absolute_url", ""),
                (j.get("location") or {}).get("name", "")) for j in data.get("jobs", [])]


def fetch_ashby(src: dict, cfg: dict) -> list[Job]:
    data = http_get(f"https://api.ashbyhq.com/posting-api/job-board/{src['board']}").json()
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        loc = j.get("location") or ""
        if j.get("isRemote"):
            loc = f"{loc} Remote".strip()
        out.append(Job(str(j["id"]), j.get("title", ""), j.get("jobUrl", ""), loc))
    return out


def fetch_lever(src: dict, cfg: dict) -> list[Job]:
    data = http_get(f"https://api.lever.co/v0/postings/{src['board']}?mode=json").json()
    return [Job(str(j["id"]), j.get("text", ""), j.get("hostedUrl", ""),
                (j.get("categories") or {}).get("location", "")) for j in data]


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
        for q in qs:
            for p in range(pages):
                time.sleep(pause)
                data = http_post(api, {"appliedFacets": {}, "limit": 20, "offset": p * 20,
                                       "searchText": q}, session=s, headers=api_headers).json()
                posts = data.get("jobPostings", [])
                for j in posts:
                    path = j.get("externalPath", "")
                    if path:
                        jobs[path] = Job(path, j.get("title", ""), home + path, j.get("locationsText", ""))
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
            jobs[jid] = Job(jid, j.get("title", ""),
                            "https://www.amazon.jobs" + j.get("job_path", ""), j.get("location", ""))
    return list(jobs.values())


def fetch_eightfold(src: dict, cfg: dict) -> list[Job]:
    """Eightfold career sites (Nvidia, Qualcomm, Microsoft). Tries Eightfold's JSON APIs,
    and falls back to loading the search page in the browser if both are blocked."""
    host, domain = src["host"], src["domain"]
    qs = queries_for(src, cfg, "search_queries")
    jobs: dict[str, Job] = {}
    errors = []

    def add(p: dict) -> None:
        jid = str(p.get("id") or p.get("positionId") or "")
        if not jid:
            return
        locs = p.get("locations") or p.get("standardizedLocations") or p.get("location") or ""
        if isinstance(locs, list):
            locs = " | ".join(str(l.get("name", l) if isinstance(l, dict) else l) for l in locs[:3])
        url = p.get("canonicalPositionUrl") or p.get("positionUrl") or f"/careers/job/{jid}"
        if url.startswith("/"):
            url = f"https://{host}{url}"
        jobs[jid] = Job(jid, p.get("name") or p.get("title") or "", url, str(locs))

    endpoints = [
        (f"https://{host}/api/pcsx/search",
         lambda q: {"domain": domain, "query": q, "location": "", "start": 0, "sort_by": "timestamp"},
         lambda d: (d.get("data") or {}).get("positions")),
        (f"https://{host}/api/apply/v2/jobs",
         lambda q: {"domain": domain, "query": q, "start": 0, "num": 50, "sort_by": "timestamp"},
         lambda d: d.get("positions")),
    ]
    for url, params, extract in endpoints:
        try:
            for q in qs:
                positions = extract(http_get(url, params=params(q)).json())
                if positions is None:
                    raise ValueError("unexpected response shape")
                for p in positions:
                    add(p)
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


def fetch_browser(src: dict, cfg: dict) -> list[Job]:
    """Load a careers search page in a real browser and collect job links matching a regex."""
    link_re = re.compile(src["link"], re.I)
    wait_ms = int(src.get("wait_ms", 5000))
    url_tpl = src["url"]
    qs = queries_for(src, cfg, "browser_queries") if "{query}" in url_tpl else [""]
    jobs: dict[str, Job] = {}
    for q in qs:
        page = Browser.page()
        try:
            page.goto(url_tpl.replace("{query}", quote(q)), wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(wait_ms)
            for _ in range(2):  # nudge lazy-loading lists
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(800)
            links = page.eval_on_selector_all(
                "a[href]", "els => els.map(e => [e.href, (e.innerText || e.getAttribute('aria-label') || '')])")
        finally:
            page.context.close()
        for href, text in links:
            if not link_re.search(href):
                continue
            href = urldefrag(href)[0]
            lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
            title = lines[0][:160] if lines else ""
            loc = " · ".join(lines[1:3])[:120] if len(lines) > 1 else ""
            if href not in jobs or (title and not jobs[href].title):
                jobs[href] = Job(href, title, href, loc)
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
        loc = f" · {j.location}" if j.location else ""
        lines.append(f"{company}: {j.title}{loc}\n{j.url}")
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
        try:
            requests.post(url, data=body.encode("utf-8"), headers=h, timeout=TIMEOUT).raise_for_status()
        except requests.RequestException as e:
            print(f"ntfy failed: {e}", file=sys.stderr)

    if len(new) <= 8:  # one tappable push per job
        for company, j in new:
            post(f"New: {company}", f"{j.title}\n{j.location}".strip(), click=j.url)
    else:
        body = "\n".join(f"• {c}: {j.title}" for c, j in new[:30])
        if len(new) > 30:
            body += f"\n…and {len(new) - 30} more (see email)"
        post(f"{len(new)} new jobs", body)
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


# ───────────────────────────── main ─────────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"seen": {}, "failures": {}}


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
    args = ap.parse_args()

    load_dotenv(HERE / ".env")
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    jf = JobFilter(cfg.get("filters", {}))
    fail_alert_after = int(cfg.get("alert_after_failures", 3))

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
    new_jobs: list[tuple[str, Job]] = []
    warnings: list[str] = []
    rc = 0

    try:
        for src in sources:
            name, typ = src["name"], src["type"]
            t0 = time.time()
            try:
                jobs = ADAPTERS[typ](src, cfg)
                err = None
            except Exception as e:  # keep going; one broken site shouldn't stop the rest
                jobs, err = [], f"{type(e).__name__}: {e}"
                if os.environ.get("JOBWATCH_DEBUG"):
                    traceback.print_exc()
            dt = time.time() - t0

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
                n = state["failures"].get(name, 0) + 1
                state["failures"][name] = n
                print(f"✗ {name}: {err[:200]} (failure #{n})")
                if n == fail_alert_after:
                    warnings.append(f"{name} has failed {n} runs in a row: {err[:200]}. "
                                    f"Run `python jobwatch.py --check --only \"{name}\"` and fix companies.yaml.")
                continue
            state["failures"].pop(name, None)

            first_run = name not in state["seen"]
            seen = set(state["seen"].get(name, []))
            fresh = [j for j in jobs if j.id not in seen]
            state["seen"][name] = list(seen | {j.id for j in jobs})
            matching = [j for j in fresh if jf.matches(j)]
            if first_run and not args.notify_existing:
                print(f"• {name}: first run, recorded {len(jobs)} existing jobs ({len(matching)} match) silently")
                continue
            print(f"✓ {name}: {len(jobs)} jobs, {len(fresh)} new, {len(matching)} new matches ({dt:.0f}s)")
            new_jobs += [(name, j) for j in matching]
    finally:
        Browser.close()

    if args.check:
        return rc

    if new_jobs or warnings:
        notify(new_jobs, warnings, dry=args.dry_run)
    save_state(state)
    print(f"Done: {len(new_jobs)} new matching job(s), {len(warnings)} warning(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

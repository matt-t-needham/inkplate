"""Daily quote: Wikiquote's Quote of the Day, attributed and dated.

Each day the dashboard shows that day's Wikiquote QOTD. A candidate is used
only when it passes every gate:

- it fits the panel (the caller's `fits(text)` measures it in the real font);
- it isn't vetoed from the pane and hasn't been shown before (history);
- the quote can be found on the author's own Wikiquote page, outside any
  Disputed / Misattributed / Attributed / "about" section, and its citation
  (or the section it sits in) yields a year. Attribution and date are the
  point — a quote we can't place is skipped, never shown bare.

When today's QOTD fails a gate, a replacement is drawn from the QOTD archive
strictly before NO_REPEAT_BEFORE (the day this feature started), so a
fallback can never be a quote the daily feed is still going to serve.
Fallback dates are seeded by the day, so a re-render picks the same one.

Network shape: one `action=parse` call for a QOTD page, one for the author's
page, per candidate; MAX_TRIES caps a bad day, and a failed day backs off
for FAIL_BACKOFF_S so renders don't hammer the API.
"""

import html
import random
import re
import time
from datetime import date, timedelta

import datasources as ds

API = "https://en.wikiquote.org/w/api.php"
NO_REPEAT_BEFORE = date(2026, 10, 7)
ARCHIVE_START = date(2005, 1, 1)
MAX_TRIES = 12
FAIL_BACKOFF_S = 1800
HISTORY_CAP = 5000

CURRENT = "quote.json"
HISTORY = "quote_history.json"
FAILED = "quote_failed.json"

# Section headings whose quotes are not reliably sourced.
_REJECT = re.compile(r"disputed|misattribut|attributed|unsourced|apocryph|\babout\b", re.I)
_YEAR_BC = re.compile(r"\b(\d{1,4})\s*(BC|BCE)\b")
_YEAR = re.compile(r"\b(1[0-9]{3}|20[0-2][0-9])\b")


def qotd_title(day: date) -> str:
    return f"Wikiquote:Quote of the day/{day:%B} {day.day}, {day.year}"


def key(text: str) -> str:
    """Identity for history/veto: letters and digits only, lowercased."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())[:120]


def _parse(page: str, prop: str) -> dict | None:
    raw = ds._get_json(API, params={
        "action": "parse", "page": page, "prop": prop, "format": "json",
        "formatversion": 2, "redirects": 1, "disablelimitreport": 1,
    })
    return None if "error" in raw else raw.get("parse")


# ── markup → text ─────────────────────────────────────────────────────────────

def _html_text(h: str) -> str:
    h = re.sub(r"(?is)<(style|script)\b.*?</\1>", "", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", h)
    return html.unescape(re.sub(r"<[^>]+>", "", h))


def _wiki_text(s: str) -> str:
    s = re.sub(r"(?is)<ref[^>]*/>|<ref[^>]*>.*?</ref>", "", s)
    for _ in range(3):  # templates can nest a little
        s = re.sub(r"\{\{[^{}]*\}\}", "", s)
    s = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"\[https?://\S+\s+([^\]]*)\]", r"\1", s)
    s = s.replace("'''", "").replace("''", "")
    s = re.sub(r"<[^>]+>", "", s)
    return " ".join(html.unescape(s).split())


def _italic(s: str) -> str | None:
    m = re.search(r"''(?!')(.+?)''", s.replace("'''", ""))
    return _wiki_text(m.group(1)) if m else None


# ── QOTD page and author page ────────────────────────────────────────────────

def _qotd(day: date) -> dict | None:
    """{text, author, author_page} from one QOTD page, or None."""
    p = _parse(qotd_title(day), "text")
    if not p:
        return None
    h = p.get("text") or ""
    plain = _html_text(h)
    m = re.search(r"(?s)^(.*?)~\s*([^~\n]+?)\s*~", plain)
    if not m:
        return None
    lines = [ln.strip() for ln in m.group(1).splitlines() if ln.strip()]
    text = " / ".join(lines).strip(' "“”')
    author = " ".join(m.group(2).split())
    page = None
    for a in re.finditer(r'<a [^>]*title="([^"]+)"[^>]*>(.*?)</a>', h, re.S):
        if " ".join(_html_text(a.group(2)).split()) == author:
            page = html.unescape(a.group(1))
            break
    if not text or not author or not page:
        return None
    return {"text": text, "author": author, "author_page": page}


def _year_in(s: str) -> str | None:
    m = _YEAR_BC.search(s or "")
    if m:
        return f"{m.group(1)} BC"
    m = _YEAR.search(s or "")
    return m.group(1) if m else None


def _source(author_page: str, text: str) -> dict | None:
    """{work, year} for `text` as cited on the author's page; None when the
    quote isn't found in a sourced section or no year can be established."""
    p = _parse(author_page, "wikitext")
    if not p:
        return None
    lines = (p.get("wikitext") or "").splitlines()
    want = key(text)
    probes = {want[:40], want[-40:]} if len(want) >= 40 else {want}
    h2 = h3 = ""
    for i, ln in enumerate(lines):
        hm = re.match(r"^(={2,})\s*(.*?)\s*=+\s*$", ln)
        if hm:
            if len(hm.group(1)) == 2:
                h2, h3 = hm.group(2), ""
            else:
                h3 = hm.group(2)
            continue
        if not ln.startswith("*") or ln.startswith("**"):
            continue
        body = key(_wiki_text(ln.lstrip("*")))
        if not any(pr and pr in body for pr in probes):
            continue
        if _REJECT.search(h2) or _REJECT.search(h3):
            return None
        cite = next((c for c in lines[i + 1:i + 4] if c.startswith("**")), "")
        year = _year_in(_wiki_text(cite)) or _year_in(h3) or _year_in(h2)
        if not year:
            return None
        work = _italic(cite) or _italic(h3)
        if not work and h3:
            work = re.sub(r"\s*\(.*?\)\s*$", "", _wiki_text(h3)) or None
        if work and len(work) > 60:
            work = work[:57].rstrip() + "…"
        return {"work": work, "year": year}
    return None


# ── selection ────────────────────────────────────────────────────────────────

def _candidates(day: date):
    yield day
    rng = random.Random(day.toordinal())
    span = (NO_REPEAT_BEFORE - ARCHIVE_START).days
    for _ in range(MAX_TRIES - 1):
        yield ARCHIVE_START + timedelta(days=rng.randrange(span))


def cached_quote() -> dict | None:
    """The quote currently chosen (what the panel shows) — no network."""
    c = ds._cache_read(CURRENT)
    return c.get("data") if c else None


def get_quote(day: date | None = None, fits=lambda text: True, vetoed=()) -> dict | None:
    """Today's quote: {text, author, work, year, qotd_date, key}. See module
    docstring for the gates. Falls back to the last good quote on failure."""
    day = day or date.today()
    vetoed = {key(v) for v in vetoed}
    cur = ds._cache_read(CURRENT)
    stale = cur.get("data") if cur else None
    if stale and stale.get("key") in vetoed:
        stale = None
    if cur and cur.get("day") == day.isoformat() and stale:
        return stale
    failed = ds._cache_read(FAILED)
    if (failed and failed.get("day") == day.isoformat()
            and time.time() - failed.get("at", 0) < FAIL_BACKOFF_S):
        return stale

    hist = ds._cache_read(HISTORY) or {"keys": []}
    seen = set(hist["keys"])
    for d in _candidates(day):
        try:
            q = _qotd(d)
            if not q:
                continue
            k = key(q["text"])
            if k in vetoed or k in seen or not fits(q["text"]):
                continue
            src = _source(q["author_page"], q["text"])
            if not src:
                continue
        except Exception as e:
            ds.log.warning("quote candidate %s failed: %s", d, e)
            continue
        data = {"text": q["text"], "author": q["author"], "work": src["work"],
                "year": src["year"], "qotd_date": d.isoformat(), "key": k}
        ds._cache_write(CURRENT, {"day": day.isoformat(), "data": data})
        hist["keys"] = (hist["keys"] + [k])[-HISTORY_CAP:]
        ds._cache_write(HISTORY, hist)
        return data

    ds._cache_write(FAILED, {"day": day.isoformat(), "at": time.time()})
    ds._log_event(f"no usable Wikiquote quote for {day} after {MAX_TRIES} candidates")
    return stale

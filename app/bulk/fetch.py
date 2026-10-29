"""Fill insolvency practitioners, firm and charge holders for a list of companies.

Sources, matching the hand-filled examples:
  - Practitioners (who, and in what order): Companies House insolvency record,
    latest case, practitioners still acting. Names shortened to first + last.
  - The first name as the practitioner publishes it (e.g. "Ken", not "Kenneth"),
    and their firm: the Gazette appointment notice ("Ken Touhey (IP No. 8369) of
    Insolvency and Recovery Limited, ...").
  - Unsatisfied charge holders: Companies House charges, outstanding and
    part-satisfied only, de-duplicated, in register order.

Every company's raw findings go to a JSONL cache, so the job can stop and
resume without repeating work.


"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import logging

import httpx

log = logging.getLogger("bulk")

CH = "https://api.company-information.service.gov.uk"
GZ = "https://www.thegazette.co.uk"
CH_RATE = float(os.environ.get("BULK_CH_RATE", "1.6"))  # leaves headroom on the shared key for live alerts
APPOINT = re.compile(r"appointment of (?:joint )?(?:liquidator|administrator|provisional liquidator|receiver|"
                     r"trustee|nominee|supervisor)|notice of appointment|appointment", re.I)


# Set while an alert is being processed, so the bulk job steps aside for it.
ALERTS_BUSY = threading.Event()


class Limiter:
    """Smooth rate limit shared by all threads."""
    def __init__(self, rate, yield_to_alerts=False):
        self.gap = 1.0 / rate
        self.next = time.monotonic()
        self.lock = threading.Lock()
        self.yield_to_alerts = yield_to_alerts

    def wait(self):
        gap = self.gap * 4 if (self.yield_to_alerts and ALERTS_BUSY.is_set()) else self.gap
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next)
            self.next = t + gap
        if t > now:
            time.sleep(t - now)


ch_limit = Limiter(CH_RATE, yield_to_alerts=True)
gz_limit = Limiter(4.0)
local = threading.local()


def client(kind):
    c = getattr(local, kind, None)
    if c is None:
        if kind == "ch":
            c = httpx.Client(base_url=CH, auth=(os.environ["CH_API_KEY"], ""), timeout=30)
        else:
            c = httpx.Client(timeout=30, headers={"User-Agent": "CaseAlerts/1.0 (Christie & Co research)"},
                             follow_redirects=True)
        setattr(local, kind, c)
    return c


def get(kind, url, **kw):
    lim = ch_limit if kind == "ch" else gz_limit
    for attempt in range(6):
        lim.wait()
        try:
            r = client(kind).get(url, **kw)
        except httpx.HTTPError:
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", "30")) + 1
            log.info(f"429 from {kind}, waiting {wait}s (remain={r.headers.get('X-Ratelimit-Remain')})")
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            log.info(f"{r.status_code} from {kind} {url[:60]}")
            time.sleep(3 * (attempt + 1))
            continue
        if kind == "ch":
            rem = r.headers.get("X-Ratelimit-Remain")
            if rem is not None and int(rem) < 20:
                log.info(f"CH remain {rem}")
        return r
    return None


# ---------------------------------------------------------------- Companies House
def ch_insolvency(n):
    r = get("ch", f"/company/{n}/insolvency")
    if r is None:
        return None, "error"
    if r.status_code == 404:
        return [], "none"
    if r.status_code != 200:
        return None, f"http {r.status_code}"
    cases = []
    for c in r.json().get("cases", []):
        dates = sorted(d.get("date") for d in c.get("dates", []) if d.get("date"))
        cases.append({
            "type": c.get("type"), "number": c.get("number"), "dates": dates,
            "practitioners": [{
                "name": p.get("name"), "role": p.get("role"),
                "appointed_on": p.get("appointed_on"), "ceased_to_act_on": p.get("ceased_to_act_on"),
                "address": p.get("address") or {},
            } for p in c.get("practitioners", [])],
        })
    return cases, "ok"


def ch_charges(n):
    items, start, total = [], 0, None
    while True:
        r = get("ch", f"/company/{n}/charges", params={"items_per_page": 100, "start_index": start})
        if r is None:
            return None, "error"
        if r.status_code == 404:
            return [], "none"
        if r.status_code != 200:
            return None, f"http {r.status_code}"
        j = r.json()
        total = j.get("total_count", 0)
        page = j.get("items", [])
        for it in page:
            items.append({
                "status": it.get("status"),
                "created_on": it.get("created_on"), "delivered_on": it.get("delivered_on"),
                "satisfied_on": it.get("satisfied_on"),
                "holders": [p.get("name") for p in it.get("persons_entitled") or [] if p.get("name")],
            })
        start += len(page)
        if not page or start >= total:
            return items, "ok"


# ---------------------------------------------------------------- The Gazette
SECTION_START = re.compile(r"(Office Holder/?s?\s*:|Liquidator'?s?'?\s+name|Names? and Address(?:es)? of (?:the )?(?:Joint )?"
                           r"(?:Administrators?|Liquidators?|Supervisors?|Trustees?)|Name\(s\) of (?:Administrator|Liquidator)|"
                           r"Office Holder Details|Joint Liquidators?:|Liquidator:|Administrators?:)", re.I)
SECTION_END = re.compile(r"(Date of [Aa]ppointment|By whom [Aa]ppointed|Further details|For further|Any person who|"
                         r"Alternative contact|Ag [A-Z]{2}\d+|Company Address)")


def office_section(text):
    m = SECTION_START.search(text)
    if not m:
        return text
    rest = text[m.start():]
    e = SECTION_END.search(rest, 20)
    return rest[: e.start() if e else 1500][:1500]


def notice_text(nid):
    r = get("gz", f"{GZ}/notice/{nid}/data.xml")
    if r is None or r.status_code != 200:
        return None
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", r.text, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t)))


def gazette(n):
    r = get("gz", f"{GZ}/insolvency/notice/data.json", params={"text": f'"{n}"', "results-page-size": 50})
    if r is None or r.status_code != 200:
        return None, "error"
    e = r.json().get("entry", [])
    e = [e] if isinstance(e, dict) else e
    notices = []
    for x in e:
        cats = x.get("category")
        cats = cats if isinstance(cats, list) else [cats]
        notices.append({"date": (x.get("published") or "")[:10],
                        "type": ", ".join(c.get("@term", "") for c in cats if c),
                        "id": (x.get("id") or "").rsplit("/", 1)[-1]})
    # newest appointment notice first, then other notices; keep the office-holder text of up to two
    appts = sorted([x for x in notices if APPOINT.search(x["type"])], key=lambda x: x["date"], reverse=True)
    others = sorted([x for x in notices if not APPOINT.search(x["type"])], key=lambda x: x["date"], reverse=True)
    read = []
    for x in (appts + others)[:3]:
        txt = notice_text(x["id"])
        if not txt or n.lstrip("0").upper() not in txt.replace(" ", "").upper():
            continue  # notice must actually carry this company number
        read.append({**x, "section": office_section(txt)})
        if len(read) == 2 or (read and APPOINT.search(x["type"])):
            break
    return {"notices": read, "count": len(notices)}, "ok" if notices else "none"


# ---------------------------------------------------------------- one company
def fetch_one(n):
    out = {"n": n}
    with ThreadPoolExecutor(2) as ex:
        fg = ex.submit(gazette, n)
        ins, out["ins_status"] = ch_insolvency(n)
        chg, out["chg_status"] = ch_charges(n)
        gz, out["gz_status"] = fg.result()
    out["insolvency"], out["charges"], out["gazette"] = ins, chg, gz
    return out

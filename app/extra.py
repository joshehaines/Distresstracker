"""Columns G (Multiple Caseboard Filings), H (Property Postcode), I (Trading Name).

Only verifiable sources, strict matching:
- G: The Gazette insolvency notices found by exact company number, Companies House
  insolvency history, and earlier alerts processed by this tool. 'Yes' needs an
  earlier, distinct proceeding; 'No' only when every source was checked and none
  showed one; otherwise blank.
- H/I: FSA Food Hygiene Rating register, accepted only when the establishment
  name contains every significant word of the company name AND it sits at the
  company's registered-office postcode. Anything weaker is left blank.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

import httpx

from .companies_house import norm

GAZETTE = "https://www.thegazette.co.uk/insolvency/notice/data.json"
FSA = "https://api.ratings.food.gov.uk/Establishments"
# Only court-type proceedings count as "Caseboard filings". Voluntary-liquidation
# resolutions, liquidator appointments, creditor meetings and dividends do not.
COURT_NOTICE = re.compile(r"petition|winding[- ]up order|administrat|voluntary arrangement|moratorium|"
                          r"bankruptcy order|restoration", re.I)
NOT_COURT = re.compile(r"resolution|appointment of liquidator|meeting|dividend|release|final", re.I)
COURT_CH = {"compulsory liquidation", "administration", "in administration", "corporate voluntary arrangement",
            "moratorium", "administrative receiver"}
STOP = {"ltd", "limited", "the", "and", "co", "uk", "plc", "llp", "group", "holdings", "services",
        "international", "in", "of", "liquidation", "administration", "cyf"}


@dataclass
class Extra:
    multiple: str = ""            # Yes / No / ''
    multiple_notes: list[str] = field(default_factory=list)
    postcode: str = ""
    trading_name: str = ""
    premises_note: str = ""


def _words(s: str) -> set[str]:
    return {t for t in norm(s).split() if t not in STOP and len(t) > 1}


def _pc(s: str | None) -> str:
    return re.sub(r"\s+", "", (s or "").upper())


class Sources:
    def __init__(self, ch_client: httpx.Client):
        self.ch = ch_client
        self.web = httpx.Client(timeout=20, headers={"User-Agent": "CaseAlerts/1.0"})

    def _get(self, client, url, **kw):
        for i in range(3):
            try:
                r = client.get(url, **kw)
                if r.status_code == 429 or r.status_code >= 500:
                    time.sleep(2 * (i + 1))
                    continue
                return r
            except httpx.HTTPError:
                time.sleep(2 * (i + 1))
        return None

    # ---- G ---------------------------------------------------------------------
    def gazette(self, number: str) -> list[dict] | None:
        r = self._get(self.web, GAZETTE, params={"text": f'"{number}"', "results-page-size": 50})
        if r is None or r.status_code != 200:
            return None
        e = r.json().get("entry", [])
        e = [e] if isinstance(e, dict) else e
        out = []
        for x in e:
            cats = x.get("category")
            cats = cats if isinstance(cats, list) else [cats]
            out.append({"date": (x.get("published") or "")[:10],
                        "type": ", ".join(c.get("@term", "") for c in cats if c),
                        "id": (x.get("id") or "").rsplit("/", 1)[-1]})
        return out

    def ch_insolvency(self, number: str) -> list[dict] | None:
        r = self._get(self.ch, f"/company/{number}/insolvency")
        if r is None:
            return None
        if r.status_code == 404:
            return []
        if r.status_code != 200:
            return None
        cases = []
        for c in r.json().get("cases", []):
            dates = sorted(d.get("date") for d in c.get("dates", []) if d.get("date"))
            cases.append({"type": (c.get("type") or "").replace("-", " "), "first": dates[0] if dates else None})
        return cases

    # ---- H / I -----------------------------------------------------------------
    def registered_postcode(self, number: str) -> str | None:
        r = self._get(self.ch, f"/company/{number}/registered-office-address")
        if r is None or r.status_code != 200:
            return None
        return r.json().get("postal_code")

    def fsa_match(self, legal_name: str, ro_postcode: str | None) -> dict | None:
        if not ro_postcode:
            return None
        want = _words(legal_name)
        if not want:
            return None
        r = self._get(self.web, FSA, params={"address": ro_postcode, "pageSize": 200},
                      headers={"x-api-version": "2", "Accept": "application/json"})
        if r is None or r.status_code != 200:
            return None
        hits = [e for e in r.json().get("establishments", [])
                if _pc(e.get("PostCode")) == _pc(ro_postcode) and want <= _words(e.get("BusinessName", ""))]
        if len(hits) != 1:
            return None
        return hits[0]


def enrich_extra(cases, lookups, history: dict[str, list[str]], ch_client: httpx.Client) -> dict[str, Extra]:
    src = Sources(ch_client)
    out: dict[str, Extra] = {}
    cache: dict[str, tuple] = {}
    for c in cases:
        ex = Extra()
        lk = lookups[c.case_ref]
        out[c.case_ref] = ex
        if not lk.company_number:
            continue
        n = lk.company_number
        if n not in cache:
            cache[n] = (src.gazette(n), src.ch_insolvency(n), src.registered_postcode(n))
        gaz, ins, ro = cache[n]

        # --- G
        filed = date.fromisoformat(c.filed) if c.filed else None
        evidence = []
        prior_alerts = [r for r in history.get(n, []) if r != c.case_ref]
        if prior_alerts:
            evidence.append("other Caseboard cases " + ", ".join(dict.fromkeys(prior_alerts)))
        if gaz and filed:
            earlier = [g for g in gaz if g["date"] and date.fromisoformat(g["date"]) < filed
                       and COURT_NOTICE.search(g["type"]) and not NOT_COURT.search(g["type"])]
            if earlier:
                evidence.append("earlier Gazette: " + "; ".join(
                    f"{g['type']} {g['date']}" for g in earlier[:3]))
        if ins and filed:
            older = [i for i in ins if i["first"] and date.fromisoformat(i["first"]) < filed - timedelta(days=30)
                     and i["type"] in COURT_CH]
            if older:
                evidence.append("earlier Companies House: " + "; ".join(
                    f"{i['type']} from {i['first']}" for i in older[:3]))
        if evidence:
            ex.multiple = "Yes"
            ex.multiple_notes = evidence
        elif gaz is not None and ins is not None:
            ex.multiple = "No"
            ex.multiple_notes = ["No earlier court insolvency filings found (Gazette, Companies House, previous alerts)"]

        # --- H / I
        try:
            m = src.fsa_match(lk.legal_name or c.search_name, ro)
        except Exception:
            m = None
        if m:
            ex.postcode = m.get("PostCode", "")
            name = (m.get("BusinessName") or "").strip()
            if name and norm(name) != norm(lk.legal_name or c.search_name):
                ex.trading_name = name
            ex.premises_note = (f"Premises from FSA hygiene register (FHRS {m.get('FHRSID')}, "
                                f"{m.get('BusinessType')}).")
    return out

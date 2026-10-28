"""Companies House enrichment.

SIC codes and charges come only from the Companies House API profile of a
verified company number. Descriptions come from Companies House's published
SIC list (sic_codes.json). Nothing is inferred from names or filings.
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx

API = "https://api.company-information.service.gov.uk"
PROFILE_URL = "https://find-and-update.company-information.service.gov.uk/company/{}"
SIC = json.loads((Path(__file__).parent / "sic_codes.json").read_text())

# statuses used in the register and Notes/Outcome
VERIFIED, AMBIGUOUS, NO_MATCH, UNAVAILABLE, NA, PENDING = (
    "Verified", "Ambiguous", "No match found", "Source unavailable", "Not applicable", "Pending",
)

_SUFFIXES = [
    (r"\blimited\b", "ltd"), (r"\bltd\.?\b", "ltd"), (r"\bpublic limited company\b", "plc"),
    (r"\bcyfyngedig\b", "cyf"), (r"\bcompany\b", "co"), (r"\b&\b", "and"), (r"\buk\b", "uk"),
]


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    for pat, rep in _SUFFIXES:
        s = re.sub(pat, rep, s)
    s = re.sub(r"\((?:in\s+)?(?:liquidation|lioui?dation|administration|receivership)\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"^the\s+", "", s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass
class Lookup:
    status: str = PENDING
    company_number: str | None = None
    legal_name: str | None = None
    company_status: str | None = None
    profile_url: str | None = None
    checked: str | None = None
    sic_codes: list[str] = field(default_factory=list)
    sic_descriptions: list[str] = field(default_factory=list)
    charge_status: str | None = None      # Verified / Source unavailable
    charge_holders: list[str] = field(default_factory=list)
    charges_total: int | None = None
    attempts: list[str] = field(default_factory=list)
    reason: str = ""


class CompaniesHouse:
    def __init__(self, api_key: str | None = None, timeout: float = 20.0):
        key = api_key or os.environ.get("CH_API_KEY")
        if not key:
            raise RuntimeError("CH_API_KEY not set")
        self.client = httpx.Client(base_url=API, auth=(key, ""), timeout=timeout)
        self._last = 0.0

    # Companies House allows 600 requests / 5 min; stay well under.
    def _get(self, path: str, params: dict | None = None) -> httpx.Response | None:
        for attempt in range(4):
            wait = 0.12 - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.client.get(path, params=params)
            except httpx.HTTPError:
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "10")))
                continue
            if r.status_code >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue
            return r
        return None

    # ---- resolution -----------------------------------------------------
    def _search(self, q: str) -> list[dict] | None:
        r = self._get("/search/companies", {"q": q, "items_per_page": 50})
        if r is None or r.status_code != 200:
            return None
        return r.json().get("items", [])

    def _advanced(self, name: str) -> list[dict] | None:
        r = self._get("/advanced-search/companies", {"company_name_includes": name, "size": 100})
        if r is None:
            return None
        if r.status_code == 404:
            return []
        if r.status_code != 200:
            return None
        return [
            {"company_number": i.get("company_number"), "title": i.get("company_name"),
             "company_status": i.get("company_status")}
            for i in r.json().get("items", [])
        ]

    @staticmethod
    def _exact(items: list[dict], target: str) -> list[dict]:
        t = norm(target)
        out, seen = [], set()
        for i in items:
            if norm(i.get("title", "")) == t and i.get("company_number") not in seen:
                out.append(i)
                seen.add(i.get("company_number"))
        return out

    @staticmethod
    def _prefer_insolvent(cands: list[dict], context_insolvent: bool) -> list[dict]:
        """Several companies can share a name (one dissolved, one live).
        For an insolvency filing prefer the non-dissolved one(s)."""
        live = [c for c in cands if c.get("company_status") not in ("dissolved", "converted-closed", "removed")]
        return live if (context_insolvent and live) else cands

    def resolve(self, search_name: str, supplied_number: str | None, lk: Lookup,
                heading: str | None = None) -> None:
        if supplied_number:
            lk.attempts.append(f"profile {supplied_number} (number supplied in alert)")
            prof = self._profile(supplied_number)
            if prof is None:
                lk.status, lk.reason = UNAVAILABLE, "Companies House profile request failed for supplied number."
                return
            if prof == 404:
                lk.attempts.append("supplied number not found; falling back to name search")
            else:
                if norm(prof.get("company_name", "")) != norm(search_name):
                    lk.reason = (f"Supplied number {supplied_number} is registered as "
                                 f"'{prof.get('company_name')}', not the alert name.")
                self._fill(prof, lk)
                return

        variants = [search_name]
        alt = re.sub(r"\blimited\b", "Ltd", search_name, flags=re.I)
        if alt == search_name:
            alt = re.sub(r"\bltd\.?\b", "Limited", search_name, flags=re.I)
        if alt != search_name:
            variants.append(alt)
        no_the = re.sub(r"^the\s+", "", search_name, flags=re.I)
        # Alerts sometimes drop the "Limited" suffix; also accept the name with it added.
        targets = [search_name]
        if not re.search(r"\b(limited|ltd\.?|llp|plc|lp|cic|cio|unlimited|cyf|cyfyngedig)\s*$", search_name, re.I):
            targets.append(search_name + " Limited")
            variants.append(search_name + " Limited")

        any_ok = False
        cands: list[dict] = []
        for label, fn, q in (
            ("name search", self._search, variants[0]),
            ("name search (suffix variant)", self._search, variants[-1]),
            ("advanced name search", self._advanced, no_the),
        ):
            items = fn(q)
            lk.attempts.append(f"{label}: '{q}' -> {'error' if items is None else len(items)} results")
            if items is None:
                continue
            any_ok = True
            cands = []
            for t in targets:
                cands += [x for x in self._exact(items, t) if x not in cands]
            if cands:
                break

        if not any_ok:
            lk.status, lk.reason = UNAVAILABLE, "Companies House search failed on all three attempts."
            return
        if not cands:
            lk.status, lk.reason = NO_MATCH, "No Companies House company with this exact name after three searches."
            return
        cands = self._prefer_insolvent(cands, True)
        if len(cands) > 1:
            # the alert heading often states the insolvency status; use it to break the tie
            m = re.search(r"\((?:in\s+)?(administration|liquidation|lioui?dation|receivership)\)",
                          heading or "", re.I)
            if m:
                want = {"administration": "administration", "receivership": "receivership"}.get(
                    m.group(1).lower(), "liquidation")
                narrowed = [c for c in cands if c.get("company_status") == want]
                if len(narrowed) == 1:
                    lk.attempts.append(f"several name matches; picked the one with status '{want}' as stated in the alert")
                    cands = narrowed
        if len(cands) > 1:
            lk.status = AMBIGUOUS
            lk.reason = "Several companies share this name: " + "; ".join(
                f"{c['company_number']} ({c.get('company_status')})" for c in cands[:5])
            return
        prof = self._profile(cands[0]["company_number"])
        if prof is None or prof == 404:
            lk.status, lk.reason = UNAVAILABLE, f"Profile {cands[0]['company_number']} could not be opened."
            return
        self._fill(prof, lk)

    def _profile(self, number: str):
        r = self._get(f"/company/{number}")
        if r is None:
            return None
        if r.status_code == 404:
            return 404
        if r.status_code != 200:
            return None
        return r.json()

    def _fill(self, prof: dict, lk: Lookup) -> None:
        lk.company_number = prof.get("company_number")
        lk.legal_name = prof.get("company_name")
        lk.company_status = prof.get("company_status")
        lk.profile_url = PROFILE_URL.format(lk.company_number)
        lk.checked = date.today().isoformat()
        codes = [str(c) for c in (prof.get("sic_codes") or [])]
        lk.sic_codes = codes
        # A few official descriptions contain ';' (e.g. 69109). Column F is ';'-separated,
        # so swap the internal one for ',' to keep codes and descriptions aligned.
        lk.sic_descriptions = [SIC.get(c, f"[code {c} not in Companies House list]").replace(";", ",")
                               for c in codes]
        lk.status = VERIFIED
        if not codes:
            num = (lk.company_number or "").upper()
            why = ("LLPs do not report SIC codes to Companies House." if num[:2] in ("OC", "SO", "NC") else
                   "Overseas entity; Companies House holds no SIC codes for it." if num[:2] in ("FC", "BR", "OE", "SF", "NF") else
                   "The Companies House record lists no SIC codes.")
            lk.reason = (lk.reason + " " if lk.reason else "") + why

    # ---- charges ----------------------------------------------------------
    def charges(self, lk: Lookup) -> None:
        if not lk.company_number:
            return
        holders: list[str] = []
        start, total = 0, None
        while True:
            r = self._get(f"/company/{lk.company_number}/charges", {"items_per_page": 100, "start_index": start})
            if r is None or r.status_code not in (200, 404):
                lk.charge_status = UNAVAILABLE
                return
            if r.status_code == 404:  # no charges register
                total = 0
                break
            j = r.json()
            total = j.get("total_count", 0)
            items = j.get("items", [])
            for it in items:
                if it.get("status") in ("outstanding", "part-satisfied"):
                    for p in it.get("persons_entitled") or []:
                        n = (p.get("name") or "").strip()
                        if n and n.lower() not in (h.lower() for h in holders):
                            holders.append(n)
            start += len(items)
            if not items or start >= total:
                break
        lk.charges_total = total
        lk.charge_holders = holders
        lk.charge_status = VERIFIED


def enrich(cases, api_key: str | None = None, progress=None) -> dict[str, Lookup]:
    """Look up every company case. Returns case_ref -> Lookup.
    Individuals/unidentified get Not applicable / a specific reason."""
    ch = CompaniesHouse(api_key)
    out: dict[str, Lookup] = {}
    cache: dict[tuple, Lookup] = {}
    companies = [c for c in cases if c.subject_type == "company"]
    done = 0
    for c in cases:
        lk = Lookup()
        if c.subject_type == "individual":
            lk.status, lk.reason = NA, "Not applicable: individual proceeding"
        elif c.subject_type == "unidentified":
            lk.status, lk.reason = NO_MATCH, "Company not identified in alert, so no Companies House lookup possible."
        else:
            key = (norm(c.search_name), c.company_number)
            if key in cache:
                lk = cache[key]
            else:
                try:
                    ch.resolve(c.search_name, c.company_number, lk, c.subject_name)
                    if lk.status == VERIFIED:
                        ch.charges(lk)
                except Exception as e:  # one failure must not stop the run
                    lk.status, lk.reason = UNAVAILABLE, f"Lookup error: {type(e).__name__}"
                cache[key] = lk
            done += 1
            if progress:
                progress(done, len(companies), c)
        out[c.case_ref] = lk

    # final sweep: retry anything that failed for transport reasons
    for c in companies:
        lk = out[c.case_ref]
        if lk.status == UNAVAILABLE:
            retry = Lookup()
            try:
                ch.resolve(c.search_name, c.company_number, retry, c.subject_name)
                if retry.status == VERIFIED:
                    ch.charges(retry)
            except Exception:
                pass
            if retry.status != UNAVAILABLE:
                retry.attempts = lk.attempts + ["final retry"] + retry.attempts
                out[c.case_ref] = retry
    return out

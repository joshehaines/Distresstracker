"""One run: PDF -> register -> Companies House -> workbook -> validation -> summary."""
from __future__ import annotations

from pathlib import Path

import os

import httpx

from .companies_house import API, NA, PENDING, VERIFIED, enrich
from .extra import enrich_extra
from .extract import extract
from .workbook import build_rows, validate, write_register, write_workbook


def run(pdf: str | Path, template: str | Path, out_dir: str | Path, api_key: str | None = None,
        history: dict | None = None, progress=None) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmpl_mtime = Path(template).stat().st_mtime

    ex = extract(str(pdf))
    cases = ex["cases"]
    if not cases:
        raise ValueError("No case references found in this PDF. Is it a Caseboard alert?")

    lookups = enrich(cases, api_key=api_key, progress=progress)
    if callable(history):
        history = history([lk.company_number for lk in lookups.values()])
    history = history or {}
    if progress:
        progress(0, 0, type("C", (), {"subject_name": "Checking Gazette, insolvency history and premises"})())
    key = api_key or os.environ.get("CH_API_KEY")
    with httpx.Client(base_url=API, auth=(key, ""), timeout=20) as chc:
        extras = enrich_extra(cases, lookups, history, chc)
    rows = build_rows(cases, lookups, extras)

    date_tag = ex["alert_date"] or "undated"
    xlsx = out_dir / f"Case Alerts {date_tag}.xlsx"
    write_workbook(template, xlsx, rows)
    register = out_dir / f"Register {date_tag}.csv"
    write_register(register, cases, lookups)
    fails = validate(xlsx, template, tmpl_mtime, cases, rows) + ex["problems"]

    companies = [c for c in cases if c.subject_type == "company"]
    verified = [c for c in companies if lookups[c.case_ref].status == VERIFIED and lookups[c.case_ref].sic_codes]
    unresolved = [c for c in cases if c.subject_type != "individual"
                  and not (lookups[c.case_ref].status == VERIFIED and lookups[c.case_ref].sic_codes)]
    return {
        "alert_date": ex["alert_date"],
        "pages": ex["page_count"],
        "pages_with_text": ex["pages_with_text"],
        "records": len(rows),
        "raw_refs": ex["raw_ref_count"],
        "extraction_complete": len(rows) == ex["raw_ref_count"] and not ex["problems"],
        "companies": len(companies),
        "sic_verified": len(verified),
        "individuals": sum(c.subject_type == "individual" for c in cases),
        "unresolved": [{"case_ref": c.case_ref, "subject": c.subject_name,
                        "status": lookups[c.case_ref].status, "reason": lookups[c.case_ref].reason}
                       for c in unresolved],
        "pending": [c.case_ref for c in cases if lookups[c.case_ref].status == PENDING],
        "validation_failures": fails,
        "xlsx": str(xlsx),
        "register": str(register),
        "rows": [{
            "ref": c.case_ref, "subject": c.subject_name, "kind": c.subject_type,
            "filing": c.filing_type, "petitioner": c.petitioner,
            "number": lookups[c.case_ref].company_number or "",
            "status": lookups[c.case_ref].status,
            "sic": row["values"][4] or "", "sic_desc": row["values"][5] or "",
            "multiple": row["values"][6] or "", "postcode": row["values"][7] or "",
            "trading": row["values"][8] or "", "charges": row["values"][9] or "",
            "notes": row["values"][12] or "",
            "caseboard": row.get("caseboard_url", ""),
        } for c, row in zip(cases, rows)],
        "company_numbers": {c.case_ref: lookups[c.case_ref].company_number for c in cases
                            if lookups[c.case_ref].company_number},
    }

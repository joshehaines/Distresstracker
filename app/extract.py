"""Caseboard alert PDF -> case register.

Deterministic: every case reference in the PDF becomes exactly one register
entry. Nothing is filtered. Counts are cross-checked against a raw regex scan
of the text so a parsing slip shows up as a validation failure, not a silent
missing row.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime

import pdfplumber

# Any Caseboard-style reference: 2-4 letter list code, year, optional court code, number.
# e.g. CR-2026-007806, BR-2026-000904, CR-2026-MAN-001486, IL-2026-000012, HC-2026-LDS-000001
CASE_REF = re.compile(r"^([A-Z]{2,4}-(?:19|20)\d{2}-(?:[A-Z]{2,5}-)?\d{3,7})\b(.*)$")
CASE_REF_ANY = re.compile(r"\b[A-Z]{2,4}-(?:19|20)\d{2}-(?:[A-Z]{2,5}-)?\d{3,7}\b")
FILED = re.compile(r"^Filed:\s*(\d{2}-\d{2}-\d{4})")
ALERT_DATE = re.compile(r"Case alerts for (\d{2}-\d{2}-\d{4})")
PARTY_LABELS = (
    "Debtor", "Company", "Petitioner", "Applicant", "Respondent", "Claimant",
    "Defendant", "Third Party", "Nominee", "Creditor", "Interested Party",
    "Appellant", "Liquidator", "Administrator", "Supervisor", "Trustee",
    "Official Receiver", "Intervener",
)
PARTY_LINE = re.compile(r"^(" + "|".join(re.escape(l) for l in PARTY_LABELS) + r")\s*:\s*(.*)$")
FOOTER = re.compile(r"^Caseboard\s*\|")

COMPANY_WORDS = re.compile(
    r"\b(limited|ltd\.?|llp|plc|lp|cic|cio|company|holdings|group|unlimited|"
    r"cyfyngedig|cyf|partnership|trust|association|academ(y|ies)|incorporated|inc\.?)\b",
    re.I,
)
# company number forms seen in Caseboard headings
CRN_PATTERNS = [
    re.compile(r"\(\s*The registered number of the Company\s+is\s+([A-Z]{0,2}\d{6,8})\s*\)", re.I),
    re.compile(r"\(\s*CRN[:.\s]*([A-Z]{0,2}\d{6,8})\s*\)", re.I),
    re.compile(r"\(\s*(?:Company\s+(?:No\.?|Number)[:.\s]*)?([A-Z]{2}\d{6}|\d{7,8})\s*\)", re.I),
]
STATUS_SUFFIX = re.compile(
    r"\s*\((?:in\s+(?:creditors'?\s+voluntary\s+)?(?:liquidation|lioui?dation|administration|"
    r"receivership|members'?\s+voluntary\s+liquidation))\)\s*",
    re.I,
)
AND_OTHERS = re.compile(r"\s+and\s+others\s*$", re.I)
SOLICITOR = re.compile(r"\s*\[[^\]]*\]\s*")


@dataclass
class Party:
    role: str
    name: str          # with solicitor stripped
    raw: str


@dataclass
class Case:
    order: int
    page: int
    alert_date: str | None
    case_ref: str
    court_list: str
    filed: str | None
    proceeding: str          # full "Companies - Petition - Winding Up Petition"
    heading: str             # subject heading as printed
    parties: list[Party] = field(default_factory=list)
    # derived
    subject_name: str = ""   # proceeding subject, cleaned of CRN text
    search_name: str = ""    # normalised for Companies House search
    company_number: str | None = None
    subject_type: str = ""   # company | individual | unidentified
    filing_type: str = ""
    petitioner: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self):
        d = asdict(self)
        return d


def _read_lines(pdf_path: str) -> tuple[list[tuple[int, str]], int]:
    """Return (page, line) pairs across the whole PDF with page-number
    furniture and footers removed, plus page count."""
    out: list[tuple[int, str]] = []
    with pdfplumber.open(pdf_path) as pdf:
        n = len(pdf.pages)
        for i, page in enumerate(pdf.pages, start=1):
            lines = [l.rstrip() for l in (page.extract_text() or "").splitlines()]
            # trailing page number on its own line
            while lines and lines[-1].strip().isdigit() and len(lines[-1].strip()) <= 3:
                lines.pop()
            # leading lone page number (some exports put it at top)
            if lines and lines[0].strip() == str(i):
                lines.pop(0)
            for l in lines:
                if l.strip():
                    out.append((i, l.strip()))
    return out, n


def _clean_party(raw: str) -> str:
    return SOLICITOR.sub(" ", raw).strip(" ,")


ORG_WORDS = re.compile(r"\b(department|secretary of state|council|registrar|service|bank|hmrc|"
                       r"receiver|commissioners|authority|ministry|trustees?)\b", re.I)
ROLE_WORDS = re.compile(r"(liquidator|administrator|trustee|supervisor|receiver|nominee)", re.I)


def _is_person(name: str) -> bool:
    bare = re.sub(r"\(.*?\)", "", name)
    if COMPANY_WORDS.search(bare) or ORG_WORDS.search(bare):
        return False
    if "," not in bare:
        return False
    last = bare.split(",", 1)[0].strip()
    return 0 < len(last.split()) <= 2


def _person_name(name: str) -> str:
    """Caseboard writes individuals as 'Surname, Forenames' -> 'Forenames Surname'.
    A role in brackets (e.g. 'Joint Administrator') is kept, shortened."""
    role = ""
    m = re.search(r"\(([^)]*)\)", name)
    if m and ROLE_WORDS.search(m.group(1)):
        rm = re.search(r"(joint\s+)?(liquidator|administrator|trustee|supervisor|receiver|nominee)", m.group(1), re.I)
        role = f" ({rm.group(0).strip().capitalize()})" if rm else ""
    bare = re.sub(r"\(.*?\)", "", name).strip()
    last, first = [p.strip() for p in bare.split(",", 1)]
    return f"{first} {last}{role}".strip()


def _display(name: str) -> str:
    return _person_name(name) if _is_person(name) else name


def _filing_type(proceeding: str) -> str:
    parts = [p.strip() for p in proceeding.split(" - ") if p.strip()]
    if not parts:
        return proceeding
    if parts[0].lower() == "bankruptcy":
        kind = parts[1] if len(parts) > 1 else "Petition"
        who = parts[2] if len(parts) > 2 else ""
        return f"Bankruptcy {kind}" + (f" ({who})" if who else "")
    return parts[-1]


def _derive(c: Case) -> None:
    heading = c.heading
    crn = None
    for pat in CRN_PATTERNS:
        m = pat.search(heading)
        if m:
            crn = m.group(1).upper()
            heading = pat.sub("", heading).strip()
            break
    # company number may only be on the Company: party line
    if not crn:
        for p in c.parties:
            if p.role == "Company":
                for pat in CRN_PATTERNS:
                    m = pat.search(p.raw)
                    if m:
                        crn = m.group(1).upper()
                        break
    if crn and crn.isdigit():
        crn = crn.zfill(8)
    c.company_number = crn

    if AND_OTHERS.search(heading):
        c.notes.append("Alert names this subject 'and others'; unnamed parties not expanded.")
    c.subject_name = re.sub(r"\s{2,}", " ", heading).strip()

    search = AND_OTHERS.sub("", c.subject_name)
    search = STATUS_SUFFIX.sub(" ", search).strip()
    c.search_name = re.sub(r"\s{2,}", " ", search)

    proc_lower = c.proceeding.lower()
    roles = {p.role for p in c.parties}
    if proc_lower.startswith("bankruptcy") or "Debtor" in roles:
        c.subject_type = "individual"
    elif re.search(r"\bv\.?\s+(the\s+)?registrar of companies\b", c.subject_name, re.I):
        c.subject_type = "unidentified"
        c.notes.append("Restoration claim heading names the claimant, not the company to be restored; company not identified in alert.")
    elif COMPANY_WORDS.search(c.search_name) or crn or "Company" in roles:
        c.subject_type = "company"
    else:
        c.subject_type = "unidentified"
        c.notes.append("Subject could not be classified as a company or individual from the alert text.")

    c.filing_type = _filing_type(c.proceeding)

    pets = [_display(p.name) for p in c.parties if p.role == "Petitioner"]
    c.petitioner = "; ".join(pets)

    # context that does not belong in Petitioner goes to notes
    for p in c.parties:
        if p.role in ("Applicant", "Claimant", "Defendant", "Respondent", "Nominee", "Third Party", "Debtor"):
            if p.role == "Applicant" and p.name.lower() == c.search_name.lower():
                c.notes.append("Applicant: the company itself.")
                continue
            c.notes.append(f"{p.role}: {_display(p.name).strip()}.")


def extract(pdf_path: str) -> dict:
    lines, page_count = _read_lines(pdf_path)
    full_text = "\n".join(l for _, l in lines)

    alert_date = None
    m = ALERT_DATE.search(full_text)
    if m:
        alert_date = datetime.strptime(m.group(1), "%d-%m-%Y").date().isoformat()

    cases: list[Case] = []
    cur: Case | None = None
    stage = None  # filed -> proceeding -> heading -> parties
    for page, line in lines:
        if FOOTER.match(line):
            continue
        mref = CASE_REF.match(line)
        if mref:
            cur = Case(
                order=len(cases) + 1, page=page, alert_date=alert_date,
                case_ref=mref.group(1), court_list=mref.group(2).strip(),
                filed=None, proceeding="", heading="",
            )
            cases.append(cur)
            stage = "filed"
            continue
        if cur is None:
            continue  # email header
        if stage == "filed":
            mf = FILED.match(line)
            if mf:
                cur.filed = datetime.strptime(mf.group(1), "%d-%m-%Y").date().isoformat()
                stage = "proceeding"
                continue
            stage = "proceeding"
        if stage == "proceeding":
            cur.proceeding = line
            stage = "heading"
            continue
        mp = PARTY_LINE.match(line)
        if mp:
            role, raw = mp.group(1), mp.group(2)
            cur.parties.append(Party(role=role, name=_clean_party(raw), raw=raw))
            stage = "parties"
            continue
        if stage == "heading":
            cur.heading = (cur.heading + " " + line).strip()
            continue
        if stage == "parties" and cur.parties:
            p = cur.parties[-1]
            p.raw = (p.raw + " " + line).strip()
            p.name = _clean_party(p.raw)
            continue

    for c in cases:
        _derive(c)

    raw_refs = CASE_REF_ANY.findall(full_text)
    distinct_raw = sorted(set(raw_refs))
    parsed_refs = [c.case_ref for c in cases]
    dupes = sorted({r for r in parsed_refs if parsed_refs.count(r) > 1})

    problems = []
    # independent cross-check: every case block carries a "Filed:" line
    filed_lines = sum(1 for _, l in lines if FILED.match(l))
    if filed_lines != len(cases):
        problems.append(f"{filed_lines} 'Filed:' lines in the PDF but {len(cases)} cases parsed")
    if set(parsed_refs) != set(distinct_raw):
        problems.append(f"Parsed refs differ from raw scan: missing={sorted(set(distinct_raw)-set(parsed_refs))}")
    for c in cases:
        if not c.filed:
            problems.append(f"{c.case_ref}: no filed date")
        if not c.heading:
            problems.append(f"{c.case_ref}: no subject heading")
        if not c.proceeding:
            problems.append(f"{c.case_ref}: no proceeding type")

    return {
        "alert_date": alert_date,
        "page_count": page_count,
        "pages_with_text": len({p for p, _ in lines}),
        "raw_ref_count": max(len(distinct_raw), filed_lines),
        "duplicate_refs": dupes,
        "problems": problems,
        "cases": cases,
    }


if __name__ == "__main__":
    import json, sys
    r = extract(sys.argv[1])
    print(json.dumps({k: v for k, v in r.items() if k != "cases"}, indent=1))
    for c in r["cases"]:
        print(f"{c.order:>2} p{c.page} {c.case_ref:<20} {c.subject_type:<12} {c.company_number or '':<9} "
              f"{c.filing_type:<55} | {c.subject_name} | pet={c.petitioner} | {' '.join(c.notes)}")

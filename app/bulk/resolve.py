"""Turn cached findings into the spreadsheet values, following the hand-filled examples."""
from __future__ import annotations

import re

FIRM_SUFFIX = re.compile(r"\b(limited|ltd|llp|plc|lp|partners|associates|advisory|recovery|insolvency|"
                         r"restructuring|business recovery|accountants)\b", re.I)
ORG_UPPER = {"llp": "LLP", "llc": "LLC", "plc": "PLC", "ltd": "Ltd", "uk": "UK", "lp": "LP", "hsbc": "HSBC",
             "nv": "NV", "bv": "BV", "ag": "AG", "sa": "SA", "rbs": "RBS", "tsb": "TSB", "frp": "FRP", "btg": "BTG"}
NAME_PREFIX = re.compile(r"^(?:mr|mrs|ms|miss|dr)\.?\s+", re.I)


def tidy_org(name: str) -> str:
    """Companies House and Gazette casing ('S&W Partners Llp', 'Rook Bristol Financial Llc') -> 'LLP', 'LLC'."""
    name = re.sub(r"\s+", " ", (name or "").strip(" ,.;"))
    if name.isupper() and len(name) > 4:
        name = name.title()
    return re.sub(r"\b(llp|llc|plc|ltd|uk|lp|hsbc|nv|bv|ag|sa|rbs|tsb|frp|btg)\b", lambda m: ORG_UPPER[m.group(1).lower()], name, flags=re.I)


def short_name(full: str) -> str:
    """'John Paul Bell' -> 'John Bell'. Keeps hyphenated and Mc/O' surnames intact."""
    full = NAME_PREFIX.sub("", re.sub(r"\s+", " ", (full or "").strip()))
    parts = [p for p in full.split(" ") if p]
    if not parts:
        return ""
    if full.isupper():
        parts = [p.capitalize() if "-" not in p else "-".join(x.capitalize() for x in p.split("-")) for p in parts]
    return fix_case(parts[0] if len(parts) == 1 else f"{parts[0]} {parts[-1]}")


def fix_case(name: str) -> str:
    def word(w):
        m = re.match(r"^(Mc|O')([a-z])(.*)$", w)
        return m.group(1) + m.group(2).upper() + m.group(3) if m else w
    return " ".join(word(w) for w in name.split(" "))


def surname(full: str) -> str:
    parts = NAME_PREFIX.sub("", full or "").split()
    return parts[-1].lower().strip(".,") if parts else ""


def firm_from_address(addr: dict) -> str | None:
    """Firm named at the front of a practitioner's Companies House address, e.g.
    'Clarke Bell Limited, Suite 2 ...' or 'C/O Restructuring Department S&W Partners Llp 45 Gresham Street'."""
    line = (addr or {}).get("address_line_1") or ""
    line = re.sub(r"^(?:c/o|care of)\s+", "", line, flags=re.I)
    line = re.sub(r"^restructuring department\s+", "", line, flags=re.I)
    m = re.match(r"(.+?\b(?:limited|ltd\.?|llp|plc))\b", line, re.I)
    if m and len(m.group(1)) <= 70 and not re.match(r"^\d", m.group(1)):
        return tidy_org(m.group(1))
    return None


NAME = r"\b((?:(?:Mr|Mrs|Ms|Miss|Dr)\.?\s+)?[A-Z][A-Za-z'’\-]+(?:\s+[A-Z][A-Za-z'’\-\.]*){1,4})"
NAME_NC = NAME.replace("\\b((?:", "\\b(?:(?:", 1)
# "Ken Touhey (IP No. 8369 ) of Insolvency and Recovery Limited ," / "Zain Iqbal , of Cooper Young and,"
WITH_NO = re.compile(NAME + r"\s*,?\s*\(\s*(?:Office\s+Holder\s+Number|IP\s*(?:No\.?|Number|number)|IP)?\s*:?\s*(\d{3,6})\s*\)", re.S)
OF_FIRM = re.compile(r"^\s*,?\s*(?:\(\s*[^)]{0,30}\)\s*)?,?\s*(?:both\s+|all\s+|each\s+)?of\s+(?:the\s+firm\s+)?"
                     r"([A-Z0-9][^,;]{1,90}?)\s*(?:\s+and)?\s*(?:[,;]|\.(?!\s*(?:Limited|Ltd|LLP|Llp|PLC|Plc|UK)\b))", re.S)
NAME_OF = re.compile(NAME + r"\s*,\s*(?:\(\s*\d{3,6}\s*\)\s*,?\s*)?of\s+", re.S)
GROUP = re.compile(r"(?P<names>(?:" + NAME_NC + r"(?:\s*\(\s*(?:IP\s*No\.?)?\s*\d{3,6}\s*\))?\s*(?:,|\band\b)\s*)+" + NAME_NC +
                   r")(?:\s*\(\s*(?:IP\s*No\.?)?\s*\d{3,6}\s*\))?\s*,?\s*(?:both|all|each)\s+of\s+(?P<firm>[A-Z0-9][^,;]{1,90}?)\s*(?:[,;]|\.(?!\s*(?:Limited|Ltd|LLP|Llp|PLC|Plc|UK)\b))", re.S)
SOLE = re.compile(r"(?:name and address|Office Holder/?s?)\s*:\s*" + NAME + r"\s*,", re.S)
NOT_PERSON = re.compile(r"\b(limited|ltd|llp|plc|street|road|house|floor|court|the company|registered|office|notice|"
                        r"liquidator|administrator|insolvency|business|recovery|partners|centre|suite|unit|building)\b", re.I)


def parse_ips(section: str):
    """[(name, firm)] for the office holders named in a Gazette notice section."""
    found = []

    def add(name, start, end):
        name = re.sub(r"\s+", " ", name).strip()
        name = re.sub(r"^(?:and|both|Joint Liquidators?:?|Liquidators?:?|Administrators?:?)\s+", "", name, flags=re.I)
        if NOT_PERSON.search(name) or len(name.split()) < 2:
            return
        if any(surname(n) == surname(name) and n.split()[0].lower() == name.split()[0].lower() for n, _ in found):
            return
        tail = section[end:end + 160]
        fm = OF_FIRM.match(tail)
        firm = fm.group(1).strip() if fm else None
        found.append([name, firm])

    # "Sean Bucknall and Garry Lock , both of Quantuma LLP ," -> every name before "both of" gets the firm
    for m in GROUP.finditer(section):
        firm = re.sub(r"\s+and$", "", m.group("firm")).strip()
        for nm in re.split(r"\s*(?:,|\band\b)\s*", m.group("names")):
            nm = re.sub(r"\s*\(.*?\)\s*", " ", nm).strip()
            if nm and not NOT_PERSON.search(nm) and len(nm.split()) >= 2 and not any(
                    surname(n) == surname(nm) and n.split()[0].lower() == nm.split()[0].lower() for n, _ in found):
                found.append([re.sub(r"\s+", " ", nm), firm])
    for m in WITH_NO.finditer(section):
        add(m.group(1), m.start(), m.end())
    for m in NAME_OF.finditer(section):
        add(m.group(1), m.start(), m.start() + len(m.group(1)))
    # "Liquidator's name and address: GARY STONES, 63 WALTER ROAD" (sole practitioner, no firm named)
    if not found:
        m = SOLE.search(section)
        if m and not NOT_PERSON.search(m.group(1)):
            found.append([m.group(1).strip(), None])
    # "A (IP No. 1) and B (IP No. 2) both of Firm": fill missing firms from the next holder's
    for i in range(len(found) - 2, -1, -1):
        if not found[i][1] and found[i + 1][1]:
            found[i][1] = found[i + 1][1]
    if found and not found[-1][1]:
        prev = [f for _, f in found if f]
        if prev:
            found[-1][1] = prev[-1]
    for f in found:
        if f[1]:
            f[1] = re.sub(r"\s+and$", "", f[1]).strip()
    return found


def latest_case(cases):
    if not cases:
        return None
    return max(cases, key=lambda c: (c["dates"][-1] if c["dates"] else "", c.get("number") or ""))


def resolve(rec: dict) -> dict:
    out = {"ips": [], "firm": "", "unsatisfied": "", "unsat_dates": "", "satisfied": "", "sat_dates": "",
           "sources": []}
    gz = rec.get("gazette") or {}
    gz_ips = []
    for nt in gz.get("notices") or []:
        gz_ips = [(n, None, f) for n, f in parse_ips(nt.get("section") or "")]
        if gz_ips:
            break
    by_full = {(surname(n), n.split()[0].lower()): (n, firm) for n, _no, firm in gz_ips}
    by_surname = {}
    for n, _no, firm in gz_ips:
        by_surname.setdefault(surname(n), (n, firm))

    case = latest_case(rec.get("insolvency") or [])
    ch_ips = []
    if case:
        acting = [p for p in case["practitioners"] if not p.get("ceased_to_act_on")] or case["practitioners"]
        ch_ips = [p for p in acting if (p.get("role") or "practitioner") in ("practitioner", "receiver-manager",
                  "receiver", "proposed-liquidator", "provisional-liquidator", "administrative-receiver")] or acting

    ch_ips = [p for p in ch_ips if p.get("name")]
    official = [p for p in ch_ips if re.search(r"official receiver", p["name"], re.I)]
    if official and len(official) == len(ch_ips):
        out["ips"] = ["Official Receiver"]
        out["firm"] = "The Insolvency Service"
        out["sources"].append("Official Receiver: Companies House")
        ch_ips = []
    seen = set()
    ch_ips = [p for p in ch_ips if not (p["name"].lower() in seen or seen.add(p["name"].lower()))]

    def lookup(name):
        return by_full.get((surname(name), short_name(name).split(" ")[0].lower())) or by_surname.get(surname(name))

    if ch_ips:
        for p in ch_ips[:3]:
            g = lookup(p["name"])
            first = short_name(g[0]).split(" ")[0] if g else short_name(p["name"]).split(" ")[0]
            out["ips"].append(f"{first} {short_name(p['name']).split(' ')[-1]}".strip())
        out["sources"].append("IPs: Companies House" + (" + Gazette names" if gz_ips else ""))
        lead = lookup(ch_ips[0]["name"])
        firm = lead[1] if lead and lead[1] else None
        if not firm:
            firm = next((f for _n, _no, f in gz_ips if f), None)
            if firm:
                out["sources"].append("firm: Gazette (other office holder)")
        else:
            out["sources"].append("firm: Gazette")
        if not firm:
            firm = firm_from_address(ch_ips[0].get("address"))
            if firm:
                out["sources"].append("firm: Companies House address")
        out["firm"] = tidy_org(firm) if firm else ""
    elif gz_ips and not out["ips"]:
        out["ips"] = [short_name(n) for n, _no, _f in gz_ips[:3]]
        out["firm"] = tidy_org(gz_ips[0][2]) if gz_ips[0][2] else ""
        out["sources"].append("IPs and firm: Gazette")

    holders, dates, sat, sat_dates = [], [], [], []
    for c in rec.get("charges") or []:
        if c["status"] in ("outstanding", "part-satisfied"):
            for h in c["holders"]:
                h = tidy_org(h)
                if h.lower() not in (x.lower() for x in holders):
                    holders.append(h)
                    dates.append(c.get("created_on") or c.get("delivered_on") or "")
        elif c["status"] in ("fully-satisfied", "satisfied"):
            for h in c["holders"]:
                h = tidy_org(h)
                if h.lower() not in (x.lower() for x in sat):
                    sat.append(h)
                    sat_dates.append(c.get("satisfied_on") or "")
    out["unsatisfied"] = "; ".join(holders)
    out["unsat_dates"] = "; ".join(dates)
    out["satisfied"] = "; ".join(sat)
    out["sat_dates"] = "; ".join(sat_dates)
    out["status"] = {"insolvency": rec.get("ins_status"), "charges": rec.get("chg_status"),
                     "gazette": rec.get("gz_status")}
    return out

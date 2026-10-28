# Case Alerts

Invite-only web tool: upload a Caseboard case-alert PDF, get the CCO-sector workbook back.

1. **Extract** every case reference from the PDF (cross-checked against a raw text scan, so nothing is dropped).
2. **Enrich** each company from the Companies House Public Data API: exact-name or company-number match, every SIC code with its official description, and holders of outstanding registered charges. Individuals are marked *Not applicable*; anything unresolved gets a specific reason.
3. **Write** a new copy of the master template (Sheet1, A–N headers, formatting kept, historical rows removed) and **validate** the saved file before it can be downloaded.

Property postcode, trading name and IBIS fields are left blank — they can't be verified automatically.

## Environment

| Variable | Required | Notes |
|---|---|---|
| `CH_API_KEY` | yes | Companies House **REST** key |
| `SECRET_KEY` | yes | long random string (signs session cookies) |
| `ADMIN_EMAIL` | first start | first admin; a one-time setup link is printed to the logs |
| `ADMIN_NAME` | no | |
| `BASE_URL` | recommended | public URL, used in invite links |
| `DATA_DIR` | no | default `/data` — mount a persistent volume here |
| `COOKIE_SECURE` | no | `1` (default) behind HTTPS |

After first sign-in, upload the master template under **Admin**. It is never modified; each run works on a copy.

## Run locally

```
pip install -r requirements.txt
DATA_DIR=./data COOKIE_SECURE=0 SECRET_KEY=dev CH_API_KEY=... ADMIN_EMAIL=you@example.com \
  uvicorn app.main:app --reload
```

## Layout

- `app/extract.py` — PDF → case register
- `app/companies_house.py` — lookups, SIC, charges
- `app/workbook.py` — template writer + validation
- `app/pipeline.py` — one run end to end
- `app/main.py`, `app/db.py`, `app/templates/` — the website (FastAPI + SQLite)

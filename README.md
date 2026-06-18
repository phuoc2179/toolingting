# Anduin Portal Documents — Contact Access Report

Builds a **Contact → documents they can access** report for an Anduin firm.
Resolves every portal-document sharing target, including the matrix-based ones,
and works at large scale (e.g. 65k documents / 1,500 contacts).

There are **two ways to run it**, sharing the same logic (`anduin_portal_report.py`):

1. **Web UI** (`app.py`) — the same interface as the original HTML tool, but a
   local Python backend does all the fetching/processing. The browser only
   renders the finished report (lazily), so large firms won't crash the tab.
2. **CLI** (`anduin_portal_report.py`) — headless; writes `report.json`.

## Setup (Python 3.8+)

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate        macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
```

## Option 1 — Web UI

```bash
python app.py
# open http://127.0.0.1:5000 in your browser
```

Fill in Server, Firm ID, API key, pick the mode / investment options, and click
**Generate report**. Progress streams in the status line; when it finishes you get
the summary chips, searchable contact cards (click a contact to expand their
documents), and a JSON preview with **Download JSON** / **Copy JSON**.

Why this scales: the heavy work (fetching all documents, the async matrix/investment
exports, and the inversion) runs in Python. The browser receives the finished report
and renders contacts in pages of 100, building each contact's document table only
when you expand that contact — so the DOM stays small.

## Option 2 — CLI

```bash
python anduin_portal_report.py \
  --server https://api-minas-tirith.anduin.dev \
  --firm-id fdfym4md5jlverjn \
  --api-key YOUR_KEY \
  [--use-investment-matrix] [--investments-via-iefle] \
  [--mode bulk|per-contact] [--output report.json]
```

The API key can also come from the `ANDUIN_API_KEY` environment variable.

| Flag | Default | Meaning |
|------|---------|---------|
| `--server` / `--firm-id` / `--api-key` | (required) | Connection details |
| `--mode` | `bulk` | `bulk` (efficient) or `per-contact` (exact) |
| `--use-investment-matrix` | off | Resolve `Investments` via the investment communication matrix |
| `--investments-via-iefle` | off | Resolve `Investments` via investment → (IE, FLE) → IE×FLE×commType matrix (takes priority) |
| `--output` | `report.json` | Output file |
| `--concurrency` | `4` | Parallel requests in per-contact mode |

## How sharing targets are resolved

- `AllContacts` → every contact
- `Contacts` → the listed contact IDs (lists/FLEs are pre-expanded by the backend)
- `InvestmentEntities` → contacts assigned to those entities
- `InvestmentEntitiesInFundLegalEntity` → communication matrix (IE × FLE × commType, "yes" only)
- `Investments` → investment communication matrix, or (with `--investments-via-iefle`)
  investment → (IE, FLE) from the investments export, then the IE × FLE × commType matrix

Matrix/investment data comes from the async export endpoints
(`POST …/export` → poll `GET /api/v1/request/{id}` → download CSV). Only the exports
actually needed for the firm's documents are run. Documents that can't be resolved
with the chosen options are listed under `meta.partiallyResolvedDetails`.

## Files

- `app.py` — Flask web server (UI + API)
- `templates/index.html` — the web UI
- `anduin_portal_report.py` — all report logic + CLI
- `requirements.txt` — `requests`, `flask`

## Notes

- The web server is for **local single-user** use (`127.0.0.1`). Don't expose it
  publicly without adding auth.
- CSV parsing relies on the current export headers (`contactId`,
  `investmentEntityId`, `fundLegalEntityId`, `investmentId`, `communicationTypes`,
  and `id` in the investments export). Update the parser lookups if those change.

#!/usr/bin/env python3
"""
Anduin Portal Documents → Contact access report (CLI).

A faithful Python port of the browser tool. Builds a "Contact → documents they
can access" report for a firm, resolving every portal-document sharing target:

  - AllContacts                         → every contact
  - Contacts                            → the contactIds on the share (lists/FLEs
                                          are already expanded into contactIds by the backend)
  - InvestmentEntities                  → contacts assigned to those entities (from contact data)
  - InvestmentEntitiesInFundLegalEntity → contacts whose communication-matrix row for
                                          (IE, FLE) has the document's communicationTypeId = "yes"
  - Investments                         → either (a) the investment communication matrix directly, or
                                          (b) when --investments-via-iefle is on: investment → (IE, FLE)
                                          via the investments export, then the IE×FLE×commType matrix

Output: a single JSON report file.

Run modes:
  --mode bulk         fetch all documents once, then resolve locally (efficient; default)
  --mode per-contact  one portal-documents query per contact (exact, heavier on the server)

Usage:
  python anduin_portal_report.py \
      --server https://api-minas-tirith.anduin.dev \
      --firm-id fdfym4md5jlverjn \
      --api-key YOUR_KEY \
      [--use-investment-matrix] [--investments-via-iefle] \
      [--mode bulk|per-contact] [--output report.json] [--concurrency 4]

The API key can also be supplied via the ANDUIN_API_KEY environment variable.
"""

import argparse
import csv
import io
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("This tool needs the 'requests' package. Install it with:\n    pip install requests")


# ──────────────────────────────────────────────────────────────────────────
# API client (counts every request, mirroring netCalls in the browser tool)
# ──────────────────────────────────────────────────────────────────────────
class Client:
    def __init__(self, server, firm_id, api_key, verbose=True, on_log=None):
        self.server = server.rstrip("/")
        self.firm_id = firm_id
        self.session = requests.Session()
        self.session.headers.update({
            "authorization": "Bearer " + api_key.strip(),
            "content-type": "application/json",
        })
        self.net_calls = 0
        self._lock = threading.Lock()
        self.verbose = verbose
        self.on_log = on_log

    def _bump(self, n=1):
        with self._lock:
            self.net_calls += n

    def idm_url(self, path):
        return f"{self.server}/api/v1/idm/{self.firm_id}/{path}"

    def get(self, url, params=None):
        self._bump()
        r = self.session.get(url, params=params, timeout=120)
        if not r.ok:
            msg = f"HTTP {r.status_code} GET {url}"
            try:
                body = r.json()
                msg += ": " + (body.get("message") or json.dumps(body))
            except Exception:
                pass
            raise RuntimeError(msg)
        return r.json()

    def post(self, url, payload):
        self._bump()
        r = self.session.post(url, data=json.dumps(payload or {}), timeout=120)
        if not r.ok:
            msg = f"HTTP {r.status_code} POST {url}"
            try:
                body = r.json()
                msg += ": " + (body.get("message") or json.dumps(body))
            except Exception:
                pass
            raise RuntimeError(msg)
        return r.json()

    def download(self, file_url):
        # Pre-signed URL: no auth header, no CORS in Python.
        self._bump()
        r = requests.get(file_url, timeout=300)
        if not r.ok:
            raise RuntimeError(f"Could not download export file (HTTP {r.status_code}).")
        return r.text

    def log(self, msg):
        if self.on_log:
            try:
                self.on_log(msg)
            except Exception:
                pass
        if self.verbose:
            print(msg, file=sys.stderr, flush=True)


# ──────────────────────────────────────────────────────────────────────────
# Fetchers
# ──────────────────────────────────────────────────────────────────────────
def fetch_all_contacts(client):
    base = client.idm_url("contacts")
    offset, total, out = 0, float("inf"), []
    while offset < total:
        data = client.get(base, params={"offset": offset, "first": 100})
        total = data.get("total", 0) or 0
        page = data.get("data") or []
        out.extend(page)
        offset += len(page)
        client.log(f"  contacts… {len(out)} / {total}")
        if not page:
            break
    return out


def fetch_fle_names(client):
    """Fetch all fund legal entities (cursor-paginated) → {id: name}."""
    base = client.idm_url("fund-legal-entities")
    names, cursor, guard = {}, None, 0
    while True:
        params = {"cursor": cursor} if cursor else {}
        data = client.get(base, params=params)
        for fle in (data.get("data") or []):
            if fle and fle.get("id"):
                names[fle["id"]] = fle.get("name") or fle["id"]
        cursor = data.get("nextCursor")
        guard += 1
        if not (data.get("data") or []) or not cursor or guard >= 50:
            break
    client.log(f"  fund legal entities… {len(names)}")
    return names


def fetch_all_portal_documents(client):
    base = client.idm_url("portal-documents")
    out, cursor, page = [], None, 0
    while True:
        params = {"cursor": cursor} if cursor else {}
        data = client.get(base, params=params)
        out.extend(data.get("data") or [])
        cursor = data.get("nextCursor")
        page += 1
        client.log(f"  documents… {len(out)} loaded ({page} page{'s' if page > 1 else ''})")
        if not (data.get("data") or []) or not cursor:
            break
    return out


def fetch_docs_for_contact(client, contact_id):
    base = client.idm_url("portal-documents")
    out, cursor = [], None
    while True:
        params = {"contact-id": contact_id}
        if cursor:
            params["cursor"] = cursor
        data = client.get(base, params=params)
        out.extend(data.get("data") or [])
        cursor = data.get("nextCursor")
        if not (data.get("data") or []) or not cursor:
            break
    return out


def fetch_all_contact_docs(client, contacts, concurrency=4):
    """Per-contact mode: one query per contact, run in a small thread pool."""
    results = [None] * len(contacts)
    done = {"n": 0}
    done_lock = threading.Lock()

    def work(i, c):
        try:
            results[i] = fetch_docs_for_contact(client, c["id"])
        except Exception:
            results[i] = []
        with done_lock:
            done["n"] += 1
            if done["n"] % 25 == 0 or done["n"] == len(contacts):
                client.log(f"  querying documents… {done['n']} / {len(contacts)} contacts")

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        for i, c in enumerate(contacts):
            ex.submit(work, i, c)
    return results


# ──────────────────────────────────────────────────────────────────────────
# Async bulk-export workflow: POST …/export → requestId → poll → download CSV
# ──────────────────────────────────────────────────────────────────────────
def poll_long_request(client, request_id, label="export"):
    url = f"{client.server}/api/v1/request/{request_id}"
    for _ in range(120):  # ~3 minutes at 1.5s
        data = client.get(url)
        status = data.get("status")
        if status == "Completed":
            return data.get("result") or {}
        if status == "Failed":
            raise RuntimeError(f"{label} job failed: {data.get('error', 'unknown error')}")
        time.sleep(1.5)
    raise RuntimeError(f"{label} job timed out after ~3 minutes.")


def run_export_job(client, endpoint_path, parser, label):
    """Run a full async export (handles cursor paging); returns combined parsed rows."""
    base = client.idm_url(endpoint_path)
    all_rows, cursor, guard = [], None, 0
    while True:
        init = client.post(base, {"cursor": cursor} if cursor else {})
        request_id = init.get("requestId")
        client.log(f"  {label}: started (request {request_id})")
        result = poll_long_request(client, request_id, label)
        if result.get("fileUrl"):
            csv_text = client.download(result["fileUrl"])
            rows = parser(csv_text)
            all_rows.extend(rows)
            client.log(f"  {label}: +{len(rows)} rows (total {len(all_rows)})")
        cursor = result.get("nextCursor")
        guard += 1
        if not cursor or guard >= 50:
            break
    return all_rows


# ──────────────────────────────────────────────────────────────────────────
# CSV parsing
# ──────────────────────────────────────────────────────────────────────────
def _rows(csv_text):
    return list(csv.reader(io.StringIO(csv_text)))


def parse_yes_comm_types(raw):
    """JSON communicationTypes map → set of communicationTypeIds whose value is 'yes'."""
    yes = set()
    v = (raw or "").strip()
    if not v:
        return yes
    try:
        obj = json.loads(v)
        for k, val in obj.items():
            if str(val).lower() == "yes":
                yes.add(k)
    except Exception:
        pass
    return yes


def parse_matrix_csv(csv_text):
    """
    Communication / investment-communication matrix CSV (named headers).
    Returns rows of {contact, ie, fle, investment, yesTypes:set}.
    """
    rows = _rows(csv_text)
    if len(rows) < 2:
        return []
    header = [h.strip() for h in rows[0]]

    def idx(name):
        return header.index(name) if name in header else -1

    ci = {
        "contact": idx("contactId"),
        "ie": idx("investmentEntityId"),
        "fle": idx("fundLegalEntityId"),
        "investment": idx("investmentId"),
        "comm": idx("communicationTypes"),
    }
    out = []
    for r in rows[1:]:
        if not r or all(not str(c).strip() for c in r):
            continue

        def get(i):
            return str(r[i]).strip() if 0 <= i < len(r) else ""

        rec = {
            "contact": get(ci["contact"]) or None,
            "ie": get(ci["ie"]) or None,
            "fle": get(ci["fle"]) or None,
            "investment": get(ci["investment"]) or None,
            "yesTypes": parse_yes_comm_types(get(ci["comm"])),
        }
        if rec["contact"]:
            out.append(rec)
    return out


def parse_investments_csv(csv_text):
    """Investments export CSV (headers: id, fundLegalEntityId, investmentEntityId, …)."""
    rows = _rows(csv_text)
    if len(rows) < 2:
        return []
    header = [h.strip() for h in rows[0]]

    def idx(name):
        return header.index(name) if name in header else -1

    ci = {"id": idx("id"), "ie": idx("investmentEntityId"), "fle": idx("fundLegalEntityId")}
    out = []
    for r in rows[1:]:
        if not r or all(not str(c).strip() for c in r):
            continue

        def get(i):
            return str(r[i]).strip() if 0 <= i < len(r) else ""

        rec = {"investment": get(ci["id"]) or None, "ie": get(ci["ie"]) or None, "fle": get(ci["fle"]) or None}
        if rec["investment"]:
            out.append(rec)
    return out


# ──────────────────────────────────────────────────────────────────────────
# Map builders
# ──────────────────────────────────────────────────────────────────────────
def build_matrix_maps(cm_rows, inv_rows):
    """
    commMatrix → key '{ie}|{fle}|{commTypeId}'   → set(contactId)
    invMatrix  → key '{investmentId}|{commTypeId}' → set(contactId)
    """
    ie_fle_comm = {}
    for r in cm_rows:
        if not (r["contact"] and r["ie"] and r["fle"]):
            continue
        for ct in r["yesTypes"]:
            ie_fle_comm.setdefault(f"{r['ie']}|{r['fle']}|{ct}", set()).add(r["contact"])

    inv_comm = {}
    for r in inv_rows:
        if not (r["contact"] and r["investment"]):
            continue
        for ct in r["yesTypes"]:
            inv_comm.setdefault(f"{r['investment']}|{ct}", set()).add(r["contact"])

    return {"ieFleCommToContacts": ie_fle_comm, "invCommToContacts": inv_comm}


def build_investment_map(inv_rows):
    """investmentId → {ie, fle}."""
    m = {}
    for r in inv_rows:
        if r["investment"]:
            m[r["investment"]] = {"ie": r["ie"], "fle": r["fle"]}
    return m


def count_target_types(docs):
    target_types = ["Contacts", "InvestmentEntitiesInFundLegalEntity", "Investments",
                    "InvestmentEntities", "AllContacts"]
    counts = {t: 0 for t in target_types}
    counts["Other"] = 0
    for d in docs:
        t = (d.get("sharedWith") or {}).get("targetType")
        counts[t if t in counts else "Other"] += 1
    return counts


# ──────────────────────────────────────────────────────────────────────────
# Related funds (from the get-portal-documents response)
# ──────────────────────────────────────────────────────────────────────────
def related_funds_from_share(sw, fle_names):
    ids = set()
    if sw:
        t = sw.get("targetType")
        if t == "Contacts":
            for f in (sw.get("fundLegalEntityIds") or []):
                if f:
                    ids.add(f)
        elif t == "InvestmentEntitiesInFundLegalEntity":
            for p in (sw.get("investmentEntityInFundLegalEntity") or []):
                if p.get("fundLegalEntityId"):
                    ids.add(p["fundLegalEntityId"])
        elif t == "Investments":
            for inv_id in (sw.get("investmentIds") or []):
                fle = ".".join(str(inv_id).split(".")[:2])  # fdf….fle…
                if ".fle" in fle:
                    ids.add(fle)
    return [{"id": fid, "name": (fle_names or {}).get(fid)} for fid in ids]


# ──────────────────────────────────────────────────────────────────────────
# Core inversion: documents → per-contact access
# ──────────────────────────────────────────────────────────────────────────
def invert_docs_to_contacts(contacts, all_docs, matrices, use_investment_matrix=False,
                            use_investments_via_iefle=False):
    ie_fle_comm = matrices.get("ieFleCommToContacts") or {}
    inv_comm = matrices.get("invCommToContacts") or {}
    investment_map = matrices.get("investmentMap") or {}

    by_contact_id = {c["id"]: i for i, c in enumerate(contacts)}

    ie_to_contact_idx = {}
    for i, c in enumerate(contacts):
        for a in (c.get("assignedInvestmentEntities") or []):
            ie_to_contact_idx.setdefault(a["id"], []).append(i)

    docs_per_contact = [[] for _ in contacts]
    seen = [set() for _ in contacts]
    unresolved = []

    def add_by_idx(idx, doc):
        if idx is None or idx < 0:
            return
        if doc["id"] in seen[idx]:
            return
        seen[idx].add(doc["id"])
        docs_per_contact[idx].append(doc)

    def add_by_contact_id(cid, doc):
        add_by_idx(by_contact_id.get(cid), doc)

    for doc in all_docs:
        sw = doc.get("sharedWith") or {}
        t = sw.get("targetType")
        partial, reason = False, ""

        if t == "AllContacts":
            for i in range(len(contacts)):
                add_by_idx(i, doc)

        elif t == "Contacts":
            for cid in (sw.get("contactIds") or []):
                add_by_contact_id(cid, doc)

        elif t == "InvestmentEntities":
            for ie_id in (sw.get("investmentEntityIds") or []):
                for i in ie_to_contact_idx.get(ie_id, []):
                    add_by_idx(i, doc)

        elif t == "InvestmentEntitiesInFundLegalEntity":
            any_matched = False
            for p in (sw.get("investmentEntityInFundLegalEntity") or []):
                s = ie_fle_comm.get(f"{p.get('investmentEntityId')}|{p.get('fundLegalEntityId')}|{p.get('communicationTypeId')}")
                if s:
                    any_matched = True
                    for cid in s:
                        add_by_contact_id(cid, doc)
            if not ie_fle_comm or not any_matched:
                partial, reason = True, "no communication-matrix rows matched this entity/fund/communication-type"

        elif t == "Investments":
            comm_types = sw.get("communicationTypeIds") or []
            investment_ids = sw.get("investmentIds") or []

            if use_investments_via_iefle:
                if investment_map and ie_fle_comm:
                    any_matched = False
                    for inv_id in investment_ids:
                        inv = investment_map.get(inv_id)
                        if not inv or not inv.get("ie") or not inv.get("fle"):
                            continue
                        for ct in comm_types:
                            s = ie_fle_comm.get(f"{inv['ie']}|{inv['fle']}|{ct}")
                            if s:
                                any_matched = True
                                for cid in s:
                                    add_by_contact_id(cid, doc)
                    if not any_matched:
                        partial, reason = True, "no communication-matrix rows matched the investments' entity/fund/communication-type"
                else:
                    partial, reason = True, "investment-based sharing — investments export or communication matrix unavailable"

            elif use_investment_matrix and inv_comm:
                any_matched = False
                for inv_id in investment_ids:
                    for ct in comm_types:
                        s = inv_comm.get(f"{inv_id}|{ct}")
                        if s:
                            any_matched = True
                            for cid in s:
                                add_by_contact_id(cid, doc)
                if not any_matched:
                    partial, reason = True, "no investment-matrix rows matched these investments/communication-types"

            else:
                partial, reason = True, "investment-based sharing — enable --use-investment-matrix to resolve"

        else:
            partial, reason = True, "unknown target type"

        if partial:
            unresolved.append({"id": doc.get("id"), "name": doc.get("name"),
                               "targetType": t or "unknown", "reason": reason})

    return docs_per_contact, unresolved


# ──────────────────────────────────────────────────────────────────────────
# Report builder
# ──────────────────────────────────────────────────────────────────────────
def build_report(contacts, docs_per_contact, client, fle_names, *, query_mode,
                 unresolved, matrix_info, total_documents_from_api, target_type_counts):
    contact_reports = []
    for c, docs in zip(contacts, docs_per_contact):
        contact_reports.append({
            "contact": {
                "id": c.get("id"), "firstName": c.get("firstName"), "lastName": c.get("lastName"),
                "email": c.get("email"), "company": c.get("company"), "title": c.get("title"),
            },
            "documentCount": len(docs),
            "documents": [{
                "id": d.get("id"),
                "name": d.get("name"),
                "documentTypeId": d.get("documentTypeId"),
                "sharedWithType": (d.get("sharedWith") or {}).get("targetType"),
                "relatedFunds": related_funds_from_share(d.get("sharedWith"), fle_names),
                "sharedAt": d.get("sharedAt"),
                "effectiveDate": d.get("effectiveDate"),
                "lastModifiedAt": d.get("lastModifiedAt"),
                "sharedBy": d.get("sharedBy"),
                "checksum": d.get("checksum"),
            } for d in docs],
        })

    all_file_ids = set()
    for r in contact_reports:
        for d in r["documents"]:
            all_file_ids.add(d["id"])

    contacts_with = sum(1 for r in contact_reports if r["documentCount"] > 0)
    total_documents = total_documents_from_api if total_documents_from_api is not None else len(all_file_ids)

    meta = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "server": client.server,
        "firmId": client.firm_id,
        "queryMode": query_mode,
        "apiRequestCount": client.net_calls,
        "totalContacts": len(contacts),
        "contactsWithDocuments": contacts_with,
        "contactsWithoutDocuments": len(contacts) - contacts_with,
        "totalDocuments": total_documents,
        "totalUniqueDocuments": len(all_file_ids),
    }
    if target_type_counts:
        meta["documentsByTargetType"] = target_type_counts
    if matrix_info:
        meta["matrices"] = matrix_info
    if unresolved:
        meta["partiallyResolvedDocuments"] = len(unresolved)
        meta["partiallyResolvedNote"] = (
            "These documents use sharing targets that couldn't be fully mapped to contacts with the "
            "current settings (e.g. investment-based sharing when no matrix is enabled, or no matching "
            "matrix row). See each entry's reason; per-contact mode resolves every type exactly."
        )
        meta["partiallyResolvedDetails"] = unresolved

    return {"meta": meta, "contactDocuments": contact_reports}


def print_summary(report):
    m = report["meta"]
    print("\n=== Summary ===")
    print(f"  Contacts:           {m['totalContacts']}")
    print(f"    with documents:   {m['contactsWithDocuments']}")
    print(f"    without docs:     {m['contactsWithoutDocuments']}")
    print(f"  Documents (API):    {m['totalDocuments']}")
    print(f"  Unique mapped:      {m['totalUniqueDocuments']}")
    print(f"  API calls made:     {m['apiRequestCount']}")
    if m.get("documentsByTargetType"):
        print("  By target type:")
        for k, v in m["documentsByTargetType"].items():
            if v:
                print(f"    {k}: {v}")
    if m.get("matrices"):
        mx = m["matrices"]
        print("  Matrix records:")
        if "communicationMatrixRows" in mx:
            print(f"    IE×FLE×Type:      {mx['communicationMatrixRows']}")
        if "investmentMatrixRows" in mx:
            print(f"    Inv×Type:         {mx['investmentMatrixRows']}")
        if "investmentRecords" in mx:
            print(f"    Investments:      {mx['investmentRecords']}")
    if m.get("partiallyResolvedDocuments"):
        print(f"  ⚠ Partially resolved documents: {m['partiallyResolvedDocuments']} "
              f"(see meta.partiallyResolvedDetails in the report)")


# ──────────────────────────────────────────────────────────────────────────
# Orchestration
# ──────────────────────────────────────────────────────────────────────────
def generate(client, *, mode, use_investment_matrix, use_investments_via_iefle, concurrency,
             include_raw_documents=False):
    client.log("Step 1 — Loading contacts…")
    contacts = fetch_all_contacts(client)
    if not contacts:
        raise RuntimeError("No contacts found for this firm.")
    client.log(f"Loaded {len(contacts)} contacts.")

    client.log("Loading fund legal entity names…")
    try:
        fle_names = fetch_fle_names(client)
    except Exception as e:
        client.log(f"  (non-fatal) could not load fund legal entities: {e}")
        fle_names = {}

    matrix_info = {}
    total_documents_from_api = None
    target_type_counts = None
    raw_portal_documents = None  # raw list-portal-documents API response, for optional download

    if mode == "bulk":
        client.log("Step 2 — Fetching all portal documents…")
        all_docs = fetch_all_portal_documents(client)

        has_iefle = any((d.get("sharedWith") or {}).get("targetType") == "InvestmentEntitiesInFundLegalEntity" for d in all_docs)
        has_investments = any((d.get("sharedWith") or {}).get("targetType") == "Investments" for d in all_docs)
        need_comm_matrix = has_iefle or (has_investments and use_investments_via_iefle)

        cm_rows, inv_rows, investment_rows = [], [], []

        if need_comm_matrix:
            client.log("Step 3 — Exporting communication matrices…")
            cm_rows = run_export_job(client, "communication-matrices/export", parse_matrix_csv, "comm-matrix export")
            matrix_info["communicationMatrixRows"] = len(cm_rows)

        if has_investments and use_investments_via_iefle:
            client.log("Step 3 — Exporting investments…")
            investment_rows = run_export_job(client, "investments/export", parse_investments_csv, "investments export")
            matrix_info["investmentRecords"] = len(investment_rows)
        elif has_investments and use_investment_matrix:
            client.log("Step 3 — Exporting investment communication matrices…")
            inv_rows = run_export_job(client, "investment-communication-matrices/export", parse_matrix_csv, "inv-matrix export")
            matrix_info["investmentMatrixRows"] = len(inv_rows)

        matrix_info["investmentMatrixUsed"] = use_investment_matrix
        matrix_info["investmentsViaIEFLE"] = use_investments_via_iefle
        matrix_info["investmentTargetsPresent"] = has_investments
        matrix_info["ieFleTargetsPresent"] = has_iefle

        matrices = build_matrix_maps(cm_rows, inv_rows)
        matrices["investmentMap"] = build_investment_map(investment_rows)

        client.log("Resolving documents → contacts…")
        docs_per_contact, unresolved = invert_docs_to_contacts(
            contacts, all_docs, matrices,
            use_investment_matrix=use_investment_matrix,
            use_investments_via_iefle=use_investments_via_iefle,
        )
        total_documents_from_api = len(all_docs)
        target_type_counts = count_target_types(all_docs)
        raw_portal_documents = all_docs

    else:  # per-contact
        client.log(f"Step 2 — Querying documents per contact (concurrency={concurrency})…")
        docs_per_contact = fetch_all_contact_docs(client, contacts, concurrency=concurrency)
        unresolved = []
        uniq = {}
        for lst in docs_per_contact:
            for d in lst:
                uniq.setdefault(d["id"], d)
        target_type_counts = count_target_types(list(uniq.values()))
        raw_portal_documents = list(uniq.values())

    report = build_report(
        contacts, docs_per_contact, client, fle_names,
        query_mode=mode, unresolved=unresolved, matrix_info=matrix_info,
        total_documents_from_api=total_documents_from_api, target_type_counts=target_type_counts,
    )
    if include_raw_documents:
        report["rawPortalDocuments"] = raw_portal_documents or []
    return report


def main(argv=None):
    p = argparse.ArgumentParser(description="Build a Contact → portal-documents access report for an Anduin firm.")
    p.add_argument("--server", required=True, help="Base URL, e.g. https://api-minas-tirith.anduin.dev")
    p.add_argument("--firm-id", required=True, help="Firm ID, e.g. fdfym4md5jlverjn")
    p.add_argument("--api-key", default=os.environ.get("ANDUIN_API_KEY"),
                   help="API key (or set the ANDUIN_API_KEY environment variable)")
    p.add_argument("--mode", choices=["bulk", "per-contact"], default="bulk",
                   help="bulk (default, efficient) or per-contact (exact, heavier)")
    p.add_argument("--use-investment-matrix", action="store_true",
                   help="Resolve Investments-shared docs via the investment communication matrix")
    p.add_argument("--investments-via-iefle", action="store_true",
                   help="Resolve Investments via investment→(IE,FLE) then the IE×FLE×commType matrix "
                        "(takes priority over --use-investment-matrix)")
    p.add_argument("--output", default="report.json", help="Output JSON file (default: report.json)")
    p.add_argument("--concurrency", type=int, default=4, help="Parallel requests in per-contact mode (default: 4)")
    p.add_argument("--quiet", action="store_true", help="Suppress progress output")
    args = p.parse_args(argv)

    if not args.api_key:
        p.error("an API key is required (pass --api-key or set ANDUIN_API_KEY)")

    client = Client(args.server, args.firm_id, args.api_key, verbose=not args.quiet)

    start = time.time()
    try:
        report = generate(
            client, mode=args.mode,
            use_investment_matrix=args.use_investment_matrix,
            use_investments_via_iefle=args.investments_via_iefle,
            concurrency=args.concurrency,
        )
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print_summary(report)
    print(f"\nReport written to {args.output} in {time.time() - start:.1f}s "
          f"({client.net_calls} API calls).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

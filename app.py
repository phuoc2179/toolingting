#!/usr/bin/env python3
"""
Local web UI for the Anduin Portal Documents report.

Runs a small Flask server that serves the same UI as the original HTML tool,
but does all the heavy lifting (fetching, async exports, inversion of 65k+ docs)
in Python — so the browser only renders the finished report. No CORS, no
browser memory limits on the data work.

Usage:
    pip install -r requirements.txt
    python app.py
    # then open http://127.0.0.1:5000 in your browser

The report logic is imported wholesale from anduin_portal_report.py.
"""

import threading
import uuid

from flask import Flask, jsonify, render_template, request

import anduin_portal_report as core

app = Flask(__name__)

# In-memory job registry (single-user local tool).
JOBS = {}
LOCK = threading.Lock()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/compare")
def compare():
    # Client-side comparison of two downloaded report JSON files.
    return render_template("compare.html")


@app.route("/api/generate", methods=["POST"])
def generate():
    data = request.get_json(force=True) or {}
    server = (data.get("server") or "").strip()
    firm_id = (data.get("firmId") or "").strip()
    api_key = (data.get("apiKey") or "").strip()
    mode = data.get("mode") or "bulk"
    use_inv = bool(data.get("useInvestmentMatrix"))
    use_iefle = bool(data.get("useInvestmentsViaIEFLE"))
    try:
        concurrency = int(data.get("concurrency") or 4)
    except (TypeError, ValueError):
        concurrency = 4

    if not (server and firm_id and api_key):
        return jsonify({"error": "Server, Firm ID and API key are all required."}), 400

    job_id = uuid.uuid4().hex
    with LOCK:
        JOBS[job_id] = {"status": "running", "progress": "Starting…", "report": None, "error": None}

    def run():
        def on_log(msg):
            with LOCK:
                if job_id in JOBS:
                    JOBS[job_id]["progress"] = str(msg).strip()
        try:
            client = core.Client(server, firm_id, api_key, verbose=False, on_log=on_log)
            report = core.generate(
                client, mode=mode,
                use_investment_matrix=use_inv,
                use_investments_via_iefle=use_iefle,
                concurrency=concurrency,
            )
            with LOCK:
                JOBS[job_id].update(status="done", report=report, progress="Done.")
        except Exception as e:  # noqa: BLE001 — surface any failure to the UI
            with LOCK:
                JOBS[job_id].update(status="error", error=str(e))

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"jobId": job_id})


@app.route("/api/status/<job_id>")
def status(job_id):
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        resp = {"status": job["status"], "progress": job["progress"], "error": job["error"]}
        if job["status"] == "done":
            resp["report"] = job["report"]
    return jsonify(resp)


@app.route("/api/done/<job_id>", methods=["POST"])
def done(job_id):
    # Frontend calls this once it has the report, to free server memory.
    with LOCK:
        JOBS.pop(job_id, None)
    return jsonify({"ok": True})


if __name__ == "__main__":
    print("Open http://127.0.0.1:5000 in your browser")
    app.run(host="127.0.0.1", port=5000, threaded=True)

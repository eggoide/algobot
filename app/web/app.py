"""Flask app serving /backtest UI and JSON API.

Routes:
- GET  /backtest/               → form page
- POST /backtest/run            → start single backtest job, returns {job_id}
- POST /backtest/replay         → start live-vs-backtest replay job
- GET  /backtest/jobs           → list recent jobs
- GET  /backtest/job/<id>       → JSON status (running|done|error)
- GET  /backtest/result/<id>    → JSON full result (only when status==done)
- GET  /backtest/view/<id>      → HTML result page (chart.js)
- GET  /backtest/replay/<id>    → HTML replay comparison page
- GET  /backtest/healthz        → liveness

App is mounted at /backtest/* via nginx reverse proxy. url_prefix='/backtest' is
applied to all routes so links work from the parent dashboard.
"""

from __future__ import annotations

import os
import sys
import json
from typing import Any, Dict

from flask import Flask, jsonify, request, render_template, abort, url_for

# /app is mounted by compose
sys.path.insert(0, "/app")

from web.jobs import JOBS
from web import runner


app = Flask(
    __name__,
    static_url_path="/backtest/static",
    template_folder="templates",
    static_folder="static",
)

# All routes are also exposed under /backtest prefix (nginx proxies that path through)
# We use Blueprint for cleanliness.
from flask import Blueprint
bp = Blueprint("backtest", __name__, url_prefix="/backtest")


@bp.route("/")
def index():
    return render_template("index.html", recent=JOBS.list_recent(limit=10))


@bp.route("/healthz")
def health():
    return jsonify({"ok": True})


@bp.route("/run", methods=["POST"])
def run_backtest():
    form = request.get_json(silent=True) or request.form.to_dict()
    params = {
        "strategy": form.get("strategy", "enhanced"),
        "symbols": form.get("symbols", "").strip() or None,
        "period": form.get("period", "2y"),
        "interval": form.get("interval", "1h"),
        "start_date": form.get("start_date") or None,
        "end_date": form.get("end_date") or None,
        "benchmark": form.get("benchmark", "SPY"),
        "capital": _to_float(form.get("capital")),
        "max_positions": _to_int(form.get("max_positions")),
        "slippage_pct": _to_float(form.get("slippage_pct")),
        "fee_model": form.get("fee_model") or None,
        "use_next_open": _to_bool(form.get("use_next_open"), default=True),
        "auto_adjust": _to_bool(form.get("auto_adjust"), default=True),
        "refresh": _to_bool(form.get("refresh"), default=False),
    }
    # Strategy override
    overrides: Dict[str, Any] = {}
    for k in ("buy_drop", "sell_gain", "rsi_limit", "stop_loss", "trailing_stop_pct", "time_stop_bars"):
        v = form.get(k)
        if v not in (None, ""):
            overrides[k] = _to_float(v) if k != "time_stop_bars" else _to_int(v)
    if overrides:
        params["strategy_overrides"] = overrides

    job = JOBS.create("single", params)
    JOBS.run_async(job, runner.run_single)
    return jsonify({"job_id": job.id})


@bp.route("/replay", methods=["POST"])
def run_replay():
    form = request.get_json(silent=True) or request.form.to_dict()
    params = {
        "strategy": form.get("strategy", "enhanced"),
        "slippage_pct": _to_float(form.get("slippage_pct")),
        "fee_model": form.get("fee_model") or None,
        "use_next_open": _to_bool(form.get("use_next_open"), default=True),
    }
    job = JOBS.create("replay", params)
    JOBS.run_async(job, runner.run_replay)
    return jsonify({"job_id": job.id})


@bp.route("/job/<job_id>")
def job_status(job_id: str):
    j = JOBS.get(job_id)
    if j is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(j.to_dict())


@bp.route("/result/<job_id>")
def job_result(job_id: str):
    j = JOBS.get(job_id)
    if j is None:
        return jsonify({"error": "not found"}), 404
    if j.status != "done":
        return jsonify({"error": f"status={j.status}"}), 409
    return jsonify(j.result or {})


@bp.route("/jobs")
def jobs_list():
    return jsonify(JOBS.list_recent(limit=30))


@bp.route("/view/<job_id>")
def view_result(job_id: str):
    j = JOBS.get(job_id)
    if j is None:
        abort(404)
    return render_template("result.html", job=j.to_dict(), job_id=job_id)


@bp.route("/replay/<job_id>")
def view_replay(job_id: str):
    j = JOBS.get(job_id)
    if j is None:
        abort(404)
    return render_template("replay.html", job=j.to_dict(), job_id=job_id)


# Helpers ----------------------------------------------------------------------

def _to_float(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v):
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_bool(v, default=False):
    if v in (None, ""):
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "on")


app.register_blueprint(bp)


# Also serve at root for convenience (so direct port access works during dev)
@app.route("/")
def root_redirect():
    return render_template("index.html", recent=JOBS.list_recent(limit=10))


@app.route("/healthz")
def root_health():
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────
# Dashboard v2 control API
# Writes to /data/control.json which bot.py reads at start of each BUY cycle.
# Mounted at /v2/api/control via nginx proxy.
# ─────────────────────────────────────────────────────────
CONTROL_FILE = os.environ.get("CONTROL_FILE", "/data/control.json")


def _read_control() -> Dict[str, Any]:
    try:
        with open(CONTROL_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _write_control(payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(CONTROL_FILE), exist_ok=True)
    tmp = CONTROL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, CONTROL_FILE)


@app.route("/v2/api/control", methods=["GET", "POST"])
def v2_control():
    if request.method == "GET":
        return jsonify(_read_control())

    body = request.get_json(silent=True) or {}
    cur = _read_control()
    if "paused" in body:
        cur["paused"] = bool(body["paused"])
    cur["updated_at"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
    cur["updated_by"] = "dashboard_v2"
    _write_control(cur)
    return jsonify({"ok": True, "control": cur})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8081, debug=False)

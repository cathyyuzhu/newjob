import logging

from flask import Flask, jsonify, render_template, request

from config import load_config, save_config
from models import init_db, list_jobs, list_runs, set_job_status
from pipeline import analyze_and_record_safe
from scheduler import start_scheduler, reschedule
from scraper import run_search_once

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
init_db()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config", methods=["GET"])
def get_config():
    return jsonify(load_config())


@app.route("/api/config", methods=["POST"])
def update_config():
    cfg = load_config()
    data = request.get_json(force=True)

    for key in ("country_indeed", "tracker_xlsx_path", "base_resume_path", "resume_output_dir", "anthropic_model"):
        if key in data:
            cfg[key] = data[key]
    for key in ("results_wanted", "schedule_hour", "schedule_minute"):
        if key in data:
            cfg[key] = int(data[key])
    for key in ("keywords", "locations", "sites"):
        if key in data:
            cfg[key] = [v.strip() for v in data[key] if v and v.strip()]

    save_config(cfg)
    reschedule(cfg["schedule_hour"], cfg["schedule_minute"])
    return jsonify(cfg)


@app.route("/api/search/run", methods=["POST"])
def trigger_search():
    result = run_search_once()
    return jsonify(result)


@app.route("/api/jobs", methods=["GET"])
def get_jobs():
    status = request.args.get("status")
    return jsonify(list_jobs(status=status))


@app.route("/api/jobs/<int:job_id>/status", methods=["POST"])
def update_job_status(job_id):
    data = request.get_json(force=True)
    status = data.get("status")
    if status not in ("new", "reviewed", "dismissed"):
        return jsonify({"error": "invalid status"}), 400
    set_job_status(job_id, status)
    return jsonify({"ok": True})


@app.route("/api/jobs/<int:job_id>/analyze", methods=["POST"])
def analyze_job_route(job_id):
    try:
        result = analyze_and_record_safe(job_id)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/runs", methods=["GET"])
def get_runs():
    return jsonify(list_runs())


if __name__ == "__main__":
    start_scheduler()
    app.run(host="127.0.0.1", port=5050, debug=False)

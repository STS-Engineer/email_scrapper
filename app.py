from flask import Flask, request, jsonify, render_template
import threading
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
import os

from graph_extractor import run_search, BASE_OUTPUT_FOLDER, SEARCH_DOMAIN
from conversation_rollup import run_rollup

app = Flask(__name__)
CRON_SECRET = os.getenv("CRON_SECRET", "super_secret_fallback_key")
ENABLE_LOCAL_SCHEDULER = os.getenv("ENABLE_LOCAL_SCHEDULER", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# Each client is scraped individually to avoid Graph API search-term conflicts
NIGHTLY_CLIENTS = ["mahle", "@nidec", "valeo"]


def safe_print_str(text):
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def safe_print(message):
    print(safe_print_str(message))


# --- NEW: Global Status Tracker ---
scraping_status = {
    "is_running": False
}


def scrape_worker(domain, start_date, end_date):
    """Wraps the run_search function to update the status when finished."""
    global scraping_status
    scraping_status["is_running"] = True
    try:
        run_search(custom_domain=domain, custom_start=start_date, custom_end=end_date)
    except Exception as e:
        safe_print(f"Scraping error: {e}")
    finally:
        # No matter what happens, tell the UI we are done!
        scraping_status["is_running"] = False


def nightly_pipeline():
    """Scrapes each client individually for the last 24h, then runs conversation rollup."""
    global scraping_status
    scraping_status["is_running"] = True
    try:
        for client in NIGHTLY_CLIENTS:
            safe_print(f"=== Nightly scrape: starting client '{client}' ===")
            try:
                run_search(custom_domain=client)
            except Exception as e:
                safe_print(f"Nightly scrape error for '{client}': {e}")

        safe_print("=== Nightly scrape complete. Starting conversation rollup... ===")
        try:
            run_rollup()
        except Exception as e:
            safe_print(f"Nightly rollup error: {e}")

        safe_print("=== Nightly pipeline finished ===")
    finally:
        scraping_status["is_running"] = False


def cron_scrape_worker():
    """Runs the full nightly pipeline (used by the external trigger endpoint)."""
    nightly_pipeline()


@app.route('/')
def dashboard():
    return render_template('index.html')


@app.route('/api/status', methods=['GET'])
def get_status():
    """API endpoint for the UI to check if the scraper is currently running."""
    return jsonify(scraping_status)


@app.route('/api/scrape', methods=['POST'])
def trigger_scrape():
    global scraping_status

    # Prevent the user from spamming the button if it's already running
    if scraping_status["is_running"]:
        return jsonify({"error": "A scraping job is already currently running. Please wait."}), 400

    data = request.json
    search_domain = data.get('search_domain', SEARCH_DOMAIN)
    start_date = data.get('start_date')
    end_date = data.get('end_date')

    if not start_date or not end_date:
        return jsonify({"error": "Missing dates. Please provide start_date and end_date."}), 400

    # Pass it to our new wrapper function instead of directly to run_search
    scrape_thread = threading.Thread(target=scrape_worker, args=(search_domain, start_date, end_date))
    scrape_thread.start()

    return jsonify({
        "status": "Accepted",
        "message": f"Started scraping '{search_domain}' from {start_date} to {end_date}."
    }), 202


@app.route('/api/trigger-scrape', methods=['POST'])
def trigger_external_scrape():
    global scraping_status

    if request.headers.get("X-Cron-Secret") != CRON_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    if scraping_status["is_running"]:
        return jsonify({"error": "A scraping job is already currently running. Please wait."}), 400

    scrape_thread = threading.Thread(target=cron_scrape_worker)
    scrape_thread.start()

    return jsonify({
        "status": "Started",
        "message": "Authorized background scrape has started."
    }), 200


def start_scheduler():
    scheduler = BackgroundScheduler(timezone=pytz.timezone('Africa/Tunis'))
    scheduler.add_job(
        nightly_pipeline,
        'cron',
        hour=0,
        minute=0,
        id='nightly_pipeline',
        replace_existing=True,
    )
    scheduler.start()
    safe_print("APScheduler active. Nightly pipeline scheduled for 00:00 Africa/Tunis.")


# Start at module level only when explicitly enabled.
if ENABLE_LOCAL_SCHEDULER:
    start_scheduler()
else:
    safe_print("APScheduler disabled. Set ENABLE_LOCAL_SCHEDULER=true to enable local scheduling.")


if __name__ == '__main__':
    safe_print("Starting Avocarbon API Server...")
    app.run(host='0.0.0.0', port=5000, debug=False)

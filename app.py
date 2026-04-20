from flask import Flask, request, jsonify, render_template
import threading
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
import os

from graph_extractor import run_search, BASE_OUTPUT_FOLDER, SEARCH_DOMAIN

app = Flask(__name__)
CRON_SECRET = os.getenv("CRON_SECRET", "super_secret_fallback_key")


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


def cron_scrape_worker():
    """Runs the default scheduled scrape while updating shared status."""
    global scraping_status
    scraping_status["is_running"] = True
    try:
        run_search()
    except Exception as e:
        safe_print(f"External trigger scraping error: {e}")
    finally:
        scraping_status["is_running"] = False


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
    scheduler.add_job(run_search, 'cron', hour=16, minute=35)
    scheduler.start()
    safe_print("APScheduler is active in the background. Waiting for midnight...")


if __name__ == '__main__':
    start_scheduler()
    safe_print("Starting Avocarbon API Server...")
    app.run(host='0.0.0.0', port=5000, debug=False)

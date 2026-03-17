from flask import Flask, request, jsonify, render_template
import threading
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
import os
from graph_extractor import run_search, BASE_OUTPUT_FOLDER, SEARCH_DOMAIN

app = Flask(__name__)

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
        print(f"❌ Scraping error: {e}")
    finally:
        # No matter what happens, tell the UI we are done!
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

def start_scheduler():
    scheduler = BackgroundScheduler(timezone=pytz.timezone('Africa/Tunis'))
    scheduler.add_job(run_search, 'cron', hour=0, minute=0)
    scheduler.start()
    print("⏰ APScheduler is active in the background. Waiting for midnight...")

if __name__ == '__main__':
    if not os.path.exists(BASE_OUTPUT_FOLDER):
        os.makedirs(BASE_OUTPUT_FOLDER)
        
    start_scheduler()
    print("🌐 Starting Avocarbon API Server...")
    app.run(host='0.0.0.0', port=5000, debug=False)
"""
Ain Real Estate — KPI & Sales Intelligence System
Entry point for Gunicorn and direct execution
"""
import os
import logging
from app import create_app
from config import Config

log = logging.getLogger(__name__)

app = create_app()

# Start background sync scheduler unless disabled
if Config.DISABLE_SYNC:
    log.info("⏸️  DISABLE_SYNC=true — Master V sync scheduler is OFF")
else:
    try:
        from app.sync_service import start_sync_scheduler
        start_sync_scheduler()
    except Exception as e:
        log.error(f"⚠️  Failed to start sync scheduler: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    debug = os.environ.get("FLASK_DEBUG") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)

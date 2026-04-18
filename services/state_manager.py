import json
import os
from datetime import datetime, timedelta
import pytz
from .sheets import GoogleSheetsManager
from config import GOOGLE_SHEETS_CREDENTIALS, SHEET_ID, CENTRAL_TZ

gs_manager = GoogleSheetsManager(GOOGLE_SHEETS_CREDENTIALS, SHEET_ID)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATUS_FILE = os.path.join(BASE_DIR, "draft_status.json")

draft_state = {    
    "running": False,
    "timer_paused": False,
    "current_pick_index": 0,
    "trade_in_progress": False,
    "picks": [],
    "teams": {},
    "users": {},
    "prospects": {},
    "positions": {},
    "warning_sent": None,
    "last_sync": "Never"
}

def save_status():
# Saves the current running/paused state to a local file.
    with open(STATUS_FILE, "w") as f:
        data = {
            "running": draft_state["running"],
            "timer_paused": draft_state["timer_paused"]
        }
        json.dump(data, f)

def load_status():
 # Loads the state back into the draft_state dictionary on startup.
    try:
        with open(STATUS_FILE, "r") as f:
            data = json.load(f)
            draft_state["running"] = data.get("running", False)
            draft_state["timer_paused"] = data.get("timer_paused", False)
    except FileNotFoundError:
    # If the file doesn't exist yet, just keep the defaults
        pass

async def load_data():
    try:
        draft_state["teams"] = gs_manager.load_teams()
        draft_state["users"] = gs_manager.load_users()
        draft_state["prospects"] = gs_manager.load_prospects()
        draft_state["picks"] = gs_manager.load_picks()
        draft_state["positions"] = gs_manager.load_positions()
        draft_state["last_sync"] = datetime.now(CENTRAL_TZ).strftime("%H:%M:%S")
    except Exception as e:
        print(f"Error loading data: {e}")
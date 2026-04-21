import os
import json
import pytz
from dotenv import load_dotenv

load_dotenv()

ADMIN_IDS = json.loads(os.getenv("ADMIN_USERS", "[]"))
ALLOWED_CHANNELS = json.loads(os.getenv("ALLOWED_CHANNELS", "[]"))
REMINDER_CHANNEL_ID = int(os.getenv("REMINDER_CHANNEL_ID", 0))
PICK_CHANNEL_ID = int(os.getenv("PICK_CHANNEL_ID", 0))
SHEET_ID = os.getenv("GOOGLE_SHEET_ID", "")
GOOGLE_SHEETS_CREDENTIALS = "credentials.json"
CENTRAL_TZ = pytz.timezone('America/Chicago')
PICK_TIME_HOURS = 2
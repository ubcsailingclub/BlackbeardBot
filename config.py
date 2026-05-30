import os
from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

TOKEN = os.getenv("DISCORD_TOKEN", "")
WA_API_KEY = os.getenv("WA_API_KEY", "")
WA_ACCOUNT_ID = int(os.getenv("WA_ACCOUNT_ID", "0"))

VERIFY_CHANNEL_NAME = "get-verified"  # Discord server channel used to get verified
VERIFY_MESSAGE_STATE_FILE = "data/verify_message.json"

WA_API_VERSION = "v2.1"

ROLE_SOCIAL = "social"
ROLE_SWABBIE = "Swabbie"

GET_ROLES_CHANNEL_NAME = "get-roles"
ROLE_PANEL_STATE_FILE = "data/role_panels.json"

# Google Sheets and Workhours Config
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "data/google_credentials.json")
GOOGLE_SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID", "")
WORKHOURS_SHEET_NAME = os.getenv("WORKHOURS_SHEET_NAME", "Form Responses 1")
VERIFIED_MEMBERS_FILE = os.getenv("VERIFIED_MEMBERS_FILE", "data/verified_members.json")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Check your .env file.")

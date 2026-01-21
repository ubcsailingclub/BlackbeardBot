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

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Check your .env file.")

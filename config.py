from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()

BOT_TOKEN = os.getenv("DISCORD_TOKEN", "")
SCRIPT_TIMEOUT = int(os.getenv("SCRIPT_TIMEOUT", "60"))
SCRIPTS_DIR = Path(__file__).parent / "scripts"
SCHEDULES_DIR = Path(__file__).parent / "schedules"
SHORTCUTS_DIR = Path(__file__).parent / "shortcuts"
PERMISSIONS_PATH = Path(__file__).parent / "permissions.json"

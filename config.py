# config.py
from pathlib import Path
from datetime import timedelta

# Project root and uploads dir
PROJECT_ROOT = Path(__file__).resolve().parent
UPLOAD_DIR = PROJECT_ROOT / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Rate limiting for registrations (per browser session)
MAX_REG_ATTEMPTS = 5
REG_RATE_WINDOW = timedelta(minutes=10)

# Global CSS for layout
CUSTOM_CSS = """
"""  # you can put your CSS here

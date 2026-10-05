import os

# config.py

# --- Google Sheets ---
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID')
WORKSHEET_NAME = 'Ad Scraper'
CREDENTIALS_FILE = 'creds.json'

# --- Scraper settings ---
HEADLESS = False

WAIT_TIMEOUT = 20      # Seconds to wait for the video to load after clicking play

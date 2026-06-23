import gspread
from oauth2client.service_account import ServiceAccountCredentials
import config
import time
from datetime import datetime, timedelta
import uuid

# --------------------------
# Cached sheet to reduce API reads
# --------------------------
SHEET_CACHE = None
SHEET_CACHE_TIME = None
SHEET_CACHE_TTL = 60  # seconds

CLAIM_AGENT_COL = 9
CLAIM_TIME_COL = 10
CLAIM_TOKEN_COL = 11
CLAIM_STATUS_COL = 12
CLAIM_TTL_MINUTES = 5  # adjust to 370 for production

LOG_BATCH_SIZE = 5  # batch logs to reduce API calls
LOG_CACHE = []
WRITE_LOGS = False 
# --------------------------
# Sheet auth
# --------------------------
def get_sheet():
    global SHEET_CACHE, SHEET_CACHE_TIME
    now = time.time()
    if SHEET_CACHE and SHEET_CACHE_TIME and now - SHEET_CACHE_TIME < SHEET_CACHE_TTL:
        return SHEET_CACHE

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(config.CREDENTIALS_FILE, scope)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(config.SPREADSHEET_ID).worksheet(config.WORKSHEET_NAME)

    SHEET_CACHE = sheet
    SHEET_CACHE_TIME = now
    return sheet


def get_spreadsheet():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(config.CREDENTIALS_FILE, scope)
    client = gspread.authorize(creds)
    return client.open_by_key(config.SPREADSHEET_ID)

# --------------------------
# Logs disabled
# --------------------------
WRITE_LOGS = False

def flush_logs():
    """Logs disabled - do nothing"""
    global LOG_CACHE
    LOG_CACHE = []
    return


def add_log(row_number="", status="", log_type="", url="", video_id="", app_link="", message=""):
    """Logs disabled - do nothing"""
    return


# --------------------------
# Agent helpers
# --------------------------
def ensure_agent_headers():
    sheet = get_sheet()
    headers = sheet.row_values(1)
    required = {9: "Agent", 10: "Claim Time", 11: "Claim Token", 12: "Claim Status", 13: "Headline", 14: "Description"}
    updates = []
    for col, name in required.items():
        current = headers[col - 1] if len(headers) >= col else ""
        if current != name:
            col_letter = chr(64 + col) if col <= 26 else chr(64 + (col // 26)) + chr(64 + (col % 26))
            updates.append({"range": f"{col_letter}1", "values": [[name]]})
    if updates:
        sheet.batch_update(updates)


def is_claim_expired(claim_time_text):
    if not claim_time_text:
        return True
    try:
        claim_time = datetime.strptime(claim_time_text, "%Y-%m-%d %H:%M:%S")
        return datetime.now() - claim_time > timedelta(minutes=CLAIM_TTL_MINUTES)
    except Exception:
        return True


def is_processed_video_value(value):
    value = str(value or "").strip()
    return bool(value)


# --------------------------
# Sheet snapshot with retry
# --------------------------
def get_agent_rows_snapshot():
    ensure_agent_headers()
    sheet = get_sheet()

    for attempt in range(5):
        try:
            values = sheet.get_all_values()
            break
        except gspread.exceptions.APIError as e:
            if hasattr(e, "response") and e.response.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"⚠ 429 rate limit hit, retrying in {wait}s")
                time.sleep(wait)
            else:
                raise
    else:
        raise Exception("Failed to read sheet after retries due to 429")

    rows = []
    for idx in range(1, len(values)):
        row_num = idx + 1
        row = values[idx]

        url = row[7].strip() if len(row) >= 8 else ""
        video_id = row[5].strip() if len(row) >= 6 else ""
        stop_flag = row[12].strip() if len(row) >= 13 else ""  # optional Stop Flag column

        claim_agent = row[8].strip() if len(row) >= 9 else ""
        claim_time = row[9].strip() if len(row) >= 10 else ""
        claim_token = row[10].strip() if len(row) >= 11 else ""
        claim_status = row[11].strip() if len(row) >= 12 else ""

        if not url:
            continue

        rows.append({
            "row_num": row_num,
            "url": url,
            "video_id": video_id,
            "claim_agent": claim_agent,
            "claim_time": claim_time,
            "claim_token": claim_token,
            "claim_status": claim_status,
            "stop_flag": stop_flag,
            "processed": is_processed_video_value(video_id),
            "claim_expired": is_claim_expired(claim_time)
        })
    return rows


# --------------------------
# Agent row handling
# --------------------------
def count_unprocessed_rows():
    rows = get_agent_rows_snapshot()
    return sum(1 for r in rows if r["url"] and not r["processed"])


def get_next_agent_task(direction, agent_name, run_id):
    direction = direction.lower().strip()
    if direction not in ["top", "bottom"]:
        raise ValueError("direction must be 'top' or 'bottom'")

    sheet = get_sheet()  # needed to update claims
    rows = get_agent_rows_snapshot()
    unprocessed = [r for r in rows if r["url"] and not r["processed"]]

    if not unprocessed:
        return None

    if len(unprocessed) == 1 and direction == "bottom":
        try:
            add_log(row_number="", status="COLLISION_STOP", log_type=agent_name,
                    message="Only one unprocessed row left. Bottom agent stopped to avoid collision.")
            flush_logs()
        except Exception:
            pass
        return "COLLISION_STOP"

    candidates = sorted(unprocessed, key=lambda x: x["row_num"], reverse=(direction=="bottom"))

    for candidate in candidates:
        row_num = candidate["row_num"]
        url = candidate["url"]

        if candidate["stop_flag"].upper() == "STOP":
            print(f"🛑 {agent_name}: Stop flag detected. Stopping agent.")
            return "COLLISION_STOP"

        if candidate["claim_agent"] and candidate["claim_agent"] != agent_name and not candidate["claim_expired"]:
            continue

        token = f"{agent_name}-{run_id}-{uuid.uuid4().hex[:10]}"
        claim_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Claim row
        sheet.update(f"I{row_num}:L{row_num}", [[agent_name, claim_time, token, "CLAIMED"]])

        # Confirm claim
        confirm = sheet.row_values(row_num)
        confirmed_token = confirm[10].strip() if len(confirm) >= 11 else ""

        if confirmed_token == token:
            return row_num, url

    return None


def mark_agent_done(row_num, agent_name):
    try:
        sheet = get_sheet()
        sheet.update_cell(row_num, CLAIM_STATUS_COL, "DONE")
    except Exception:
        pass


def update_combined_row(row_index, data):
    """Writes combined row data to columns A-G"""
    sheet = get_sheet()
    cell_range = f"A{row_index}:G{row_index}"
    try:
        sheet.update(cell_range, [data])
    except gspread.exceptions.APIError as e:
        print(f"⚠ Failed to update row {row_index}: {e}")


def update_headline_and_description(row_index, headline, description):
    """Writes Headline and Description directly to columns M-N"""
    sheet = get_sheet()
    cell_range = f"M{row_index}:N{row_index}"
    try:
        sheet.update(cell_range, [[headline, description]])
    except gspread.exceptions.APIError as e:
        print(f"⚠ Failed to update headline/desc for row {row_index}: {e}")

# Add the get_urls_with_retry helper function which was originally called in SCRAPEER.py
def get_urls_with_retry():
    """Helper to fetch column H (transparency URLs) from sheet"""
    rows = get_agent_rows_snapshot()
    # Need to return full list matching row positions for combined scraper iteration
    sheet = get_sheet()
    col_values = sheet.col_values(8) # Assuming H is col 8
    # strip headers
    if len(col_values) > 1:
        return col_values[1:]
    return []

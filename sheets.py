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

CLAIM_AGENT_COL = 9       # I
CLAIM_TIME_COL = 10       # J
CLAIM_TOKEN_COL = 11      # K
CLAIM_STATUS_COL = 12     # L
HEADLINE_COL = 13         # M
DESCRIPTION_COL = 14      # N
IMAGE_URL_COL = 15        # O
CLAIM_TTL_MINUTES = 5  # adjust to 370 for production

LOG_BATCH_SIZE = 5  # batch logs to reduce API calls
LOG_CACHE = []


# --------------------------
# Small helpers
# --------------------------
def col_to_letter(col_num):
    """Convert 1-based column number to Google Sheets column letter."""
    result = ""
    while col_num:
        col_num, remainder = divmod(col_num - 1, 26)
        result = chr(65 + remainder) + result
    return result


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

    required = {
        CLAIM_AGENT_COL: "Agent",
        CLAIM_TIME_COL: "Claim Time",
        CLAIM_TOKEN_COL: "Claim Token",
        CLAIM_STATUS_COL: "Claim Status",
        HEADLINE_COL: "Headline",
        DESCRIPTION_COL: "Description",
        IMAGE_URL_COL: "Image URL",
        
    }

    updates = []
    for col, name in required.items():
        current = headers[col - 1] if len(headers) >= col else ""
        if current != name:
            col_letter = col_to_letter(col)
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

        url = row[7].strip() if len(row) >= 8 else ""          # H
        video_id = row[5].strip() if len(row) >= 6 else ""     # F

        claim_agent = row[CLAIM_AGENT_COL - 1].strip() if len(row) >= CLAIM_AGENT_COL else ""
        claim_time = row[CLAIM_TIME_COL - 1].strip() if len(row) >= CLAIM_TIME_COL else ""
        claim_token = row[CLAIM_TOKEN_COL - 1].strip() if len(row) >= CLAIM_TOKEN_COL else ""
        claim_status = row[CLAIM_STATUS_COL - 1].strip() if len(row) >= CLAIM_STATUS_COL else ""

        # STOP flag moved to P, because M/N/O are now Headline, Description, Image URL.
        stop_flag = ""

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

    candidates = sorted(unprocessed, key=lambda x: x["row_num"], reverse=(direction == "bottom"))

    for candidate in candidates:
        row_num = candidate["row_num"]
        url = candidate["url"]

      

        if candidate["claim_agent"] and candidate["claim_agent"] != agent_name and not candidate["claim_expired"]:
            continue

        token = f"{agent_name}-{run_id}-{uuid.uuid4().hex[:10]}"
        claim_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Claim row in I:L
        sheet.update(f"I{row_num}:L{row_num}", [[agent_name, claim_time, token, "CLAIMED"]])

        # Confirm claim
        confirm = sheet.row_values(row_num)
        confirmed_token = confirm[CLAIM_TOKEN_COL - 1].strip() if len(confirm) >= CLAIM_TOKEN_COL else ""

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
    """Writes combined row data to columns A-G with verification."""
    sheet = get_sheet()
    cell_range = f"A{row_index}:G{row_index}"

    print("SHEET DEBUG ROW:", row_index)
    print("SHEET DEBUG RANGE:", cell_range)
    print("SHEET DEBUG DATA:", data)

    try:
        result = sheet.update(
            cell_range,
            [data],
            value_input_option="USER_ENTERED"
        )

        print("SHEET UPDATE SUCCESS:", result)

        verify = sheet.row_values(row_index)
        print("SHEET AFTER WRITE A:G:", verify[:7])

    except Exception as e:
        print(f"⚠ Failed to update row {row_index}: {e}")



def update_headline_and_description(row_index, headline, description):
    """Writes Headline and Description directly to columns M-N."""
    sheet = get_sheet()
    cell_range = f"M{row_index}:N{row_index}"
    try:
        sheet.update(cell_range, [[headline or "N/A", description or "N/A"]])
    except gspread.exceptions.APIError as e:
        print(f"⚠ Failed to update headline/desc for row {row_index}: {e}")


def update_image_url(row_index, image_url):
    """Writes Image URL directly to column O."""
    sheet = get_sheet()
    cell_range = f"O{row_index}"
    try:
        sheet.update(cell_range, [[image_url or "N/A"]], value_input_option="USER_ENTERED")
    except gspread.exceptions.APIError as e:
        print(f"⚠ Failed to update image URL for row {row_index}: {e}")


# Add the get_urls_with_retry helper function which was originally called in SCRAPEER.py
def get_urls_with_retry():
    """Helper to fetch column H (transparency URLs) from sheet."""
    get_agent_rows_snapshot()  # ensures headers and validates sheet access
    sheet = get_sheet()
    col_values = sheet.col_values(8)  # H
    # strip headers
    if len(col_values) > 1:
        return col_values[1:]
    return []

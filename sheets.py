import gspread
from oauth2client.service_account import ServiceAccountCredentials
import config
import time
import random
from datetime import datetime, timedelta
import uuid

# --------------------------
# Cached sheet to reduce API reads/auth calls
# --------------------------
SHEET_CACHE = None
SHEET_CACHE_TIME = None
SHEET_CACHE_TTL = 300  # seconds

# Header cache prevents row_values(1) on every task pickup
HEADERS_CACHE = None
HEADERS_CACHE_TIME = None
HEADERS_CACHE_TTL = 3600  # seconds
HEADERS_READY = False

CLAIM_AGENT_COL = 9
CLAIM_TIME_COL = 10
CLAIM_TOKEN_COL = 11
CLAIM_STATUS_COL = 12

# Batch settings
AGENT_BATCH_SIZE = 100

# Keep this long enough for one agent to process a full 100-row batch.
# Rows are still marked DONE one-by-one immediately after processing.
CLAIM_TTL_MINUTES = 370

# Keep parallel runners allowed, but reduce same-second API bursts
CLAIM_CONFIRM_DELAY = 0.8

LOG_BATCH_SIZE = 5  # batch logs to reduce API calls
LOG_CACHE = []
WRITE_LOGS = False

# Retry settings for Google Sheets 429 / transient errors
SHEETS_MAX_RETRIES = 8
SHEETS_BASE_DELAY = 2
SHEETS_MAX_DELAY = 60


# --------------------------
# Google Sheets API helpers
# --------------------------
def is_retryable_sheets_error(error):
    """Return True for quota/rate limit and temporary server errors."""
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    text = str(error)

    return (
        status_code in (429, 500, 502, 503, 504)
        or "Quota exceeded" in text
        or "RESOURCE_EXHAUSTED" in text
        or "Read requests per minute" in text
        or "429" in text
    )


def sheets_call(func, *args, max_retries=SHEETS_MAX_RETRIES, **kwargs):
    """Run a gspread call with exponential backoff and jitter."""
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            if not is_retryable_sheets_error(e) or attempt == max_retries - 1:
                raise

            wait = min(SHEETS_MAX_DELAY, SHEETS_BASE_DELAY * (2 ** attempt))
            wait += random.uniform(0.5, 2.0)
            print(f"⚠ Google Sheets quota/temporary error. Retry {attempt + 1}/{max_retries} in {wait:.1f}s")
            time.sleep(wait)


def column_letter(col):
    """Convert 1-based column number to A1 column letter."""
    result = ""
    while col:
        col, remainder = divmod(col - 1, 26)
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

    spreadsheet = sheets_call(client.open_by_key, config.SPREADSHEET_ID)
    sheet = sheets_call(spreadsheet.worksheet, config.WORKSHEET_NAME)

    SHEET_CACHE = sheet
    SHEET_CACHE_TIME = now
    return sheet


def get_spreadsheet():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(config.CREDENTIALS_FILE, scope)
    client = gspread.authorize(creds)
    return sheets_call(client.open_by_key, config.SPREADSHEET_ID)


# --------------------------
# Logs disabled
# --------------------------
WRITE_LOGS = False


def flush_logs():
    """Logs disabled - do nothing."""
    global LOG_CACHE
    LOG_CACHE = []
    return


def add_log(row_number="", status="", log_type="", url="", video_id="", app_link="", message=""):
    """Logs disabled - do nothing."""
    return


# --------------------------
# Agent helpers
# --------------------------
def ensure_agent_headers():
    """Ensure agent columns exist without reading header row on every call."""
    global HEADERS_CACHE, HEADERS_CACHE_TIME, HEADERS_READY

    now = time.time()
    if HEADERS_READY and HEADERS_CACHE and HEADERS_CACHE_TIME and now - HEADERS_CACHE_TIME < HEADERS_CACHE_TTL:
        return HEADERS_CACHE

    sheet = get_sheet()
    headers = sheets_call(sheet.row_values, 1)

    required = {
        9: "Agent",
        10: "Claim Time",
        11: "Claim Token",
        12: "Claim Status",
        13: "Headline",
        14: "Description",
    }

    updates = []
    for col, name in required.items():
        current = headers[col - 1] if len(headers) >= col else ""
        if current != name:
            updates.append({"range": f"{column_letter(col)}1", "values": [[name]]})

    if updates:
        sheets_call(sheet.batch_update, updates)

        # Keep local cache aligned with the required headers after update.
        max_required_col = max(required)
        if len(headers) < max_required_col:
            headers.extend([""] * (max_required_col - len(headers)))
        for col, name in required.items():
            headers[col - 1] = name

    HEADERS_CACHE = headers
    HEADERS_CACHE_TIME = now
    HEADERS_READY = True
    return headers


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


def _first_cell_value(value_range):
    """Safely read the first cell from a gspread get/batch_get response item."""
    if not value_range:
        return ""
    if isinstance(value_range, list) and value_range and isinstance(value_range[0], list) and value_range[0]:
        return str(value_range[0][0]).strip()
    return ""


# --------------------------
# Sheet snapshot with retry
# --------------------------
def get_agent_rows_snapshot():
    ensure_agent_headers()
    sheet = get_sheet()

    # One read for the full snapshot. Do not add separate row/column reads here.
    values = sheets_call(sheet.get_all_values)

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
            "claim_expired": is_claim_expired(claim_time),
        })

    return rows


# --------------------------
# Agent row handling
# --------------------------
def count_unprocessed_rows():
    rows = get_agent_rows_snapshot()
    return sum(1 for r in rows if r["url"] and not r["processed"])


def get_next_agent_tasks(direction, agent_name, run_id, batch_size=AGENT_BATCH_SIZE):
    """
    Claim up to batch_size unprocessed rows and return a list of tasks:
        [(row_num, url), (row_num, url), ...]

    Returns:
        [] if no rows are available
        "COLLISION_STOP" if the bottom agent should stop or a STOP flag is found

    Important:
        The caller should process each returned task and call mark_agent_done(row_num, agent_name)
        immediately after that row finishes.
    """
    direction = direction.lower().strip()
    if direction not in ["top", "bottom"]:
        raise ValueError("direction must be 'top' or 'bottom'")

    try:
        batch_size = max(1, int(batch_size))
    except Exception:
        batch_size = AGENT_BATCH_SIZE

    sheet = get_sheet()
    rows = get_agent_rows_snapshot()
    unprocessed = [r for r in rows if r["url"] and not r["processed"]]

    if not unprocessed:
        return []

    if len(unprocessed) == 1 and direction == "bottom":
        try:
            add_log(
                row_number="",
                status="COLLISION_STOP",
                log_type=agent_name,
                message="Only one unprocessed row left. Bottom agent stopped to avoid collision.",
            )
            flush_logs()
        except Exception:
            pass
        return "COLLISION_STOP"

    candidates = sorted(unprocessed, key=lambda x: x["row_num"], reverse=(direction == "bottom"))

    attempted_claims = []
    updates = []

    for candidate in candidates:
        if len(attempted_claims) >= batch_size:
            break

        row_num = candidate["row_num"]
        url = candidate["url"]

        if candidate["stop_flag"].upper() == "STOP":
            print(f"🛑 {agent_name}: Stop flag detected. Stopping agent.")
            return "COLLISION_STOP"

        # Skip rows actively claimed by another agent.
        if candidate["claim_agent"] and candidate["claim_agent"] != agent_name and not candidate["claim_expired"]:
            continue

        token = f"{agent_name}-{run_id}-{uuid.uuid4().hex[:10]}"
        claim_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        attempted_claims.append({
            "row_num": row_num,
            "url": url,
            "token": token,
        })

        updates.append({
            "range": f"I{row_num}:L{row_num}",
            "values": [[agent_name, claim_time, token, "CLAIMED"]],
        })

    if not attempted_claims:
        return []

    # Claim all selected rows in one batch write.
    sheets_call(sheet.batch_update, updates)

    # Small delay helps parallel agents settle before confirming the winning claims.
    time.sleep(CLAIM_CONFIRM_DELAY)

    # Confirm every claim token in one batch read.
    confirm_ranges = [f"K{item['row_num']}:K{item['row_num']}" for item in attempted_claims]
    confirm_values = sheets_call(sheet.batch_get, confirm_ranges)

    confirmed_tasks = []
    for item, value_range in zip(attempted_claims, confirm_values):
        confirmed_token = _first_cell_value(value_range)
        if confirmed_token == item["token"]:
            confirmed_tasks.append((item["row_num"], item["url"]))

    return confirmed_tasks


def get_next_agent_task(direction, agent_name, run_id):
    """
    Backwards-compatible single-row task claim.
    Existing code that calls get_next_agent_task() will still work.
    """
    tasks = get_next_agent_tasks(direction, agent_name, run_id, batch_size=1)

    if tasks == "COLLISION_STOP":
        return "COLLISION_STOP"

    if not tasks:
        return None

    return tasks[0]


def mark_agent_done(row_num, agent_name=""):
    """
    Mark one row as DONE immediately after it has finished processing.

    Use this inside the per-row processing loop, not after the whole batch.
    """
    try:
        sheet = get_sheet()
        sheets_call(sheet.update_cell, row_num, CLAIM_STATUS_COL, "DONE")
    except Exception as e:
        print(f"⚠ Failed to mark row {row_num} done: {e}")


def update_combined_row(row_index, data):
    """Writes combined row data to columns A-G."""
    sheet = get_sheet()
    cell_range = f"A{row_index}:G{row_index}"
    try:
        sheets_call(sheet.update, cell_range, [data])
    except gspread.exceptions.APIError as e:
        print(f"⚠ Failed to update row {row_index}: {e}")


def update_headline_and_description(row_index, headline, description):
    """Writes Headline and Description directly to columns M-N."""
    sheet = get_sheet()
    cell_range = f"M{row_index}:N{row_index}"
    try:
        sheets_call(sheet.update, cell_range, [[headline, description]])
    except gspread.exceptions.APIError as e:
        print(f"⚠ Failed to update headline/desc for row {row_index}: {e}")


# Add the get_urls_with_retry helper function which was originally called in SCRAPEER.py
def get_urls_with_retry():
    """Helper to fetch column H (transparency URLs) from sheet using one read only."""
    sheet = get_sheet()
    values = sheets_call(sheet.get_all_values)

    # Return full list matching row positions for combined scraper iteration.
    # This replaces the old get_agent_rows_snapshot() + col_values(8) double read.
    return [(row[7].strip() if len(row) >= 8 else "") for row in values[1:]]

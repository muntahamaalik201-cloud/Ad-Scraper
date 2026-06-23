import gspread
from oauth2client.service_account import ServiceAccountCredentials
import config
import time
from datetime import datetime, timedelta
import uuid

# ==========================
# CACHE CONFIG
# ==========================
SHEET_CACHE = None
SHEET_CACHE_TIME = 0
SHEET_CACHE_TTL = 60

SNAPSHOT_CACHE = None
SNAPSHOT_TIME = 0
SNAPSHOT_TTL = 10  # IMPORTANT: reduces API hits massively

# ==========================
# COLUMNS
# ==========================
CLAIM_AGENT_COL = 9
CLAIM_TIME_COL = 10
CLAIM_TOKEN_COL = 11
CLAIM_STATUS_COL = 12
CLAIM_TTL_MINUTES = 5

LOG_CACHE = []
WRITE_LOGS = False


# ==========================
# SHEET AUTH
# ==========================
def get_sheet():
    global SHEET_CACHE, SHEET_CACHE_TIME

    now = time.time()
    if SHEET_CACHE and (now - SHEET_CACHE_TIME) < SHEET_CACHE_TTL:
        return SHEET_CACHE

    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    creds = ServiceAccountCredentials.from_json_keyfile_name(
        config.CREDENTIALS_FILE, scope
    )

    client = gspread.authorize(creds)
    sheet = client.open_by_key(config.SPREADSHEET_ID).worksheet(config.WORKSHEET_NAME)

    SHEET_CACHE = sheet
    SHEET_CACHE_TIME = now
    return sheet
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


# ==========================
# SNAPSHOT (CRITICAL OPTIMIZATION)
# ==========================
def get_agent_rows_snapshot():
    """
    ONE FULL READ ONLY (cached for 10 seconds)
    """
    global SNAPSHOT_CACHE, SNAPSHOT_TIME

    now = time.time()
    if SNAPSHOT_CACHE and (now - SNAPSHOT_TIME) < SNAPSHOT_TTL:
        return SNAPSHOT_CACHE

    sheet = get_sheet()

    for attempt in range(5):
        try:
            values = sheet.get_all_values()
            break
        except gspread.exceptions.APIError as e:
            if "429" in str(e):
                wait = 2 * (attempt + 1)
                print(f"⚠ 429 hit, retrying in {wait}s")
                time.sleep(wait)
            else:
                raise
    else:
        raise Exception("Failed to read sheet after retries")

    rows = []

    for idx in range(1, len(values)):
        row = values[idx]
        row_num = idx + 1

        url = row[7].strip() if len(row) > 7 else ""
        video_id = row[5].strip() if len(row) > 5 else ""

        claim_agent = row[8].strip() if len(row) > 8 else ""
        claim_time = row[9].strip() if len(row) > 9 else ""
        claim_token = row[10].strip() if len(row) > 10 else ""
        claim_status = row[11].strip() if len(row) > 11 else ""
        stop_flag = row[12].strip() if len(row) > 12 else ""

        rows.append({
            "row_num": row_num,
            "url": url,
            "video_id": video_id,
            "claim_agent": claim_agent,
            "claim_time": claim_time,
            "claim_token": claim_token,
            "claim_status": claim_status,
            "stop_flag": stop_flag,
            "processed": bool(video_id.strip()),
            "claim_expired": is_claim_expired(claim_time)
        })

    SNAPSHOT_CACHE = rows
    SNAPSHOT_TIME = now

    return rows


# ==========================
# HELPERS
# ==========================
def is_claim_expired(claim_time_text):
    if not claim_time_text:
        return True
    try:
        t = datetime.strptime(claim_time_text, "%Y-%m-%d %H:%M:%S")
        return datetime.now() - t > timedelta(minutes=CLAIM_TTL_MINUTES)
    except:
        return True


# ==========================
# CORE TASK PICKER (FIXED)
# ==========================
def get_next_agent_task(direction, agent_name, run_id):
    direction = direction.lower().strip()

    if direction not in ["top", "bottom"]:
        raise ValueError("direction must be top or bottom")

    sheet = get_sheet()
    rows = get_agent_rows_snapshot()

    unprocessed = [r for r in rows if r["url"] and not r["processed"]]

    if not unprocessed:
        return None

    # collision protection
    if len(unprocessed) == 1 and direction == "bottom":
        return "COLLISION_STOP"

    candidates = sorted(
        unprocessed,
        key=lambda x: x["row_num"],
        reverse=(direction == "bottom")
    )

    for c in candidates:
        row_num = c["row_num"]

        if c["stop_flag"].upper() == "STOP":
            return "COLLISION_STOP"

        # skip active claims
        if c["claim_agent"] and c["claim_agent"] != agent_name and not c["claim_expired"]:
            continue

        token = f"{agent_name}-{run_id}-{uuid.uuid4().hex[:10]}"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # SINGLE WRITE ONLY (claim row)
        sheet.update(
            f"I{row_num}:L{row_num}",
            [[agent_name, now, token, "CLAIMED"]]
        )

        # ❌ REMOVED: confirm read (major quota fix)
        # We trust write success instead of re-reading sheet

        return row_num, c["url"]

    return None


# ==========================
# SIMPLE STATUS UPDATE
# ==========================
def mark_agent_done(row_num, agent_name=None):
    sheet = get_sheet()
    try:
        sheet.update_cell(row_num, CLAIM_STATUS_COL, "DONE")
    except:
        pass


# ==========================
# BULK UPDATE HELPERS
# ==========================
def update_combined_row(row_index, data):
    sheet = get_sheet()
    try:
        sheet.update(f"A{row_index}:G{row_index}", [data])
    except Exception as e:
        print(f"Update error: {e}")


def update_headline_and_description(row_index, headline, description):
    sheet = get_sheet()
    try:
        sheet.update(f"M{row_index}:N{row_index}", [[headline, description]])
    except Exception as e:
        print(f"Update error: {e}")


# ==========================
# OPTIMIZED URL FETCH (NO EXTRA SNAPSHOT CALL)
# ==========================
def get_urls_with_retry():
    rows = get_agent_rows_snapshot()
    return [r["url"] for r in rows if r["url"]]


# ==========================
# OPTIONAL UTILS
# ==========================
def count_unprocessed_rows():
    rows = get_agent_rows_snapshot()
    return sum(1 for r in rows if r["url"] and not r["processed"])

import random
import re
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import config

# ==========================
# TIMEZONE
# ==========================
PAKISTAN_TZ = ZoneInfo("Asia/Karachi")


def pakistan_now():
    """Return an aware datetime in Pakistan Standard Time (UTC+05:00)."""
    return datetime.now(PAKISTAN_TZ)


# ==========================
# CACHE CONFIG
# ==========================
SHEET_CACHE = None
SHEET_CACHE_TIME = 0
SHEET_CACHE_TTL = 60

SNAPSHOT_CACHE = None
SNAPSHOT_TIME = 0
SNAPSHOT_TTL = 10

# ==========================
# COLUMNS
# ==========================
CLAIM_AGENT_COL = 9
CLAIM_TIME_COL = 10
CLAIM_TOKEN_COL = 11
CLAIM_STATUS_COL = 12
CLAIM_TTL_MINUTES = 15
RETRY_COOLDOWN_MINUTES = 30

LOG_CACHE = []
WRITE_LOGS = False

# Google Sheets/API errors worth retrying. A permanent permission error will
# still fail after the bounded retry count instead of stopping the whole queue.
RETRYABLE_API_STATUS_CODES = {403, 429, 500, 502, 503, 504}
SHEET_API_MAX_ATTEMPTS = 6


# ==========================
# RETRY HELPERS
# ==========================
def _extract_api_status(error):
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status

    match = re.search(r"\b(403|429|500|502|503|504)\b", str(error))
    return int(match.group(1)) if match else None


def _is_retryable_api_error(error):
    status = _extract_api_status(error)
    if status in RETRYABLE_API_STATUS_CODES:
        return True

    text = str(error).lower()
    markers = (
        "rate limit",
        "quota",
        "backend error",
        "service unavailable",
        "temporarily unavailable",
        "internal error",
        "connection reset",
        "timed out",
        "timeout",
    )
    return any(marker in text for marker in markers)


def _sheet_api_call(action_name, operation, attempts=SHEET_API_MAX_ATTEMPTS):
    """Run a gspread operation with bounded exponential backoff."""
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except gspread.exceptions.APIError as error:
            last_error = error
            if not _is_retryable_api_error(error) or attempt >= attempts:
                raise

            status = _extract_api_status(error) or "API"
            wait_seconds = min(60, 2 ** attempt) + random.uniform(0.2, 1.0)
            print(
                f"⚠ Google Sheets {status} during {action_name}; "
                f"retry {attempt}/{attempts} in {wait_seconds:.1f}s"
            )
            time.sleep(wait_seconds)
        except (TimeoutError, ConnectionError, OSError) as error:
            last_error = error
            if attempt >= attempts:
                raise

            wait_seconds = min(60, 2 ** attempt) + random.uniform(0.2, 1.0)
            print(
                f"⚠ Network error during {action_name}; "
                f"retry {attempt}/{attempts} in {wait_seconds:.1f}s: {error}"
            )
            time.sleep(wait_seconds)

    if last_error:
        raise last_error
    raise RuntimeError(f"Failed Google Sheets operation: {action_name}")


def invalidate_snapshot_cache():
    global SNAPSHOT_CACHE, SNAPSHOT_TIME
    SNAPSHOT_CACHE = None
    SNAPSHOT_TIME = 0


# ==========================
# SHEET AUTH
# ==========================
def get_sheet():
    global SHEET_CACHE, SHEET_CACHE_TIME

    now = time.time()
    if SHEET_CACHE is not None and (now - SHEET_CACHE_TIME) < SHEET_CACHE_TTL:
        return SHEET_CACHE

    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]

    creds = ServiceAccountCredentials.from_json_keyfile_name(
        config.CREDENTIALS_FILE,
        scope,
    )
    client = gspread.authorize(creds)

    sheet = _sheet_api_call(
        "open worksheet",
        lambda: client.open_by_key(config.SPREADSHEET_ID).worksheet(
            config.WORKSHEET_NAME
        ),
    )

    SHEET_CACHE = sheet
    SHEET_CACHE_TIME = now
    return sheet


# ==========================
# LOGS DISABLED
# ==========================
def flush_logs():
    global LOG_CACHE
    LOG_CACHE = []


def add_log(
    row_number="",
    status="",
    log_type="",
    url="",
    video_id="",
    app_link="",
    message="",
):
    return


# ==========================
# CLAIM/TIME HELPERS
# ==========================
def _parse_claim_time(claim_time_text):
    if not claim_time_text:
        return None

    try:
        parsed = datetime.strptime(claim_time_text, "%Y-%m-%d %H:%M:%S")
        return parsed.replace(tzinfo=PAKISTAN_TZ)
    except (TypeError, ValueError):
        return None


def is_claim_expired(claim_time_text):
    parsed = _parse_claim_time(claim_time_text)
    if parsed is None:
        return True
    return pakistan_now() - parsed > timedelta(minutes=CLAIM_TTL_MINUTES)


def is_retry_cooldown_active(claim_status, claim_time_text):
    if not str(claim_status or "").upper().startswith("RETRY_"):
        return False

    parsed = _parse_claim_time(claim_time_text)
    if parsed is None:
        return False

    return pakistan_now() - parsed < timedelta(minutes=RETRY_COOLDOWN_MINUTES)


# ==========================
# SNAPSHOT
# ==========================
def get_agent_rows_snapshot(force_refresh=False):
    """One full sheet read, cached briefly to reduce API usage."""
    global SNAPSHOT_CACHE, SNAPSHOT_TIME

    now = time.time()
    if (
        not force_refresh
        and SNAPSHOT_CACHE is not None
        and (now - SNAPSHOT_TIME) < SNAPSHOT_TTL
    ):
        return SNAPSHOT_CACHE

    sheet = get_sheet()
    values = _sheet_api_call("read full worksheet", sheet.get_all_values)

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

        rows.append(
            {
                "row_num": row_num,
                "url": url,
                "video_id": video_id,
                "claim_agent": claim_agent,
                "claim_time": claim_time,
                "claim_token": claim_token,
                "claim_status": claim_status,
                "stop_flag": "",
                "processed": bool(video_id),
                "claim_expired": is_claim_expired(claim_time),
                "retry_cooldown_active": is_retry_cooldown_active(
                    claim_status,
                    claim_time,
                ),
            }
        )

    SNAPSHOT_CACHE = rows
    SNAPSHOT_TIME = now
    return rows


# ==========================
# CORE TASK PICKER
# ==========================
def get_next_agent_task(direction, agent_name, run_id, excluded_rows=None):
    direction = direction.lower().strip()
    if direction not in {"top", "bottom"}:
        raise ValueError("direction must be top or bottom")

    excluded_rows = {int(row_num) for row_num in (excluded_rows or set())}

    sheet = get_sheet()
    rows = get_agent_rows_snapshot()

    unprocessed = [
        row for row in rows
        if row["url"] and not row["processed"] and row["row_num"] not in excluded_rows
    ]
    if not unprocessed:
        return None

    if len(unprocessed) == 1 and direction == "bottom":
        return "COLLISION_STOP"

    candidates = sorted(
        unprocessed,
        key=lambda item: item["row_num"],
        reverse=(direction == "bottom"),
    )

    for candidate in candidates:
        row_num = candidate["row_num"]

        if candidate["stop_flag"].upper() == "STOP":
            return "COLLISION_STOP"

        # A 403/503 row waits briefly while the agents continue with other rows.
        if candidate["retry_cooldown_active"]:
            continue

        # Skip every active claim, including this same agent.
        # This prevents a timed-out bottom/top worker from reclaiming the same
        # bad row forever. Claims become eligible again after CLAIM_TTL_MINUTES.
        if candidate["claim_agent"] and not candidate["claim_expired"]:
            continue

        token = f"{agent_name}-{run_id}-{uuid.uuid4().hex[:10]}"
        now_text = pakistan_now().strftime("%Y-%m-%d %H:%M:%S")

        _sheet_api_call(
            f"claim row {row_num}",
            lambda: sheet.update(
                f"I{row_num}:L{row_num}",
                [[agent_name, now_text, token, "CLAIMED"]],
            ),
        )

        # Prevent this process from selecting its own stale cached row again.
        invalidate_snapshot_cache()
        return row_num, candidate["url"]

    return None


# ==========================
# STATUS UPDATES
# ==========================
def mark_agent_done(row_num, agent_name=None):
    sheet = get_sheet()
    try:
        _sheet_api_call(
            f"mark row {row_num} done",
            lambda: sheet.update_cell(row_num, CLAIM_STATUS_COL, "DONE"),
        )
        invalidate_snapshot_cache()
    except Exception as error:
        print(f"Status update error for row {row_num}: {error}")


def mark_agent_retry(row_num, status="RETRY_NETWORK"):
    """
    Mark a temporary 403/503/network failure without filling column F.

    Because column F stays empty, the row remains eligible for a later retry.
    The active claim is released, J is refreshed in Pakistan time, and
    the picker applies a short cooldown before retrying the row.
    """
    sheet = get_sheet()
    now_text = pakistan_now().strftime("%Y-%m-%d %H:%M:%S")
    safe_status = str(status or "RETRY_NETWORK")[:100]

    try:
        _sheet_api_call(
            f"mark row {row_num} for retry",
            lambda: sheet.update(
                f"I{row_num}:L{row_num}",
                [["", now_text, "", safe_status]],
            ),
        )
        invalidate_snapshot_cache()
    except Exception as error:
        print(f"Retry-status update error for row {row_num}: {error}")


# ==========================
# BULK UPDATE HELPERS
# ==========================
def update_combined_row(row_index, data):
    sheet = get_sheet()
    try:
        _sheet_api_call(
            f"update combined row {row_index}",
            lambda: sheet.update(f"A{row_index}:G{row_index}", [data]),
        )
        invalidate_snapshot_cache()
    except Exception as error:
        print(f"Update error for row {row_index}: {error}")
        raise


def update_headline_and_description(row_index, headline, description):
    sheet = get_sheet()
    try:
        _sheet_api_call(
            f"update text row {row_index}",
            lambda: sheet.update(
                f"M{row_index}:N{row_index}",
                [[headline, description]],
            ),
        )
    except Exception as error:
        print(f"Headline/description update error for row {row_index}: {error}")
        raise


# ==========================
# URL FETCH HELPERS
# ==========================
def get_url_rows_with_retry(unprocessed_only=False):
    """Return real sheet row numbers, avoiding row-number drift when H has gaps."""
    rows = get_agent_rows_snapshot()
    result = []

    for row in rows:
        if not row["url"]:
            continue
        if unprocessed_only and row["processed"]:
            continue
        if unprocessed_only and row["retry_cooldown_active"]:
            continue
        result.append((row["row_num"], row["url"]))

    return result


def get_urls_with_retry():
    """Compatibility helper used by older scraper entry points."""
    return [url for _, url in get_url_rows_with_retry(unprocessed_only=False)]


def count_unprocessed_rows():
    rows = get_agent_rows_snapshot()
    return sum(
        1
        for row in rows
        if row["url"]
        and not row["processed"]
        and not row["retry_cooldown_active"]
    )

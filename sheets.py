"""Google Sheets helpers for the combined Google Ads scraper.

Sheet layout used by the scraper:
    A:G  -> advertiser/package/source URL/app link/time/media/time
    H    -> Google Ads Transparency URL (input)
    I:L  -> claim agent/time/token/status
    M:N  -> headline/description
    O    -> image URL
    P    -> optional STOP flag
"""

from __future__ import annotations

from datetime import datetime, timedelta
import random
import threading
import time
import uuid
from typing import Any, Callable, Optional, TypeVar

import gspread
from oauth2client.service_account import ServiceAccountCredentials

import config


# ==========================
# CACHE CONFIG
# ==========================
SHEET_CACHE = None
SHEET_CACHE_TIME = 0.0
SHEET_CACHE_TTL = 60

SNAPSHOT_CACHE = None
SNAPSHOT_TIME = 0.0
SNAPSHOT_TTL = 10

_CACHE_LOCK = threading.RLock()


# ==========================
# COLUMN CONFIG (1-BASED)
# ==========================
TRANSPARENCY_URL_COL = 8       # H
CLAIM_AGENT_COL = 9            # I
CLAIM_TIME_COL = 10            # J
CLAIM_TOKEN_COL = 11           # K
CLAIM_STATUS_COL = 12          # L
HEADLINE_COL = 13              # M
DESCRIPTION_COL = 14           # N
IMAGE_URL_COL = 15             # O
STOP_FLAG_COL = 16             # P (M:O are scraper output columns)
MEDIA_VALUE_COL = 6            # F: video ID, "image", "text", ERROR, etc.

CLAIM_TTL_MINUTES = 15
VERIFY_CLAIMS = True


# ==========================
# RETRY CONFIG
# ==========================
MAX_API_ATTEMPTS = 5
BASE_RETRY_SECONDS = 2.0
MAX_RETRY_SECONDS = 30.0
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}

T = TypeVar("T")


# ==========================
# LOGS (DISABLED)
# ==========================
LOG_CACHE = []
WRITE_LOGS = False


def flush_logs() -> None:
    """Logs are disabled; clear any in-memory entries."""
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
) -> None:
    """Logs are disabled."""
    return None


# ==========================
# RETRY HELPERS
# ==========================
def _http_status_code(error: Exception) -> Optional[int]:
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)

    if isinstance(status_code, int):
        return status_code

    text = str(error)
    for code in sorted(RETRYABLE_HTTP_CODES):
        if str(code) in text:
            return code

    return None


def _retry_after_seconds(error: Exception) -> Optional[float]:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None

    try:
        value = headers.get("Retry-After") or headers.get("retry-after")
        if value is None:
            return None
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _is_retryable_error(error: Exception) -> bool:
    if isinstance(error, gspread.exceptions.APIError):
        return _http_status_code(error) in RETRYABLE_HTTP_CODES

    # Network/transport errors are normally transient. Avoid importing a specific
    # HTTP stack because gspread versions can use different transports.
    return isinstance(error, (TimeoutError, ConnectionError, OSError))


def _run_with_retry(
    operation: Callable[[], T],
    operation_name: str,
    attempts: int = MAX_API_ATTEMPTS,
) -> T:
    """Run a Sheets operation with exponential backoff, jitter, and Retry-After."""
    last_error: Optional[Exception] = None

    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as error:
            last_error = error

            if not _is_retryable_error(error) or attempt >= attempts:
                raise

            retry_after = _retry_after_seconds(error)
            exponential = min(
                MAX_RETRY_SECONDS,
                BASE_RETRY_SECONDS * (2 ** (attempt - 1)),
            )
            jitter = random.uniform(0.0, 1.0)
            wait_seconds = retry_after if retry_after is not None else exponential + jitter

            code = _http_status_code(error)
            code_text = f" HTTP {code}" if code else ""
            print(
                f"⚠ {operation_name} failed with{code_text}; "
                f"retry {attempt}/{attempts} in {wait_seconds:.1f}s"
            )
            time.sleep(wait_seconds)

    # Defensive fallback; the loop always returns or raises.
    assert last_error is not None
    raise last_error


# ==========================
# CACHE HELPERS
# ==========================
def invalidate_snapshot() -> None:
    global SNAPSHOT_CACHE, SNAPSHOT_TIME
    with _CACHE_LOCK:
        SNAPSHOT_CACHE = None
        SNAPSHOT_TIME = 0.0


def invalidate_sheet_cache() -> None:
    global SHEET_CACHE, SHEET_CACHE_TIME
    with _CACHE_LOCK:
        SHEET_CACHE = None
        SHEET_CACHE_TIME = 0.0


# ==========================
# SHEET AUTH
# ==========================
def _open_sheet():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]

    creds = ServiceAccountCredentials.from_json_keyfile_name(
        config.CREDENTIALS_FILE,
        scope,
    )
    client = gspread.authorize(creds)
    return client.open_by_key(config.SPREADSHEET_ID).worksheet(
        config.WORKSHEET_NAME
    )


def get_sheet():
    global SHEET_CACHE, SHEET_CACHE_TIME

    now = time.time()
    with _CACHE_LOCK:
        if (
            SHEET_CACHE is not None
            and (now - SHEET_CACHE_TIME) < SHEET_CACHE_TTL
        ):
            return SHEET_CACHE

        sheet = _run_with_retry(_open_sheet, "open worksheet")
        SHEET_CACHE = sheet
        SHEET_CACHE_TIME = time.time()
        return sheet


# ==========================
# HELPERS
# ==========================
def _cell(row: list[str], column_number: int) -> str:
    index = column_number - 1
    return row[index].strip() if len(row) > index else ""


def is_claim_expired(claim_time_text: str) -> bool:
    if not claim_time_text:
        return True

    try:
        claimed_at = datetime.strptime(claim_time_text, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return True

    return datetime.now() - claimed_at > timedelta(minutes=CLAIM_TTL_MINUTES)


# ==========================
# SNAPSHOT
# ==========================
def get_agent_rows_snapshot(force_refresh: bool = False):
    """Read the worksheet once and return row-aware records."""
    global SNAPSHOT_CACHE, SNAPSHOT_TIME

    now = time.time()
    with _CACHE_LOCK:
        if (
            not force_refresh
            and SNAPSHOT_CACHE is not None
            and (now - SNAPSHOT_TIME) < SNAPSHOT_TTL
        ):
            return SNAPSHOT_CACHE

    sheet = get_sheet()
    values = _run_with_retry(sheet.get_all_values, "read worksheet snapshot")

    rows = []
    for index, row in enumerate(values[1:], start=2):
        url = _cell(row, TRANSPARENCY_URL_COL)
        media_value = _cell(row, MEDIA_VALUE_COL)
        claim_agent = _cell(row, CLAIM_AGENT_COL)
        claim_time = _cell(row, CLAIM_TIME_COL)
        claim_token = _cell(row, CLAIM_TOKEN_COL)
        claim_status = _cell(row, CLAIM_STATUS_COL)
        stop_flag = _cell(row, STOP_FLAG_COL)

        rows.append(
            {
                "row_num": index,
                "url": url,
                "video_id": media_value,  # compatibility with existing callers
                "media_value": media_value,
                "claim_agent": claim_agent,
                "claim_time": claim_time,
                "claim_token": claim_token,
                "claim_status": claim_status,
                "stop_flag": stop_flag,
                "processed": bool(media_value),
                "claim_expired": is_claim_expired(claim_time),
            }
        )

    with _CACHE_LOCK:
        SNAPSHOT_CACHE = rows
        SNAPSHOT_TIME = time.time()

    return rows


# ==========================
# CLAIM / TASK PICKER
# ==========================
def _write_claim(sheet, row_num: int, values: list[str]) -> None:
    sheet.update(
        range_name=f"I{row_num}:L{row_num}",
        values=[values],
    )


def _verify_claim(sheet, row_num: int, token: str) -> bool:
    values = sheet.get(f"I{row_num}:L{row_num}")
    if not values or not values[0] or len(values[0]) < 4:
        return False

    current = values[0]
    current_token = current[2].strip() if len(current) > 2 else ""
    current_status = current[3].strip().upper() if len(current) > 3 else ""
    return current_token == token and current_status == "CLAIMED"


def get_next_agent_task(direction: str, agent_name: str, run_id: str):
    """Claim the next unprocessed row from the top or bottom."""
    direction = direction.lower().strip()
    if direction not in {"top", "bottom"}:
        raise ValueError("direction must be 'top' or 'bottom'")

    sheet = get_sheet()

    # Claims require a fresh cross-process view. A cached snapshot can assign a row
    # that another process claimed seconds earlier.
    rows = get_agent_rows_snapshot(force_refresh=True)
    unprocessed = [row for row in rows if row["url"] and not row["processed"]]

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

        if (
            candidate["claim_agent"]
            and candidate["claim_agent"] != agent_name
            and not candidate["claim_expired"]
        ):
            continue

        token = f"{agent_name}-{run_id}-{uuid.uuid4().hex[:10]}"
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        claim_values = [agent_name, now_text, token, "CLAIMED"]

        _run_with_retry(
            lambda: _write_claim(sheet, row_num, claim_values),
            f"claim row {row_num}",
        )

        if VERIFY_CLAIMS:
            verified = _run_with_retry(
                lambda: _verify_claim(sheet, row_num, token),
                f"verify claim row {row_num}",
            )
            if not verified:
                print(f"⚠ Claim collision detected on row {row_num}; trying another row")
                invalidate_snapshot()
                continue

        invalidate_snapshot()
        return row_num, candidate["url"]

    return None


def mark_agent_done(row_num: int, agent_name: Optional[str] = None) -> None:
    sheet = get_sheet()
    _run_with_retry(
        lambda: sheet.update_cell(row_num, CLAIM_STATUS_COL, "DONE"),
        f"mark row {row_num} done",
    )
    invalidate_snapshot()


# ==========================
# WRITE HELPERS
# ==========================
def update_scrape_result(
    row_index: int,
    combined_data: list[Any],
    headline: Any,
    description: Any,
    image_url: Any,
) -> None:
    """Write A:G and M:O in one Google Sheets batch request."""
    if len(combined_data) != 7:
        raise ValueError("combined_data must contain exactly 7 values for columns A:G")

    sheet = get_sheet()
    payload = [
        {
            "range": f"A{row_index}:G{row_index}",
            "values": [combined_data],
        },
        {
            "range": f"M{row_index}:O{row_index}",
            "values": [[headline, description, image_url]],
        },
    ]

    def write_batch():
        try:
            return sheet.batch_update(payload, value_input_option="RAW")
        except TypeError:
            # Compatibility with older gspread releases.
            return sheet.batch_update(payload)

    _run_with_retry(write_batch, f"write scraper result row {row_index}")
    invalidate_snapshot()


# Compatibility helpers retained for scripts that still call them directly.
def update_combined_row(row_index: int, data: list[Any]) -> None:
    if len(data) != 7:
        raise ValueError("data must contain exactly 7 values for columns A:G")

    sheet = get_sheet()
    _run_with_retry(
        lambda: sheet.update(
            range_name=f"A{row_index}:G{row_index}",
            values=[data],
        ),
        f"update A:G row {row_index}",
    )
    invalidate_snapshot()


def update_headline_and_description(
    row_index: int,
    headline: Any,
    description: Any,
) -> None:
    sheet = get_sheet()
    _run_with_retry(
        lambda: sheet.update(
            range_name=f"M{row_index}:N{row_index}",
            values=[[headline, description]],
        ),
        f"update M:N row {row_index}",
    )
    invalidate_snapshot()


def update_image_url(row_index: int, image_url: Any) -> None:
    """Write the image URL to column O."""
    sheet = get_sheet()
    _run_with_retry(
        lambda: sheet.update_cell(row_index, IMAGE_URL_COL, image_url),
        f"update image URL row {row_index}",
    )
    invalidate_snapshot()


def update_creative_details(
    row_index: int,
    headline: Any,
    description: Any,
    image_url: Any,
) -> None:
    """Write headline, description and image URL to M:O in one request."""
    sheet = get_sheet()
    _run_with_retry(
        lambda: sheet.update(
            range_name=f"M{row_index}:O{row_index}",
            values=[[headline, description, image_url]],
        ),
        f"update M:O row {row_index}",
    )
    invalidate_snapshot()


# ==========================
# URL READ HELPERS
# ==========================
def get_url_rows_with_retry(only_unprocessed: bool = False):
    """Return exact ``(sheet_row_number, URL)`` pairs without row shifting."""
    rows = get_agent_rows_snapshot()
    return [
        (row["row_num"], row["url"])
        for row in rows
        if row["url"] and (not only_unprocessed or not row["processed"])
    ]


def get_urls_with_retry():
    """Compatibility API that preserves blank rows so enumerate(..., start=2) stays aligned."""
    rows = get_agent_rows_snapshot()
    return [row["url"] for row in rows]


def count_unprocessed_rows() -> int:
    rows = get_agent_rows_snapshot()
    return sum(1 for row in rows if row["url"] and not row["processed"])

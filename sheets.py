import gspread
from oauth2client.service_account import ServiceAccountCredentials
import config
import time
from datetime import datetime, timedelta
import uuid

SHEET_CACHE = None
SHEET_CACHE_TIME = None
SHEET_CACHE_TTL = 60

CLAIM_AGENT_COL = 9
CLAIM_TIME_COL = 10
CLAIM_TOKEN_COL = 11
CLAIM_STATUS_COL = 12
HEADLINE_COL = 13
DESCRIPTION_COL = 14
IMAGE_URL_COL = 15
CLAIM_TTL_MINUTES = 5

LOG_BATCH_SIZE = 5
LOG_CACHE = []


def col_to_letter(col_num):
    result = ""
    while col_num:
        col_num, remainder = divmod(col_num - 1, 26)
        result = chr(65 + remainder) + result
    return result


def get_sheet():
    global SHEET_CACHE, SHEET_CACHE_TIME
    now = time.time()

    if SHEET_CACHE and SHEET_CACHE_TIME and now - SHEET_CACHE_TIME < SHEET_CACHE_TTL:
        return SHEET_CACHE

    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    creds = ServiceAccountCredentials.from_json_keyfile_name(
        config.CREDENTIALS_FILE,
        scope
    )

    client = gspread.authorize(creds)
    sheet = client.open_by_key(config.SPREADSHEET_ID).worksheet(
        config.WORKSHEET_NAME
    )

    SHEET_CACHE = sheet
    SHEET_CACHE_TIME = now

    return sheet


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
            updates.append({
                "range": f"{col_to_letter(col)}1",
                "values": [[name]]
            })

    if updates:
        sheet.batch_update(updates)


def update_combined_row(row_index, data):
    """
    Writes columns A:G.
    Added debug + immediate verification.
    """

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

        # Verify written values immediately
        verify = sheet.row_values(row_index)

        print(
            "SHEET AFTER WRITE A:G:",
            verify[:7]
        )

    except Exception as e:
        print(
            f"⚠ Failed to update row {row_index}: {e}"
        )


def update_headline_and_description(row_index, headline, description):
    sheet = get_sheet()

    try:
        sheet.update(
            f"M{row_index}:N{row_index}",
            [[headline or "N/A", description or "N/A"]],
            value_input_option="USER_ENTERED"
        )

    except Exception as e:
        print(
            f"⚠ Failed headline update row {row_index}: {e}"
        )


def update_image_url(row_index, image_url):
    sheet = get_sheet()

    try:
        sheet.update(
            f"O{row_index}",
            [[image_url or "N/A"]],
            value_input_option="USER_ENTERED"
        )

    except Exception as e:
        print(
            f"⚠ Failed image URL update row {row_index}: {e}"
        )

import sys
import time
import uuid
from datetime import datetime

import sheets
from scraper import scrape_single_url

MAX_RUNTIME_SECONDS = (5 * 60 * 60) + (50 * 60)  # 5h50m


def now_text():
    return datetime.now().strftime("%I:%M:%S %p")


def run_agent(direction):
    direction = direction.lower().strip()
    if direction not in ["top", "bottom"]:
        raise ValueError("Use: python agent_runner.py top OR python agent_runner.py bottom")

    agent_name = f"AGENT_{direction.upper()}"
    run_id = uuid.uuid4().hex[:8]

    start_time = time.time()
    deadline = start_time + MAX_RUNTIME_SECONDS
    processed_count = 0

    print(f"🚀 {agent_name} started at {now_text()} with run_id={run_id}")
    sheets.add_log(row_number="", status="AGENT_STARTED", log_type=agent_name,
                   message=f"{agent_name} started with run_id={run_id}")

    # Fetch sheet snapshot once
    while time.time() < deadline:
        remaining_seconds = int(deadline - time.time())
        if remaining_seconds <= 0:
            break

        task = sheets.get_next_agent_task(direction=direction, agent_name=agent_name, run_id=run_id)

        if task is None:
            print(f"✅ {agent_name}: no unprocessed rows left.")
            sheets.add_log(row_number="", status="NO_ROWS_LEFT", log_type=agent_name,
                           message="No unprocessed rows left")
            sheets.flush_logs()
            break

        if task == "COLLISION_STOP":
            print(f"🛑 {agent_name}: stopped to avoid collision or Stop Flag")
            sheets.flush_logs()
            break

        row_num, url = task
        print(f"🔒 {agent_name}: claimed row {row_num}")

        sheets.add_log(row_number=row_num, status="ROW_CLAIMED", log_type=agent_name,
                       url=url, message=f"{agent_name} claimed row {row_num}")

        try:
            # Scrape row
            scrape_single_url((row_num, url))

            sheets.mark_agent_done(row_num, agent_name)
            processed_count += 1

            print(f"✅ {agent_name}: finished row {row_num}")
        except Exception as e:
            print(f"❌ {agent_name}: error row {row_num}: {e}")
            sheets.add_log(row_number=row_num, status="AGENT_ROW_ERROR",
                           log_type=agent_name, url=url, message=str(e))

        time.sleep(2)  # pause to avoid hitting Sheets API too fast

    sheets.add_log(row_number="", status="AGENT_STOPPED", log_type=agent_name,
                   message=f"{agent_name} stopped. Processed rows: {processed_count}")
    sheets.flush_logs()
    print(f"🛑 {agent_name} stopped at {now_text()}. Processed rows: {processed_count}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python agent_runner.py top")
        print("Usage: python agent_runner.py bottom")
        sys.exit(1)
    run_agent(sys.argv[1])

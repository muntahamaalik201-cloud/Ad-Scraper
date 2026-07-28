import faulthandler
import os
import sys
import time
import uuid
from datetime import datetime

import sheets
from scraper import scrape_single_url


MAX_RUNTIME_SECONDS = (5 * 60 * 60) + (50 * 60)  # 5h50m
IDLE_RETRY_SECONDS = 5
TASK_ERROR_RETRY_SECONDS = 10
TRACEBACK_AFTER_SECONDS = 180


def configure_live_output() -> None:
    """Force immediate output in GitHub Actions and other non-interactive runners."""
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

    try:
        sys.stdout.reconfigure(line_buffering=True, write_through=True)
    except (AttributeError, ValueError):
        pass

    try:
        sys.stderr.reconfigure(line_buffering=True, write_through=True)
    except (AttributeError, ValueError):
        pass

    faulthandler.enable()

    # When a call blocks for 3 minutes, print the Python stack automatically.
    # This makes it clear whether the agent is waiting in Sheets, Playwright,
    # networking, or another function.
    try:
        faulthandler.dump_traceback_later(
            TRACEBACK_AFTER_SECONDS,
            repeat=True,
        )
    except (AttributeError, RuntimeError):
        pass


def now_text() -> str:
    return datetime.now().strftime("%I:%M:%S %p")


def log(message: str) -> None:
    print(message, flush=True)


def run_agent(direction: str) -> None:
    direction = direction.lower().strip()

    if direction not in {"top", "bottom"}:
        raise ValueError(
            "Use: python -u agent_runner.py top "
            "OR python -u agent_runner.py bottom"
        )

    agent_name = f"AGENT_{direction.upper()}"
    run_id = uuid.uuid4().hex[:8]

    start_time = time.monotonic()
    deadline = start_time + MAX_RUNTIME_SECONDS
    processed_count = 0

    log(
        f"🚀 {agent_name} started at {now_text()} "
        f"with run_id={run_id}, pid={os.getpid()}"
    )

    sheets.add_log(
        row_number="",
        status="AGENT_STARTED",
        log_type=agent_name,
        message=f"{agent_name} started with run_id={run_id}",
    )

    while time.monotonic() < deadline:
        remaining_seconds = int(deadline - time.monotonic())
        if remaining_seconds <= 0:
            break

        log(
            f"📄 {agent_name}: requesting next {direction} task "
            f"({remaining_seconds // 60} minutes remaining)"
        )

        task_started = time.monotonic()

        try:
            task = sheets.get_next_agent_task(
                direction=direction,
                agent_name=agent_name,
                run_id=run_id,
            )
        except Exception as error:
            elapsed = time.monotonic() - task_started
            log(
                f"❌ {agent_name}: task selection failed after "
                f"{elapsed:.1f}s: {type(error).__name__}: {error}"
            )

            sheets.add_log(
                row_number="",
                status="TASK_SELECTION_ERROR",
                log_type=agent_name,
                message=str(error),
            )

            time.sleep(TASK_ERROR_RETRY_SECONDS)
            continue

        task_elapsed = time.monotonic() - task_started
        log(
            f"📋 {agent_name}: task lookup completed in "
            f"{task_elapsed:.1f}s; result={task!r}"
        )

        if task is None:
            log(f"✅ {agent_name}: no unprocessed rows left.")

            sheets.add_log(
                row_number="",
                status="NO_ROWS_LEFT",
                log_type=agent_name,
                message="No unprocessed rows left",
            )
            sheets.flush_logs()
            break

        if task == "COLLISION_STOP":
            log(
                f"🛑 {agent_name}: one unprocessed row remains; "
                "bottom agent stopped to avoid claiming the same final row "
                "as the top agent."
            )
            sheets.flush_logs()
            break

        row_num, url = task
        log(f"🔒 {agent_name}: claimed row {row_num}")
        log(f"🌐 {agent_name}: starting scraper for row {row_num}")

        sheets.add_log(
            row_number=row_num,
            status="ROW_CLAIMED",
            log_type=agent_name,
            url=url,
            message=f"{agent_name} claimed row {row_num}",
        )

        scrape_started = time.monotonic()

        try:
            scrape_single_url((row_num, url))

            scrape_elapsed = time.monotonic() - scrape_started
            log(
                f"💾 {agent_name}: scraper returned for row {row_num} "
                f"after {scrape_elapsed:.1f}s"
            )

            sheets.mark_agent_done(row_num, agent_name)
            processed_count += 1

            log(
                f"✅ {agent_name}: finished row {row_num}; "
                f"total processed={processed_count}"
            )

        except Exception as error:
            scrape_elapsed = time.monotonic() - scrape_started
            log(
                f"❌ {agent_name}: error on row {row_num} after "
                f"{scrape_elapsed:.1f}s: {type(error).__name__}: {error}"
            )

            sheets.add_log(
                row_number=row_num,
                status="AGENT_ROW_ERROR",
                log_type=agent_name,
                url=url,
                message=str(error),
            )

        # Small pause to reduce Sheets API contention between agents.
        time.sleep(2)

    sheets.add_log(
        row_number="",
        status="AGENT_STOPPED",
        log_type=agent_name,
        message=(
            f"{agent_name} stopped. "
            f"Processed rows: {processed_count}"
        ),
    )
    sheets.flush_logs()

    total_elapsed = time.monotonic() - start_time
    log(
        f"🛑 {agent_name} stopped at {now_text()}. "
        f"Processed rows: {processed_count}. "
        f"Runtime: {total_elapsed / 60:.1f} minutes"
    )


if __name__ == "__main__":
    configure_live_output()

    if len(sys.argv) < 2:
        log("Usage: python -u agent_runner.py top")
        log("Usage: python -u agent_runner.py bottom")
        sys.exit(1)

    run_agent(sys.argv[1])

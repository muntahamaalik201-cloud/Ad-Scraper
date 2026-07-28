import multiprocessing as mp
import os
import queue
import sys
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import sheets
from scraper import scrape_single_url

PAKISTAN_TZ = ZoneInfo("Asia/Karachi")
MAX_RUNTIME_SECONDS = (5 * 60 * 60) + (50 * 60)  # 5h 50m
ROW_TIMEOUT_SECONDS = 150
BETWEEN_ROWS_SECONDS = 2


def now_text():
    return datetime.now(PAKISTAN_TZ).strftime("%I:%M:%S %p")


def _scrape_child(row_num, url, result_queue):
    """Run one Playwright scrape in an isolated child process."""
    try:
        result = scrape_single_url((row_num, url))
        normalized = str(result or "DONE").upper()
        if normalized not in {"DONE", "RETRY"}:
            normalized = "DONE"
        result_queue.put(("OK", normalized))
    except BaseException as error:
        try:
            result_queue.put(("ERROR", f"{type(error).__name__}: {error}"))
        except Exception:
            pass
        raise


def scrape_with_timeout(row_num, url, timeout_seconds=ROW_TIMEOUT_SECONDS):
    """
    Isolate Chromium/Playwright per row.

    A browser crash or native segfault can only kill the child process; the
    top/bottom agent remains alive, defers the row, and moves to the next one.
    """
    ctx = mp.get_context("spawn")
    result_queue = ctx.Queue(maxsize=1)
    process = ctx.Process(
        target=_scrape_child,
        args=(row_num, url, result_queue),
        daemon=False,
    )

    process.start()
    process.join(timeout_seconds)

    if process.is_alive():
        process.terminate()
        process.join(10)
        if process.is_alive():
            process.kill()
            process.join(5)
        return "RETRY", f"TIMEOUT after {timeout_seconds}s"

    try:
        status, message = result_queue.get_nowait()
    except queue.Empty:
        status, message = None, None
    finally:
        try:
            result_queue.close()
            result_queue.join_thread()
        except Exception:
            pass

    if status == "OK":
        return message, ""

    if status == "ERROR":
        return "RETRY", message

    if process.exitcode == 0:
        # Compatibility fallback for an older scraper that returned no value.
        return "DONE", ""

    return "RETRY", f"Child process exited with code {process.exitcode}"


def run_agent(direction):
    direction = str(direction or "").lower().strip()
    if direction not in {"top", "bottom"}:
        raise ValueError("Use: python agent_runner.py top OR python agent_runner.py bottom")

    agent_name = f"AGENT_{direction.upper()}"
    run_id = uuid.uuid4().hex[:8]
    started_at = time.time()
    deadline = started_at + MAX_RUNTIME_SECONDS

    completed_count = 0
    retry_count = 0
    attempted_rows = set()

    print(
        f"🚀 {agent_name} started at {now_text()} "
        f"with run_id={run_id}, pid={os.getpid()}",
        flush=True,
    )

    while time.time() < deadline:
        remaining_minutes = max(0, int((deadline - time.time()) / 60))
        print(
            f"📄 {agent_name}: requesting next {direction} task "
            f"({remaining_minutes} minutes remaining)",
            flush=True,
        )

        lookup_started = time.time()
        try:
            task = sheets.get_next_agent_task(
                direction=direction,
                agent_name=agent_name,
                run_id=run_id,
                excluded_rows=attempted_rows,
            )
        except Exception as error:
            print(f"⚠️ {agent_name}: task lookup error: {error}", flush=True)
            time.sleep(10)
            continue

        print(
            f"📋 {agent_name}: task lookup completed in "
            f"{time.time() - lookup_started:.1f}s; result={task}",
            flush=True,
        )

        if task is None:
            print(
                f"✅ {agent_name}: no eligible rows remain in this run. "
                "Deferred rows will be retried in a later run.",
                flush=True,
            )
            break

        if task == "COLLISION_STOP":
            print(f"🛑 {agent_name}: collision stop reached", flush=True)
            break

        row_num, url = task
        attempted_rows.add(int(row_num))

        print(f"🔒 {agent_name}: claimed row {row_num}", flush=True)
        print(f"🌐 {agent_name}: starting scraper for row {row_num}", flush=True)

        row_started = time.time()
        result, detail = scrape_with_timeout(row_num, url)
        elapsed = time.time() - row_started

        print(
            f"💾 {agent_name}: scraper returned {result} for row {row_num} "
            f"after {elapsed:.1f}s",
            flush=True,
        )

        if result == "DONE":
            sheets.mark_agent_done(row_num, agent_name)
            completed_count += 1
            print(
                f"✅ {agent_name}: finished row {row_num}; "
                f"total completed={completed_count}",
                flush=True,
            )
        else:
            retry_count += 1
            # The scraper normally sets a specific RETRY_* status. This parent
            # fallback handles timeouts, child crashes, and native segfaults.
            if detail:
                status = "RETRY_TIMEOUT" if "TIMEOUT" in detail.upper() else "RETRY_CHILD_CRASH"
                sheets.mark_agent_retry(row_num, status)
            print(
                f"⏭ {agent_name}: deferred row {row_num}; "
                f"it will not be selected again in this run. "
                f"Reason: {detail or 'retry requested by scraper'}",
                flush=True,
            )

        time.sleep(BETWEEN_ROWS_SECONDS)

    print(
        f"🛑 {agent_name} stopped at {now_text()}. "
        f"Completed={completed_count}, Deferred={retry_count}, "
        f"Attempted={len(attempted_rows)}",
        flush=True,
    )


if __name__ == "__main__":
    mp.freeze_support()

    if len(sys.argv) < 2:
        print("Usage: python agent_runner.py top", flush=True)
        print("Usage: python agent_runner.py bottom", flush=True)
        raise SystemExit(1)

    run_agent(sys.argv[1])

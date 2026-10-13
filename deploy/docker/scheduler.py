"""Background jobs for the Docker setup (the "scheduler" container).

Replaces the cron / PythonAnywhere scheduled tasks:

* every  5 minutes: run_promotions   (start/stop timed sales, ad stats, email campaign batches)
* every 15 minutes: release_unpaid_orders   (free stock held by unpaid card/PayPal orders)
* once a day at DAILY_TASKS_HOUR (UTC, default 3): daily_tasks
  (order automation, reminders, payouts, badges, currency rates, backups...)

Each job runs in its own process, so one failing job never stops the others,
and the next run happens on schedule anyway.
"""
import datetime as dt
import os
import signal
import subprocess
import sys
import time

DAILY_HOUR = int(os.environ.get("DAILY_TASKS_HOUR", "3"))
JOBS = [  # (command, every N minutes)
    ("run_promotions", 5),
    ("release_unpaid_orders", 15),
]
running = True


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)


def log(msg):
    print(f"[scheduler {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


def run(command):
    log(f"start {command}")
    started = time.monotonic()
    result = subprocess.run([sys.executable, "manage.py", command])
    status = "ok" if result.returncode == 0 else f"FAILED (exit {result.returncode})"
    log(f"{command} {status} in {time.monotonic() - started:.0f}s")


def main():
    log(f"started: {', '.join(f'{c} every {m} min' for c, m in JOBS)}, daily_tasks at {DAILY_HOUR:02d}:00 UTC")
    last = {c: 0.0 for c, _ in JOBS}
    last_daily = None
    while running:
        now = time.time()
        for command, minutes in JOBS:
            if now - last[command] >= minutes * 60:
                last[command] = now
                run(command)
        today = dt.datetime.now(dt.timezone.utc)
        if today.hour == DAILY_HOUR and last_daily != today.date():
            last_daily = today.date()
            run("daily_tasks")
        for _ in range(30):  # check again in 30 s, but stop quickly on SIGTERM
            if not running:
                break
            time.sleep(1)
    log("stopped")


if __name__ == "__main__":
    main()

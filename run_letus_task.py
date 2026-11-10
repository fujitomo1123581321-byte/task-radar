# -*- coding: utf-8 -*-
import os
import sys
import time
import subprocess
from datetime import datetime

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "task_runner.log")

def log(msg):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{now_str}] {msg}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass

def main():
    log(f"LETUS Task Runner started with args: {sys.argv[1:]}")

    workspace_dir = r"c:\Users\fujit\Box\01学業2026年度\20"
    script_path = os.path.join(workspace_dir, "scan_letus_status.py")

    # Wait up to 60 seconds for Box Drive to mount if needed
    found = False
    for attempt in range(30):
        if os.path.exists(script_path):
            found = True
            break
        log(f"Waiting for Box Drive mount... (attempt {attempt + 1}/30)")
        time.sleep(2)

    if not found:
        log("Box Drive workspace not found after 60s. Skipping scan silently.")
        return 0

    mode_args = []
    if "--morning" in sys.argv:
        mode_args = ["--headless", "--morning-check-only"]
    elif "--evening" in sys.argv:
        mode_args = ["--headless"]
    else:
        mode_args = sys.argv[1:]

    # Resolve python interpreter
    python_exe = sys.executable
    cmd = [python_exe, script_path] + mode_args
    log(f"Launching script: {' '.join(cmd)}")

    try:
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW

        res = subprocess.run(
            cmd,
            cwd=workspace_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
            timeout=300
        )
        log(f"Finished with exit code {res.returncode}")
        if res.returncode != 0 and res.stderr:
            log(f"Stderr: {res.stderr.strip()[:500]}")
    except Exception as e:
        log(f"Execution error: {e}")

    return 0

if __name__ == "__main__":
    sys.exit(main())

"""
Health Checker Node Watchdog
Runs as a persistent background process. Checks every 30 seconds if
node_agent.py is alive; if not, restarts it immediately.
Uses psutil for reliable process detection (wmic was timing out).
"""
import subprocess
import time
import sys
import os
import logging
from datetime import datetime

NODE_DIR = os.path.dirname(os.path.abspath(__file__))
# Allow override via env, default to bundled Python path on Windows
PYTHON = os.environ.get("NODE_PYTHON", sys.executable)
NODE_SCRIPT = os.path.join(NODE_DIR, "node_agent.py")
SERVER = os.environ.get("NODE_SERVER", "http://47.113.216.237:8700")
NODE_NAME = os.environ.get("NODE_NAME", "家里电脑")
LOG_FILE = os.path.join(NODE_DIR, "watchdog.log")
PID_FILE = os.path.join(NODE_DIR, "node.pid")
CHECK_INTERVAL = 30  # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watchdog] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.info


def find_node_pid():
    """Find running node_agent.py PID using psutil. Returns PID or None."""
    try:
        import psutil
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                info = p.info
                if not info["name"] or "python" not in info["name"].lower():
                    continue
                cmd = " ".join(info.get("cmdline") or [])
                if "node_agent.py" in cmd:
                    return info["pid"]
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except ImportError:
        pass
    return None


def is_pid_alive(pid):
    """Check if a PID is still running."""
    if pid is None:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        pass
    return False


def read_pid_file():
    """Read tracked PID from file."""
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE, "r") as f:
                return int(f.read().strip())
    except:
        pass
    return None


def write_pid_file(pid):
    """Write PID to tracking file."""
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(pid))
    except Exception as e:
        log(f"Failed to write PID file: {e}")


def cleanup_stale_nodes(keep_pid=None):
    """Kill all node_agent.py processes except keep_pid. Returns count killed."""
    killed = 0
    try:
        import psutil
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                info = p.info
                if not info["name"] or "python" not in info["name"].lower():
                    continue
                cmd = " ".join(info.get("cmdline") or [])
                if "node_agent.py" in cmd and info["pid"] != keep_pid:
                    p.terminate()
                    killed += 1
                    log(f"Cleaned up stale node PID {info['pid']}")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception as e:
        log(f"Cleanup error: {e}")
    return killed


def start_node():
    """Start node_agent.py as a detached process. Returns PID or None."""
    try:
        env = os.environ.copy()
        # Ensure local app data is set for Playwright/Chromium detection
        if sys.platform == "win32":
            localappdata = os.environ.get("LOCALAPPDATA") or os.path.join(
                os.path.expanduser("~"), "AppData", "Local"
            )
            env["LOCALAPPDATA"] = localappdata
            userprofile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
            env["USERPROFILE"] = userprofile
            env.setdefault(
                "PLAYWRIGHT_BROWSERS_PATH",
                os.path.join(localappdata, "ms-playwright"),
            )
        creationflags = 0
        if sys.platform == "win32":
            creationflags = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        proc = subprocess.Popen(
            [PYTHON, NODE_SCRIPT, "--server", SERVER, "--name", NODE_NAME],
            cwd=NODE_DIR,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )
        pid = proc.pid
        write_pid_file(pid)
        log(f"node_agent.py started (PID {pid})")
        return pid
    except Exception as e:
        log(f"start failed: {e}")
        return None


def main():
    log("=" * 50)
    log("Watchdog started (psutil mode)")
    log(f"Node dir: {NODE_DIR}")
    log(f"Python: {PYTHON}")
    log(f"Server: {SERVER}")
    log(f"Check interval: {CHECK_INTERVAL}s")

    # On startup: find any running node, clean up duplicates
    tracked_pid = read_pid_file()
    running_pid = find_node_pid()

    if running_pid:
        # Node is running, clean up any duplicates and track it
        killed = cleanup_stale_nodes(keep_pid=running_pid)
        if killed > 0:
            log(f"Cleaned up {killed} duplicate node(s)")
        write_pid_file(running_pid)
        tracked_pid = running_pid
        log(f"Node already running (PID {running_pid})")
    else:
        log("Node not running, starting...")
        tracked_pid = start_node()
        time.sleep(5)
        # Verify it started
        if not is_pid_alive(tracked_pid):
            log("Node may have failed to start, will retry on next check")

    while True:
        try:
            time.sleep(CHECK_INTERVAL)

            # Check tracked PID first
            if tracked_pid and is_pid_alive(tracked_pid):
                continue  # Still running

            # Tracked PID dead, double-check with process scan
            running_pid = find_node_pid()
            if running_pid:
                # Found another instance (shouldn't happen, but recover)
                killed = cleanup_stale_nodes(keep_pid=running_pid)
                if killed > 0:
                    log(f"Cleaned up {killed} duplicate(s)")
                write_pid_file(running_pid)
                tracked_pid = running_pid
                log(f"Recovered node (PID {running_pid})")
            else:
                # Truly down, restart
                log("Node is DOWN! Restarting...")
                tracked_pid = start_node()
                time.sleep(5)

        except KeyboardInterrupt:
            log("Watchdog stopped by user")
            break
        except Exception as e:
            log(f"loop error: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()

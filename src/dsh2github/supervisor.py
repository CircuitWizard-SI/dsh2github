"""Restart this one listener after unexpected exits, without installing a service."""
import json
import os
import subprocess
import sys
import time
from .storage import singleton, write_if_changed


def supervise(settings):
    root = settings.runtime_dir
    root.mkdir(parents=True, exist_ok=True)
    record = root / "supervisor-process.json"
    with singleton(root / "supervisor.lock"):
        while True:
            with (root / "watch.stdout.log").open("ab") as out, (root / "watch.stderr.log").open("ab") as err:
                child = subprocess.Popen([sys.executable, "-m", "dsh2github", "--config", str(settings.path), "watch"],
                    cwd=settings.path.parent, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                write_if_changed(record, json.dumps({"pid": os.getpid(), "child_pid": child.pid,
                    "config": str(settings.path), "time": time.time()}))
                code = child.wait()
            write_if_changed(record, json.dumps({"pid": os.getpid(), "child_pid": None,
                "config": str(settings.path), "time": time.time(), "last_exit": code}))
            time.sleep(5)

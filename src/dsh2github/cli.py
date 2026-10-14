from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from . import __version__
    parser = argparse.ArgumentParser(prog="dsh2github")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    parser.add_argument("--config", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Write local configuration")
    init.add_argument("--repo", required=True)
    init.add_argument("--workspace", default="workspace")
    init.add_argument("--force", action="store_true")
    install = commands.add_parser("install-host", help="Add the local Host plugin to an existing DSH profile")
    install.add_argument("--profile", default="desktop")
    commands.add_parser("doctor", help="Check GitHub and the running DSH Host")
    commands.add_parser("setup", help="Create a private conversation entry Issue")
    commands.add_parser("tick", help="Process one poll")
    watch = commands.add_parser("watch", help="Run the listener")
    watch.add_argument("--no-publish", action="store_true", help="Keep history local; task replies still return to Issues")
    sync = commands.add_parser("sync", help="Export visible session histories")
    sync.add_argument("--publish", action="store_true")
    room = commands.add_parser("open", help="Create a room bound to an original DSH session")
    room.add_argument("selector")
    notices = commands.add_parser("notifications", help="Manage append-only completion mentions")
    notices.add_argument("action", choices=["enable", "disable", "test"])
    notices.add_argument("--issue", type=int)
    notices.add_argument("--comment", type=int)
    notices.add_argument("--update", action="store_true")
    args = parser.parse_args(argv)
    if args.config:
        os.environ["DSH2GITHUB_CONFIG"] = str(args.config.resolve())
    from .config import config_path, load_settings
    from .storage import write_if_changed, singleton
    path = config_path()
    try:
        if args.command == "init":
            import re
            if path.exists() and not args.force:
                raise ValueError("Config exists; use another --config or --force")
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo) or args.repo.startswith("OWNER/"):
                raise ValueError("Use OWNER/PRIVATE_REPO")
            write_if_changed(path, json.dumps({"repo": args.repo, "workspace": args.workspace, "runtime_dir": ".runtime",
                "poll_seconds": 15, "sync_seconds": 30}, ensure_ascii=False, indent=2) + "\n")
            print("Configuration written: " + str(path))
            return 0
        settings = load_settings(path)
        settings.require_configured()
        if args.command == "install-host":
            from .host import install_host
            print(json.dumps(install_host(settings, args.profile), ensure_ascii=False))
            return 0
        from .router import Router, Heartbeat
        router = Router(settings)
        if args.command == "doctor":
            checks = {"gh": bool(shutil.which("gh")), "dsh": bool(shutil.which("dsh") or shutil.which("dsh.cmd"))}
            try:
                router.github.validate(); checks["private_owner_repository"] = True
            except (ValueError, RuntimeError):
                checks["private_owner_repository"] = False
            try:
                health = router.host.call("health")
                checks["dsh_host"] = health.get("protocol") == 1
                checks["session_local_models"] = health.get("sessionModelSelection") is True
            except RuntimeError:
                checks["dsh_host"] = False
            print(json.dumps({"ok": all(checks.values()), "checks": checks}, ensure_ascii=False, indent=2))
            return 0 if all(checks.values()) else 1
        if args.command == "notifications":
            from .notifications import enable, disable, test_notice
            if args.update and args.action != "enable":
                raise ValueError("--update is only for notifications enable")
            if args.action == "test":
                if not args.issue or not args.comment:
                    raise ValueError("notifications test needs --issue and --comment")
                result = test_notice(settings, args.issue, args.comment)
            else:
                result = enable(settings, update=args.update) if args.action == "enable" else disable(settings)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        with singleton(settings.runtime_dir / "listener.lock"):
            if args.command == "setup":
                number = router.setup()
                print(f"https://github.com/{settings.repo}/issues/{number}")
            elif args.command == "open":
                router.github.validate(); router.refresh()
                sid = router.select_session(args.selector)
                existing = next((n for n, info in router.state["issues"].items() if info.get("sid") == sid), None)
                if existing:
                    print(f"https://github.com/{settings.repo}/issues/{existing}")
                else:
                    issue = router.github.create("对话：" + router.records[sid].get("title", sid)[:100], f"<!-- dsh2github:bind {sid} -->\n直接留言继续本机原会话。")
                    router.ensure_issue(issue)
                    print(issue["html_url"])
            elif args.command == "sync":
                from .session_sync import sync as sync_histories
                print(json.dumps(sync_histories(router, args.publish), ensure_ascii=False))
            elif args.command == "tick":
                router.tick()
                print(json.dumps({"ok": True, "jobs": len(router.state["jobs"])}))
            elif args.command == "watch":
                from .session_sync import sync as sync_histories
                router.github.validate(); router.refresh()
                heartbeat = Heartbeat(router)
                heartbeat.start()
                next_sync = 0
                try:
                    while True:
                        try:
                            router.tick()
                            if time.monotonic() >= next_sync:
                                result = sync_histories(router, not args.no_publish)
                                next_sync = time.monotonic() + settings.sync_seconds
                            write_if_changed(settings.runtime_dir / "watch-status.json", json.dumps({"ok": True, "time": time.time(), "jobs": len(router.state["jobs"])}))
                        except (OSError, ValueError, RuntimeError) as error:
                            from .text import redact
                            write_if_changed(settings.runtime_dir / "watch-status.json", json.dumps({"ok": False, "time": time.time(), "error": redact(str(error))}))
                            print("dsh2github: " + redact(str(error)), file=sys.stderr, flush=True)
                        time.sleep(settings.poll_seconds)
                finally:
                    heartbeat.close()
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        from .text import redact
        print("dsh2github: " + redact(str(error)), file=sys.stderr)
        return 1

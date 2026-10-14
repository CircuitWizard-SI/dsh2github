"""Export only visible messages to a validated private task repository."""
from datetime import datetime, timezone
import hashlib
import json
from urllib.parse import urlencode
from .github import api
from .storage import write_if_changed
from .text import label, redact, chunks


def sync(router, publish=False):
    router.github.validate()
    router.refresh()
    root = router.settings.runtime_dir / "repository"
    files = {"README.md": "# DeepSeek Harness 本机会话\n\n"}
    if router.state.get("hub"):
        files["README.md"] += f"[打开会话入口](https://github.com/{router.settings.repo}/issues/{router.state['hub']})\n\n"
    index = "| 会话 | 分类 | 更新时间 |\n|---|---|---|\n"
    cache_path = router.settings.runtime_dir / "export-state.json"
    from .storage import read_json
    cache = read_json(cache_path, {})
    next_cache = {}
    for sid, row in router.records.items():
        # Session IDs are opaque identifiers, never usable filesystem paths.
        folder = hashlib.sha256(sid.encode()).hexdigest()[:32]
        key = json.dumps([row.get("updatedAt"), row.get("running"), row.get("title")], sort_keys=True)
        if cache.get(sid, {}).get("key") == key:
            pages = cache[sid]["pages"]
        else:
            snapshot = router.host.call("session", {"sessionId": sid})
            title = row.get("title") or sid
            content = f"# {label(title)}\n\n会话 ID：`{sid}`\n\n"
            if row.get("origin") != "subagent":
                query = urlencode({"title": "对话：" + title[:100], "labels": "dsh-session",
                    "body": f"<!-- dsh2github:bind {sid} -->\n继续原 DSH 会话。"})
                content += f"[在 GitHub 中继续会话](https://github.com/{router.settings.repo}/issues/new?{query})\n\n"
            for event in snapshot["events"]:
                if event["type"] not in {"user/message", "assistant/message"} or not event.get("text"):
                    continue
                role = "用户" if event["type"] == "user/message" else "DSH"
                content += f"## {role}\n\n{redact(event['text'])}\n\n"
            pages = {f"sessions/{folder}/page-{n:03}.md": part for n, part in enumerate(chunks(content, 180000))}
        files.update(pages)
        next_cache[sid] = {"key": key, "pages": pages}
        first = next(iter(pages))
        updated = datetime.fromtimestamp(row.get("updatedAt", 0) / 1000, timezone.utc).isoformat()
        index += f"| [{label(row.get('title', sid))}]({first}) | {'子代理' if row.get('origin') == 'subagent' else '主会话'} | {updated} |\n"
    index_pages = list(chunks(index, 180000))
    if len(index_pages) == 1:
        files["README.md"] += index_pages[0]
    else:
        for n, part in enumerate(index_pages):
            name = f"sessions/index-{n:03}.md"
            files[name] = part
            files["README.md"] += f"[会话目录 {n + 1}]({name})\n\n"
    for name, content in files.items():
        write_if_changed(root / name, content)
    pushed = router.github.publish(files) if publish else False
    write_if_changed(cache_path, json.dumps(next_cache, ensure_ascii=False))
    return {"sessions": len(router.records), "pages": len(files), "published": pushed}

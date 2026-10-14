"""Stable history URLs and a managed Issue panel that preserves task text."""
import hashlib
import re

START = "<!-- dsh2github:history:start -->"
END = "<!-- dsh2github:history:end -->"
NEW_CHAT = "<!-- dsh2github:new -->"


def folder_for(session_id):
    return hashlib.sha256(session_id.encode()).hexdigest()[:32]


def history_path(session_id):
    return f"sessions/{folder_for(session_id)}/page-000.md"


def panel(repo, session_id):
    url = f"https://github.com/{repo}/blob/main/{history_path(session_id)}"
    return (f"{START}\n<!-- dsh2github:bind {session_id} -->\n"
        f"**[查看完整会话记录]({url})**\n\n"
        "首次同步完成后链接可用；后续对话会自动更新到记录中。多页记录可通过页内链接继续查看。\n"
        f"{END}")


def decorate(body, block):
    pattern = re.escape(START) + r"[\s\S]*?" + re.escape(END)
    if re.search(pattern, body):
        return re.sub(pattern, lambda _: block, body, count=1)
    return body + ("\n\n" if body else "") + block

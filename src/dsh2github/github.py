from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from .config import is_private_task_repository

SESSION_LABEL = "dsh-session"


def api(endpoint, method="GET", body=None):
    args = ["gh", "api", endpoint, "--method", method]
    if body is not None:
        args += ["--input", "-"]
    result = subprocess.run(args, input=json.dumps(body, ensure_ascii=False) if body is not None else None,
        capture_output=True, text=True, encoding="utf-8", timeout=45)
    if result.returncode:
        raise RuntimeError(f"GitHub {method} failed for {endpoint.split('?')[0]} (exit {result.returncode})")
    return json.loads(result.stdout) if result.stdout.strip() else {}


class GitHub:
    def __init__(self, repo):
        self.repo = repo
        self.prefix = "repos/" + repo
        self.owner = None
        self._session_label_ready = False

    def validate(self):
        meta = api(self.prefix)
        profile = api("user")
        if not is_private_task_repository(meta) or meta.get("owner", {}).get("type") != "User":
            raise ValueError("Use a dedicated personal private task repository")
        if meta.get("full_name", "").casefold() != self.repo.casefold() or profile.get("login") != meta["owner"]["login"]:
            raise ValueError("Authenticate as the task repository owner")
        self.owner = profile["login"]
        return meta

    def pages(self, endpoint):
        rows, page = [], 1
        while True:
            batch = api(endpoint + ("&" if "?" in endpoint else "?") + f"per_page=100&page={page}")
            rows.extend(batch)
            if len(batch) < 100:
                return rows
            page += 1

    def issues(self):
        return self.pages(self.prefix + "/issues?state=open&sort=created&direction=asc")

    def comments(self, number):
        return self.pages(f"{self.prefix}/issues/{number}/comments")

    def create(self, title, body):
        self.ensure_session_label()
        return api(self.prefix + "/issues", "POST", {"title": title, "body": body, "labels": [SESSION_LABEL]})

    def ensure_session_label(self):
        if self._session_label_ready:
            return
        labels = self.pages(self.prefix + "/labels")
        if not any(row["name"].casefold() == SESSION_LABEL for row in labels):
            api(self.prefix + "/labels", "POST", {"name": SESSION_LABEL, "color": "1F6FEB",
                "description": "连接或启动本机 DeepSeek Harness 会话"})
        self._session_label_ready = True

    def label_session(self, number):
        self.ensure_session_label()
        return api(f"{self.prefix}/issues/{number}/labels", "POST", {"labels": [SESSION_LABEL]})

    def reply(self, number, body, marker):
        # Reconcile first: a lost POST response must never lead to duplicate output.
        for row in self.comments(number):
            if row.get("user", {}).get("login") == self.owner and marker in (row.get("body") or ""):
                return row
        return api(f"{self.prefix}/issues/{number}/comments", "POST", {"body": body})

    def edit_issue(self, number, body):
        return api(f"{self.prefix}/issues/{number}", "PATCH", {"body": body})

    def close(self, number):
        return api(f"{self.prefix}/issues/{number}", "PATCH", {"state": "closed"})

    def publish(self, files):
        metadata = self.validate()
        branch = metadata.get("default_branch") or "main"
        try:
            reference = api(self.prefix + f"/git/ref/heads/{branch}")
        except RuntimeError:
            if metadata.get("size") != 0:
                raise
            api(self.prefix + "/contents/README.md", "PUT", {"message": "Create DSH session index", "branch": branch,
                "content": base64.b64encode(files["README.md"].encode()).decode()})
            reference = api(self.prefix + f"/git/ref/heads/{branch}")
        parent = reference["object"]["sha"]
        tree_id = api(self.prefix + "/git/commits/" + parent)["tree"]["sha"]
        tree = api(self.prefix + "/git/trees/" + tree_id + "?recursive=1")
        if tree.get("truncated"):
            raise RuntimeError("Remote tree is truncated")
        remote = {x["path"]: x["sha"] for x in tree["tree"] if x["type"] == "blob"}
        changes = []
        for name, content in sorted(files.items()):
            raw = content.encode()
            sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
            if remote.get(name) != sha:
                changes.append({"path": name, "mode": "100644", "type": "blob", "content": content})
        if not changes:
            return False
        new_tree = api(self.prefix + "/git/trees", "POST", {"base_tree": tree_id, "tree": changes})
        new_commit = api(self.prefix + "/git/commits", "POST", {"message": "Sync visible DeepSeek Harness conversations",
            "tree": new_tree["sha"], "parents": [parent]})
        api(self.prefix + f"/git/refs/heads/{branch}", "PATCH", {"sha": new_commit["sha"], "force": False})
        return True

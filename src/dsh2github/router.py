from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from .storage import read_json, write_if_changed
from .host import Host, HostError
from .github import GitHub
from .text import redact, label, chunks
from urllib.parse import urlencode

BOT = "<!-- dsh2github:reply -->"
HELP = """直接留言继续当前 DSH 会话；新建 Issue 启动新任务。

| 指令 | 用途 |
|---|---|
| `/session 名称或ID` | 连接本机原会话，支持唯一 ID 前缀 |
| `/models` | 查看本机可用 provider、模型及思考档位 |
| `/model provider/model [档位]` | 选择当前会话后续任务模型 |
| `/model auto` | 跟随本机默认模型 |
| `/effort [档位或auto]` | 查看或调整当前会话思考档位 |
| `/steer 内容` | 送入当前轮次；空闲时作为下一轮 |
| `/queue 内容`、`/queue` | 排队或查看消息 ID |
| `/cancel [消息ID或all]` | 撤回等待消息 |
| `/approvals` | 查看本 Issue 待确认权限 |
| `/allow 请求ID` | 仅允许该次请求 |
| `/deny 请求ID` | 拒绝该次请求 |
| `/status`、`/help` | 状态或说明 |
| `/close` | 明确关闭当前 Issue |

只有仓库所有者的留言生效。权限必须填写完整请求 ID，不能批量允许。
回复结束后 Issue 保持打开。监听器离线时新权限请求回到本机确认。"""


def parse_command(text):
    match = re.fullmatch(r"/([a-z]+)(?:\s+([\s\S]*))?", text.strip())
    return (match[1], (match[2] or "").strip()) if match else None


def completed_result(events, request_id):
    """Identify the exact admitted prompt's turn, never a later/local reply."""
    active = None
    matched = False
    reply = ""
    for event in events:
        kind = event["type"]
        if kind == "turn/start":
            active = event.get("turn")
            matched = False
            reply = ""
        elif kind == "user/message" and event.get("requestId") == request_id:
            matched = True
        elif kind == "assistant/message" and matched and event.get("text"):
            reply = event["text"]
        elif kind == "turn/end" and matched and (event.get("turn") == active or active is None):
            reason = event.get("reason")
            reason_kind = reason.get("kind") if isinstance(reason, dict) else reason
            ok = reason_kind in {"completed", "success", "stop", "done"}
            return {"ok": ok, "text": reply, "reason": reason}
    return None


class Router:
    def __init__(self, settings, github=None, host=None):
        self.settings = settings
        self.github = github or GitHub(settings.repo)
        self.host = host or Host(settings.dsh_home)
        self.path = settings.runtime_dir / "conversation-state.json"
        self.state = read_json(self.path, {"version": 1, "issues": {}, "models": {}, "jobs": {}, "approvals": {}})
        self.records = {}
        self.catalog = None
        self.route = None
        from .artifacts import Artifacts
        self.artifacts = Artifacts(settings, self.github)

    def save(self):
        write_if_changed(self.path, json.dumps(self.state, ensure_ascii=False, indent=2) + "\n")

    def refresh(self):
        health = self.host.call("health")
        fingerprint = health.get("routeFingerprint") or hashlib.sha256(json.dumps(health.get("default", {}), sort_keys=True).encode()).hexdigest()
        if self.state.get("route") != fingerprint:
            self.state["models"].clear()
            for info in self.state["issues"].values():
                for item in info["queue"]:
                    item["selection"] = dict(health.get("default", {}))
            self.state["route"] = fingerprint
            self.save()
        self.route = health.get("default", {})
        self.records = {x["sessionId"]: x for x in self.host.call("sessions")["items"]}

    def owned_sessions(self):
        bound = {info["sid"] for info in self.state["issues"].values() if info.get("sid") and not info.get("closed")}
        for _ in range(len(self.records)):
            extra = {sid for sid, row in self.records.items() if row.get("parentSessionId") in bound}
            if extra <= bound:
                break
            bound.update(extra)
        return sorted(bound)

    def reply(self, number, text, key):
        result = None
        for page, content in enumerate(chunks(redact(text))):
            part_key = key if page == 0 else key + f"-part {page}"
            marker = f"<!-- dsh2github:{part_key} -->"
            result = self.github.reply(number, f"{BOT}\n{marker}\n{content}", marker)
        return result

    def setup(self):
        self.github.validate()
        self.refresh()
        if not self.state.get("hub"):
            for issue in self.github.issues():
                if issue.get("user", {}).get("login") == self.github.owner and "<!-- dsh2github:hub -->" in (issue.get("body") or ""):
                    self.state["hub"] = issue["number"]
                    break
        if not self.state.get("hub"):
            row = self.github.create("DeepSeek Harness 会话入口", "<!-- dsh2github:hub -->\n" + HELP)
            self.state["hub"] = row["number"]
        self.save()
        self.update_hub()
        return self.state["hub"]

    def update_hub(self):
        body = "<!-- dsh2github:hub -->\n" + HELP + "\n\n## 本机会话\n\n| 名称 | ID | 状态 |\n|---|---|---|\n"
        for sid, row in self.records.items():
            if row.get("origin") != "subagent":
                room = next((n for n, info in self.state["issues"].items()
                    if info.get("sid") == sid and not info.get("closed") and int(n) != self.state.get("hub")), None)
                if room:
                    url = f"https://github.com/{self.settings.repo}/issues/{room}"
                else:
                    query = urlencode({"labels": "dsh-session", "title": "对话：" + row.get("title", sid)[:100],
                        "body": f"<!-- dsh2github:bind {sid} -->\n继续本机原会话，直接在下方留言。"})
                    url = f"https://github.com/{self.settings.repo}/issues/new?{query}"
                body += f"| [{label(row.get('title', sid))}]({url}) | `{sid}` | {'执行中' if row.get('running') else '空闲'} |\n"
        # Keep the mobile entry within GitHub's body size cap. Full index is in README.
        parts = list(chunks(body))
        if len(parts) > 1:
            body = parts[0] + "\n\n完整列表请查看仓库 README。"
        digest = hashlib.sha256(body.encode()).hexdigest()
        if self.state.get("hub") and self.state.get("hub_digest") != digest:
            self.github.edit_issue(self.state["hub"], body)
            self.state["hub_digest"] = digest
            self.save()

    def ensure_issue(self, issue):
        number = issue["number"]
        info = self.state["issues"].setdefault(str(number), {"sid": None, "cursor": 0, "queue": [], "seen": []})
        if not info.get("session_label"):
            self.github.label_session(number)
            info["session_label"] = True
            self.save()
        if info.get("initialized"):
            return info
        if number != self.state.get("hub"):
            bind = re.search(r"<!-- dsh2github:bind ([A-Za-z0-9_.:-]+) -->", issue.get("body") or "")
            if bind:
                self.bind(number, bind[1], "bind")
            else:
                # Persist the caller-minted session ID before contacting DSH.
                sid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://github.com/{self.settings.repo}/issues/{number}"))
                info["sid"] = sid
                self.save()
                self.settings.workspace.mkdir(parents=True, exist_ok=True)
                self.host.call("create", {"sessionId": sid, "cwd": str(self.settings.workspace)})
                body = (issue.get("body") or "").strip()
                prompt = issue["title"] + ("\n\n" + body if body else "")
                self.enqueue(number, prompt, 0)
        info["initialized"] = True
        self.save()
        return info

    def select_session(self, selector):
        matches = [sid for sid, row in self.records.items() if sid == selector or sid.startswith(selector) or row.get("title") == selector]
        if len(matches) != 1:
            raise ValueError("会话不存在或名称／ID 前缀不唯一；请使用完整 ID")
        sid = matches[0]
        if self.records[sid].get("origin") == "subagent":
            raise ValueError("子代理历史可查看；不能作为普通会话继续")
        return sid

    def bind(self, number, selector, key):
        info = self.state["issues"][str(number)]
        if info["queue"] or str(number) in self.state["jobs"]:
            raise ValueError("当前 Issue 有执行或等待消息，处理完后再切换会话")
        info["sid"] = self.select_session(selector)
        self.save()
        snapshot = self.host.call("session", {"sessionId": info["sid"]})
        policy = snapshot.get("approvalPolicy", "unknown")
        hint = ("\n\n当前会话审批策略为 never：DSH 会直接拒绝需要确认的操作，不会产生远端 Allow 卡。"
                "请在 DSH 中将这个会话设置为需要确认的模式后，再执行相关任务。" if policy == "never" else
                "\n\n原生权限请求会显示卡片，可用 `/allow 请求ID` 或 `/deny 请求ID`。" if policy == "ask" else
                "\n\n仅在 DSH 实际发出权限确认请求时显示远端确认卡。")
        self.reply(number, f"已连接原 DSH 会话 `{info['sid']}`。直接留言继续。" + hint, key)

    def selection(self, info):
        return self.state["models"].get(info.get("sid"))

    def enqueue(self, number, text, comment):
        info = self.state["issues"][str(number)]
        if not info.get("sid"):
            raise ValueError("先用 `/session 名称或ID` 连接会话")
        if any(x["comment"] == comment for x in info["queue"]) or str(comment) in info["seen"]:
            return
        info["queue"].append({"comment": comment, "sid": info["sid"], "text": text,
            "selection": dict(self.selection(info) or self.route or {}), "requestId": self.request_id(number, comment)})
        self.save()

    def request_id(self, number, comment):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://github.com/{self.settings.repo}/issues/{number}#issuecomment-{comment}"))

    def command(self, number, text, comment):
        info = self.state["issues"][str(number)]
        key = f"command {comment}"
        parsed = parse_command(text)
        if parsed is None:
            self.enqueue(number, text, comment)
            return
        name, argument = parsed
        if name == "session":
            self.bind(number, argument, key)
        elif name == "help":
            self.reply(number, HELP, key)
        elif name == "status":
            row = self.records.get(info.get("sid"), {})
            self.reply(number, f"会话：`{info.get('sid') or '未连接'}`\n\n模型：`{json.dumps(self.selection(info) or self.route, ensure_ascii=False)}`\n\n状态：{'执行中' if row.get('running') or str(number) in self.state['jobs'] else '空闲'}\n\n等待消息：{len(info['queue'])}", key)
        elif name in {"allow", "deny"}:
            self.decide(number, argument, "allowed-once" if name == "allow" else "rejected", comment)
        elif name == "approvals":
            self.poll_approvals()
            ids = [x["id"] for x in self.state["approvals"].values() if x["issue"] == number and x.get("pending")]
            self.reply(number, "待确认请求：" + ("、".join(f"`{x}`" for x in ids) if ids else "无"), key)
        elif name == "models":
            self.catalog = self.host.call("models")
            body = "| provider/model | 思考档位 |\n|---|---|\n"
            for group in self.catalog.get("groups", []):
                for model in group["models"]:
                    efforts = ", ".join(x["id"] for x in model.get("reasoning", {}).get("efforts", [])) or "由 DSH 默认处理"
                    body += f"| `{group['id']}/{model['id']}` | {efforts} |\n"
            for failure in self.catalog.get("failures", []):
                body += f"\n`{failure['id']}`：目录暂不可用。\n"
            self.reply(number, body, key)
        elif name in {"model", "effort", "reasoning"}:
            self.set_model(number, name, argument, key)
        elif name == "queue":
            if argument:
                self.enqueue(number, argument, comment)
                self.reply(number, f"已排队，消息 ID：`{comment}`。", key)
            else:
                rows = "\n".join(f"- `{x['comment']}`：{label(x['text'][:100])}" for x in info["queue"])
                self.reply(number, rows or "当前没有等待消息。", key)
        elif name == "cancel":
            self.cancel(number, argument or "last", key)
        elif name == "steer":
            if not argument or not info.get("sid"):
                raise ValueError("连接会话后使用 `/steer 内容`")
            previous = self.state.get("steers", {}).get(str(comment))
            snapshot = self.host.call("session", {"sessionId": info["sid"]})
            if not snapshot.get("running") and not previous:
                self.enqueue(number, argument, comment)
                self.reply(number, "会话空闲，已作为下一轮消息排队。", key)
            else:
                self.state.setdefault("steers", {})[str(comment)] = {"issue": number, "requestId": self.request_id(number, comment), "sid": info["sid"], "pending": True}
                self.save()
                result = self.host.call("prompt", {"sessionId": info["sid"], "requestId": self.request_id(number, comment), "mode": "steer", "text": argument})
                self.reply(number, "👀 已读 → 🟡 已提交 → 🟢 DSH 已接收当前轮次引导。" if result.get("accepted") else "引导未确认。", key)
                self.state["steers"][str(comment)]["pending"] = False
                self.save()
        elif name == "close":
            self.github.close(number)
            info["closed"] = True
            self.save()
        else:
            raise ValueError("未知指令；使用 `/help` 查看支持的指令")

    def set_model(self, number, name, argument, key):
        info = self.state["issues"][str(number)]
        if not info.get("sid"):
            raise ValueError("先连接会话")
        sid = info["sid"]
        current = dict(self.selection(info) or self.route or {})
        if not argument:
            self.reply(number, "当前选择：`" + json.dumps(current, ensure_ascii=False) + "`", key)
            return
        if name == "model" and argument == "auto":
            self.state["models"].pop(sid, None)
        else:
            self.catalog = self.host.call("models")
            if name == "model":
                values = argument.split()
                if len(values) > 2:
                    raise ValueError("使用 `/model provider/model [档位]`")
                token = values[0]
                matches = [(g["id"], m) for g in self.catalog.get("groups", []) for m in g["models"]
                           if token in {m["id"], g["id"] + "/" + m["id"]}]
                if len(matches) != 1:
                    raise ValueError("模型不存在或跨 provider 重名，请填写 `/models` 中的 provider/model")
                provider, model = matches[0]
                current = {"provider": provider, "model": model["id"]}
                effort = values[1] if len(values) == 2 else None
            else:
                effort = None if argument == "auto" else argument
                model = next((m for g in self.catalog.get("groups", []) if g["id"] == current.get("provider")
                    for m in g["models"] if m["id"] == current.get("model")), None)
                if model is None:
                    raise ValueError("当前模型不在可用目录中")
            if effort:
                supported = {x["id"] for x in model.get("reasoning", {}).get("efforts", [])}
                if effort not in supported:
                    raise ValueError("此模型不支持该思考档位；使用 `/models` 查看")
                current["reasoningEffort"] = effort
            else:
                current.pop("reasoningEffort", None)
            self.state["models"][sid] = current
        self.save()
        self.reply(number, "已设置当前会话后续任务的模型／档位。正在执行的轮次继续完成。", key)

    def cancel(self, number, selector, key):
        info = self.state["issues"][str(number)]
        pending = info["queue"]
        if selector not in {"last", "all"} and not selector.isdigit():
            raise ValueError("使用 `/cancel`、`/cancel 消息ID` 或 `/cancel all`")
        remove = pending[-1:] if selector == "last" else pending[:] if selector == "all" else [x for x in pending if x["comment"] == int(selector)]
        ids = {x["comment"] for x in remove}
        info["queue"] = [x for x in pending if x["comment"] not in ids]
        info["seen"].extend(str(x) for x in ids)
        self.save()
        self.reply(number, f"已撤回 {len(remove)} 条等待消息；当前执行轮次继续运行。", key)

    def room_for(self, sid):
        visited = set()
        while sid and sid not in visited:
            visited.add(sid)
            matches = [int(n) for n, info in self.state["issues"].items() if info.get("sid") == sid]
            if matches:
                jobs = [n for n in matches if str(n) in self.state["jobs"]]
                return jobs[0] if jobs else matches[0]
            sid = self.records.get(sid, {}).get("parentSessionId")
        return None

    def poll_approvals(self):
        snapshot = self.host.call("approvals")
        live = {x["id"]: x for x in snapshot["items"]}
        for request in live.values():
            number = self.room_for(request["sessionId"]) or self.room_for(request.get("rootSessionId"))
            if number is None:
                continue
            stored = self.state["approvals"].setdefault(request["id"], {**request, "issue": number, "pending": True})
            self.save()
            text = (f"🔒 DSH 需要确认\n\n会话：`{request['sessionId']}`\n\n工具：`{label(request['toolName'])}`\n\n"
                f"原因：{redact(request.get('reason') or 'DSH 权限策略要求确认')}\n\n请求 ID：`{request['id']}`\n\n"
                f"仅允许这一次：`/allow {request['id']}`\n\n拒绝：`/deny {request['id']}`\n\n"
                "有效期最长 15 分钟；只有本仓库所有者的完整指令可操作。")
            if request.get("arguments"):
                details = "\n".join("    " + line for line in redact(request["arguments"]).splitlines())
                text += "\n\n本次工具参数：\n\n" + details
            if not stored.get("published"):
                self.reply(number, text, "approval " + request["id"])
                stored["published"] = True
                self.save()
        for request in self.state["approvals"].values():
            if request.get("pending") and request["id"] not in live:
                request["pending"] = False
                self.save()
                self.reply(request["issue"], f"权限请求 `{request['id']}` 已结束或失效，旧指令不会作用于其它请求。", "expired " + request["id"])

    def decide(self, number, request_id, decision, comment):
        if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", request_id):
            raise ValueError("必须使用卡片中的完整权限请求 ID；不支持省略 ID 或批量授权")
        request = self.state["approvals"].get(request_id)
        if not request or request["issue"] != number:
            raise ValueError("该请求不属于当前 Issue，或未同步到本桥接器")
        if not request.get("pending") and not request.get("decision_inflight"):
            raise ValueError("请求已结束或失效")
        action = {"id": request_id, "sessionId": request["sessionId"], "generation": request["generation"],
                  "decision": decision, "commentId": str(comment)}
        # Persist before dispatch so recovery can reconcile the same receipt.
        if request.get("decision_inflight") and request["decision_inflight"] != action:
            raise ValueError("此权限已有操作等待确认，不能用另一个指令覆盖")
        request["decision_inflight"] = action
        self.save()
        result = self.host.call("approval/decide", action)
        if not result.get("accepted"):
            raise ValueError("DSH 未确认该权限操作")
        request["pending"] = False
        request["decision"] = decision
        request.pop("decision_inflight", None)
        self.save()
        self.reply(number, f"{'✅ 已仅允许这一次' if decision == 'allowed-once' else '🚫 已拒绝'}：`{request_id}`。DSH 已确认。", f"decision {comment}")

    def finish_jobs(self):
        for number, job in list(self.state["jobs"].items()):
            if job.get("failed"):
                result = {"ok": False, "text": "DSH 未接受这条任务：" + job["failed"], "reason": "submission-rejected"}
            else:
                snapshot = self.host.call("session", {"sessionId": job["sid"]})
                result = completed_result(snapshot["events"], job["requestId"])
            if result is None:
                continue
            text = ("✅ " if result["ok"] else "❌ ") + (result["text"] or "DSH 轮次结束，未产生可见回复。")
            text = self.artifacts.rewrite(text, None if job.get("failed") else snapshot.get("cwd"))
            if not result["ok"]:
                text += "\n\n轮次结束原因：`" + json.dumps(result["reason"], ensure_ascii=False) + "`"
            for page, part in enumerate(chunks(text)):
                # First page uses the inherited completion notifier marker.
                key = f"result {job['comment']}" if page == 0 else f"result-part {job['comment']} {page}"
                self.reply(int(number), part, key)
            self.state["jobs"].pop(number)
            self.save()

    def dispatch_queue(self):
        occupied = {job["sid"] for job in self.state["jobs"].values()}
        for number, info in self.state["issues"].items():
            if not info["queue"] or number in self.state["jobs"] or info.get("closed"):
                continue
            item = info["queue"][0]
            if item["sid"] in occupied:
                continue
            snapshot = self.host.call("session", {"sessionId": item["sid"]})
            if snapshot.get("running") or snapshot.get("queue"):
                continue
            self.state["jobs"][number] = {**item, "submitted": False}
            self.save()
            self.submit(number, self.state["jobs"][number])
            occupied.add(item["sid"])

    def submit(self, number, job):
        # DSH requestId provides native deduplication, including uncertain sends.
        try:
            result = self.host.call("prompt", {"sessionId": job["sid"], "requestId": job["requestId"],
                "mode": "queue", "text": job["text"], "selection": job.get("selection")})
        except HostError as error:
            if error.uncertain:
                raise
            job["failed"] = str(error)
            info = self.state["issues"][number]
            info["queue"] = [x for x in info["queue"] if x["requestId"] != job["requestId"]]
            info["seen"].append(str(job["comment"]))
            self.save()
            return
        if not result.get("accepted"):
            raise ValueError("DSH 未确认消息提交")
        job["submitted"] = True
        info = self.state["issues"][number]
        info["queue"] = [x for x in info["queue"] if x["requestId"] != job["requestId"]]
        if str(job["comment"]) not in info["seen"]:
            info["seen"].append(str(job["comment"]))
        self.save()
        self.reply(int(number), f"DSH 已接收消息，原会话：`{job['sid']}`。完成后在此回传。", f"submitted {job['comment']}")

    def tick(self):
        self.github.validate()
        self.refresh()
        self.host.call("heartbeat", {"sessions": self.owned_sessions()})
        # Discover cards before comments so a new allow cannot outrun its request.
        self.poll_approvals()
        issues = self.github.issues()
        open_numbers = {str(x["number"]) for x in issues if "pull_request" not in x}
        for number, info in self.state["issues"].items():
            info["closed"] = number not in open_numbers
        self.save()
        for issue in issues:
            if "pull_request" in issue or issue.get("user", {}).get("login") != self.github.owner:
                continue
            number = issue["number"]
            info = self.ensure_issue(issue)
            for comment in self.github.comments(number):
                if comment["id"] <= info["cursor"]:
                    continue
                body = (comment.get("body") or "").strip()
                if comment.get("user", {}).get("login") == self.github.owner and comment.get("user", {}).get("type") == "User" and body and not body.startswith("<!-- dsh2github:"):
                    try:
                        self.command(number, body, comment["id"])
                    except HostError as error:
                        if error.uncertain:
                            self.reply(number, "操作已发出，DSH 回执暂未确认；正在核对同一请求，勿重复发送。", f"uncertain {comment['id']}")
                            self.save()
                            raise
                        self.reply(number, "操作未完成：" + str(error), f"error {comment['id']}")
                    except ValueError as error:
                        self.reply(number, str(error), f"error {comment['id']}")
                info["cursor"] = comment["id"]
                self.save()
        self.host.call("heartbeat", {"sessions": self.owned_sessions()})
        for number, job in list(self.state["jobs"].items()):
            if not job.get("submitted"):
                self.submit(number, job)
        self.finish_jobs()
        self.dispatch_queue()
        self.update_hub()


class Heartbeat:
    def __init__(self, router):
        self.router = router
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.router.host.call("heartbeat", {"sessions": self.router.owned_sessions()})
            except (HostError, RuntimeError):
                pass
            self.stop_event.wait(10)

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=2)
        try:
            self.router.host.call("heartbeat", {"sessions": []})
        except HostError:
            pass

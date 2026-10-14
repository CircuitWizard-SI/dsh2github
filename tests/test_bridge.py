from pathlib import Path
import tempfile
import unittest
from dsh2github.config import Settings, is_private_task_repository
from dsh2github.router import Router, completed_result
from dsh2github.host import HostError, install_host
from dsh2github.text import chunks, redact


class FakeGitHub:
    owner = "owner"
    def __init__(self):
        self.posts, self.rows, self.remote_comments, self.closed = [], [], {}, []
    def validate(self):
        return {"private": True}
    def issues(self):
        return self.rows
    def comments(self, number):
        return self.remote_comments.get(number, [])
    def reply(self, number, body, marker):
        if not any(x[0] == number and x[2] == marker for x in self.posts):
            self.posts.append((number, body, marker))
        return {"id": len(self.posts)}
    def edit_issue(self, number, body):
        pass
    def close(self, number):
        self.closed.append(number)
    def label_session(self, number):
        self.labeled = getattr(self, "labeled", []) + [number]


class FakeHost:
    def __init__(self):
        self.calls = []
        self.default = {"provider": "local", "model": "model-a"}
        self.rows = [{"sessionId": "session-a", "title": "Example", "running": False}]
        self.snapshots = {"session-a": {"events": [], "running": False, "queue": []}}
        self.approvals = []
        self.fail_prompt = None
    def call(self, operation, body=None):
        body = body or {}
        self.calls.append((operation, body))
        if operation == "health":
            return {"default": self.default, "generation": "host-generation", "protocol": 1}
        if operation == "sessions":
            return {"items": self.rows}
        if operation == "heartbeat":
            return {"ok": True}
        if operation == "approvals":
            return {"generation": "host-generation", "items": self.approvals}
        if operation == "session":
            return self.snapshots.get(body["sessionId"], {"events": [], "running": False, "queue": []})
        if operation == "prompt":
            if self.fail_prompt:
                raise self.fail_prompt
            return {"accepted": True}
        if operation == "approval/decide":
            row = next((x for x in self.approvals if x["id"] == body["id"]), None)
            if not row:
                raise HostError("stale")
            self.approvals.remove(row)
            return {"accepted": True, **body}
        if operation == "models":
            return {"groups": [{"id": "local", "models": [
                {"id": "model-a", "reasoning": {"efforts": [{"id": "high"}, {"id": "low"}]}},
                {"id": "model-b"}]}]}
        if operation == "create":
            return {"sessionId": body["sessionId"]}
        raise AssertionError(operation)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".test-runtime"
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "config.json").write_text("{}")
        self.settings = Settings(root / "config.json", "owner/tasks", root / "dsh", root / "workspace", root / "runtime")
        self.github, self.host = FakeGitHub(), FakeHost()
        self.router = Router(self.settings, self.github, self.host)
        self.router.refresh()
        self.router.state["issues"]["1"] = {"sid": "session-a", "queue": [], "cursor": 0, "seen": [], "initialized": True}

    def approval(self):
        row = {"id": "12345678-1234-1234-1234-123456789abc", "sessionId": "session-a",
            "generation": "host-generation", "toolName": "read_file", "reason": "Read outside workspace"}
        self.host.approvals.append(row)
        self.router.poll_approvals()
        return row["id"]

    def test_exact_allow(self):
        request = self.approval()
        self.router.command(1, "/allow " + request, 101)
        call = next(body for op, body in self.host.calls if op == "approval/decide")
        self.assertEqual(call["decision"], "allowed-once")
        self.assertEqual(call["sessionId"], "session-a")
        self.assertEqual(call["generation"], "host-generation")
        self.assertFalse(self.router.state["approvals"][request]["pending"])

    def test_deny(self):
        request = self.approval()
        self.router.command(1, "/deny " + request, 101)
        self.assertEqual(self.router.state["approvals"][request]["decision"], "rejected")

    def test_approval_cross_issue_rejected(self):
        request = self.approval()
        self.router.state["issues"]["2"] = {"sid": "session-a", "queue": [], "seen": [], "cursor": 0}
        with self.assertRaises(ValueError):
            self.router.command(2, "/allow " + request, 101)
        self.assertFalse(any(op == "approval/decide" for op, _ in self.host.calls))

    def test_allow_requires_full_id(self):
        self.approval()
        for value in ["/allow", "/allow all", "/allow 12345678"]:
            with self.assertRaises(ValueError):
                self.router.command(1, value, 101)

    def test_expired_request_cannot_allow(self):
        request = self.approval()
        self.host.approvals.clear()
        self.router.poll_approvals()
        with self.assertRaises(ValueError):
            self.router.command(1, "/allow " + request, 101)

    def test_only_owner_comments_execute(self):
        self.github.rows = [{"number": 1, "user": {"login": "owner"}}]
        self.github.remote_comments[1] = [
            {"id": 1, "user": {"login": "intruder", "type": "User"}, "body": "execute"},
            {"id": 2, "user": {"login": "owner", "type": "Bot"}, "body": "execute"},
            {"id": 3, "user": {"login": "owner", "type": "User"}, "body": "real task"}]
        self.router.tick()
        self.assertEqual([b["text"] for op, b in self.host.calls if op == "prompt"], ["real task"])

    def test_non_owner_issues_ignored(self):
        self.github.rows = [{"number": 2, "user": {"login": "intruder"}, "title": "run", "body": "run"}]
        self.router.tick()
        self.assertNotIn("2", self.router.state["issues"])

    def test_comment_poll_is_idempotent(self):
        self.github.rows = [{"number": 1, "user": {"login": "owner"}}]
        self.github.remote_comments[1] = [{"id": 10, "user": {"login": "owner", "type": "User"}, "body": "hello"}]
        self.router.tick(); self.router.tick()
        self.assertEqual(sum(op == "prompt" for op, _ in self.host.calls), 1)

    def test_existing_sessions_receive_label_once(self):
        self.github.rows = [{"number": 1, "user": {"login": "owner"}}]
        self.router.tick()
        self.assertEqual(self.github.labeled, [1])
        self.router.tick()
        self.assertEqual(self.github.labeled, [1])
        self.assertTrue(self.router.state["issues"]["1"]["session_label"])

    def test_github_timeout_is_recoverable_by_listener(self):
        import subprocess
        from unittest.mock import patch
        from dsh2github.github import api
        with patch("dsh2github.github.subprocess.run", side_effect=subprocess.TimeoutExpired(["gh", "api"], 15)):
            with self.assertRaises(RuntimeError):
                api("repos/owner/tasks")

    def test_publish_uses_real_ref_when_repo_size_is_stale(self):
        from unittest.mock import patch
        from dsh2github.github import GitHub
        import hashlib
        github = GitHub("owner/tasks")
        raw = b"hello"
        sha = hashlib.sha1(b"blob 5\0" + raw).hexdigest()
        def request(endpoint, method="GET", body=None):
            self.assertEqual(method, "GET")
            if endpoint == "repos/owner/tasks":
                return {"private": True, "name": "tasks", "full_name": "owner/tasks", "size": 0,
                    "default_branch": "main", "owner": {"login": "owner", "type": "User"}}
            if endpoint == "user":
                return {"login": "owner"}
            if "/git/ref/heads/" in endpoint:
                return {"object": {"sha": "parent"}}
            if "/git/commits/" in endpoint:
                return {"tree": {"sha": "tree"}}
            if "/git/trees/" in endpoint:
                return {"tree": [{"path": "README.md", "type": "blob", "sha": sha}]}
            raise AssertionError(endpoint)
        with patch("dsh2github.github.api", request):
            self.assertFalse(github.publish({"README.md": "hello"}))

    def test_queue_cancel_preserves_active_job(self):
        self.router.enqueue(1, "one", 10); self.router.dispatch_queue()
        self.router.enqueue(1, "two", 20); self.router.command(1, "/cancel all", 30)
        self.assertEqual(self.router.state["issues"]["1"]["queue"], [])
        self.assertEqual(self.router.state["jobs"]["1"]["comment"], 10)

    def test_same_session_has_only_one_job(self):
        self.router.state["issues"]["2"] = {"sid": "session-a", "queue": [], "cursor": 0, "seen": []}
        self.router.enqueue(1, "one", 10); self.router.enqueue(2, "two", 20)
        self.router.dispatch_queue()
        self.assertEqual(len(self.router.state["jobs"]), 1)

    def test_running_local_session_waits(self):
        self.host.snapshots["session-a"]["running"] = True
        self.router.enqueue(1, "one", 10); self.router.dispatch_queue()
        self.assertFalse(any(op == "prompt" for op, _ in self.host.calls))

    def test_model_override_and_auto(self):
        self.router.command(1, "/model local/model-b", 1)
        self.assertEqual(self.router.state["models"]["session-a"], {"provider": "local", "model": "model-b"})
        self.assertFalse(any(op in {"selectModel", "config", "settings"} for op, _ in self.host.calls))
        self.router.command(1, "/model auto", 2); self.router.enqueue(1, "auto task", 3)
        self.assertEqual(self.router.state["issues"]["1"]["queue"][0]["selection"], self.host.default)

    def test_route_change_clears_overrides_and_queued_selection(self):
        self.router.command(1, "/model model-b", 1); self.router.enqueue(1, "one", 10)
        self.host.default = {"provider": "local", "model": "model-new"}
        self.router.refresh()
        self.assertEqual(self.router.state["models"], {})
        self.assertEqual(self.router.state["issues"]["1"]["queue"][0]["selection"], self.host.default)

    def test_effort_validated_against_model(self):
        self.router.command(1, "/effort high", 1)
        self.assertEqual(self.router.state["models"]["session-a"]["reasoningEffort"], "high")
        with self.assertRaises(ValueError):
            self.router.command(1, "/effort impossible", 2)

    def test_steer_goes_to_active_turn(self):
        self.host.snapshots["session-a"]["running"] = True
        self.router.command(1, "/steer clarification", 10)
        prompt = next(body for op, body in self.host.calls if op == "prompt")
        self.assertEqual(prompt["mode"], "steer")
        self.assertNotIn("selection", prompt)

    def test_steer_idle_is_queued(self):
        self.router.command(1, "/steer clarification", 10)
        self.assertEqual(self.router.state["issues"]["1"]["queue"][0]["text"], "clarification")

    def test_uncertain_submit_retains_id_for_recovery(self):
        self.router.enqueue(1, "hello", 10)
        self.host.fail_prompt = HostError("uncertain", uncertain=True)
        with self.assertRaises(HostError):
            self.router.dispatch_queue()
        request_id = self.router.state["jobs"]["1"]["requestId"]
        self.host.fail_prompt = None
        self.router.submit("1", self.router.state["jobs"]["1"])
        self.assertEqual([b["requestId"] for op, b in self.host.calls if op == "prompt"], [request_id, request_id])

    def test_explicit_submit_rejection_reports_once_and_frees_queue(self):
        self.router.enqueue(1, "bad", 10)
        self.host.fail_prompt = HostError("model unavailable")
        self.router.dispatch_queue()
        self.assertTrue(self.router.state["jobs"]["1"]["failed"])
        self.router.finish_jobs()
        self.assertNotIn("1", self.router.state["jobs"])
        self.assertEqual(self.router.state["issues"]["1"]["queue"], [])
        self.assertIn("DSH 未接受", self.github.posts[-1][1])

    def test_result_requires_own_prompt_and_turn_end(self):
        events = [
            {"type": "turn/start", "turn": 1}, {"type": "user/message", "requestId": "other"},
            {"type": "assistant/message", "text": "wrong"}, {"type": "turn/end", "turn": 1, "reason": {"kind": "completed"}},
            {"type": "turn/start", "turn": 2}, {"type": "user/message", "requestId": "wanted"},
            {"type": "assistant/message", "text": "progress"}, {"type": "assistant/message", "text": "final"}]
        self.assertIsNone(completed_result(events, "wanted"))
        events.append({"type": "turn/end", "turn": 2, "reason": {"kind": "completed"}})
        self.assertEqual(completed_result(events, "wanted")["text"], "final")
        self.assertTrue(completed_result(events, "wanted")["ok"])

    def test_failure_never_reports_success(self):
        events = [{"type": "turn/start", "turn": 1}, {"type": "user/message", "requestId": "wanted"},
            {"type": "turn/end", "turn": 1, "reason": {"kind": "aborted"}}]
        self.assertFalse(completed_result(events, "wanted")["ok"])

    def test_children_route_to_parent_issue(self):
        self.router.records["child"] = {"parentSessionId": "session-a", "origin": "subagent"}
        self.assertEqual(self.router.room_for("child"), 1)
        self.assertIn("child", self.router.owned_sessions())

    def test_unknown_commands_do_not_execute(self):
        with self.assertRaises(ValueError):
            self.router.command(1, "/unsupported shell", 10)
        self.assertFalse(self.router.state["issues"]["1"]["queue"])

    def test_only_explicit_close_closes(self):
        self.router.command(1, "/status", 1)
        self.assertFalse(self.github.closed)
        self.router.command(1, "/close", 2)
        self.assertEqual(self.github.closed, [1])

    def test_private_repo_guard(self):
        self.assertFalse(is_private_task_repository({"private": False, "name": "tasks"}))
        self.assertFalse(is_private_task_repository({"private": True, "name": "dsh2github"}))
        self.assertTrue(is_private_task_repository({"private": True, "name": "tasks"}))

    def test_unicode_size_and_redaction(self):
        text = "中文🙂" * 10000
        pieces = list(chunks(text))
        self.assertEqual("".join(pieces), text)
        self.assertTrue(all(len(x.encode()) <= 45000 for x in pieces))
        secret = "sk-" + "a" * 30
        self.assertNotIn(secret, redact("key " + secret))

    def test_host_install_preserves_patch_and_updates_entry_identity(self):
        profile = self.settings.dsh_home / "profiles/desktop"
        profile.mkdir(parents=True)
        source = "- id: existing\n  config:\n    keep: true\n"
        (profile / "cordis.patch.yml").write_text(source)
        self.assertTrue(install_host(self.settings)["changed"])
        original = (profile / "cordis.patch.yml").read_text()
        self.assertTrue(original.startswith(source))
        self.assertIn("id: dsh2github-host-", original)
        self.assertFalse(install_host(self.settings)["changed"])
        import re
        legacy = re.sub(r"id: dsh2github-host-[a-f0-9]+", "id: dsh2github-host", original)
        (profile / "cordis.patch.yml").write_text(legacy)
        self.assertTrue(install_host(self.settings)["changed"])
        self.assertEqual((profile / "cordis.patch.yml").read_text(), original)


if __name__ == "__main__":
    unittest.main()

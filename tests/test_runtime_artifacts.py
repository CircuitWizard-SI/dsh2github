import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from dsh2github.config import Settings
from dsh2github.runtime import listener_status
from dsh2github.artifacts import Artifacts


class RuntimeArtifactTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".test-runtime"
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = Settings(root / "config.json", "owner/tasks", root / "dsh", root / "workspace", root / "runtime")
        self.settings.runtime_dir.mkdir()
        self.settings.workspace.mkdir()

    def test_dead_process_never_reports_ready_from_old_success(self):
        row = {"ok": True, "time": time.time(), "pid": 99999999}
        (self.settings.runtime_dir / "watch-status.json").write_text(json.dumps(row))
        self.assertFalse(listener_status(self.settings)["ready"])

    def test_live_process_requires_fresh_poll(self):
        path = self.settings.runtime_dir / "watch-status.json"
        path.write_text(json.dumps({"ok": True, "time": time.time() - 300, "pid": os.getpid()}))
        self.assertFalse(listener_status(self.settings)["ready"])
        path.write_text(json.dumps({"ok": True, "time": time.time(), "pid": os.getpid()}))
        self.assertTrue(listener_status(self.settings)["ready"])

    def test_local_image_is_published_and_link_becomes_remote(self):
        class GitHub:
            prefix = "repos/owner/tasks"
            def validate(self): return {"private": True}
            def artifact_blob(self, _name): return None
        path = self.settings.workspace / "image.png"
        path.write_bytes(b"synthetic-image-fixture")
        with patch("dsh2github.artifacts.api", return_value={}) as api:
            result = Artifacts(self.settings, GitHub()).rewrite(f"![图]({path})", self.settings.workspace)
        self.assertIn("https://github.com/owner/tasks/blob/main/artifacts/", result)
        self.assertIn("?raw=true", result)
        self.assertEqual(api.call_args.args[1], "PUT")

    def test_credentials_and_files_outside_workspace_are_not_uploaded(self):
        class GitHub:
            prefix = "repos/owner/tasks"
            def validate(self): raise AssertionError("must not upload")
        secret = self.settings.workspace / "auth.json"
        outside = Path(self.temp.name) / "outside.png"
        secret.write_text("secret"); outside.write_bytes(b"outside")
        text = f"[密钥]({secret}) ![图]({outside})"
        self.assertEqual(Artifacts(self.settings, GitHub()).rewrite(text, None), text)


if __name__ == "__main__":
    unittest.main()

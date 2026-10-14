from __future__ import annotations

from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import urllib.error
import urllib.request


class HostError(RuntimeError):
    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class Host:
    def __init__(self, dsh_home):
        self.endpoint_path = dsh_home / "dsh2github/endpoint.json"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, operation, body=None):
        try:
            endpoint = json.loads(self.endpoint_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HostError("DSH Host is unavailable; run install-host and keep Harness open") from None
        if endpoint.get("host") != "127.0.0.1" or endpoint.get("protocol") != 1 or not isinstance(endpoint.get("port"), int):
            raise HostError("Invalid local Host discovery")
        url = f"http://127.0.0.1:{endpoint['port']}/{operation}"
        request = urllib.request.Request(url, method="POST", data=json.dumps(body or {}, ensure_ascii=False).encode(),
            headers={"Authorization": "Bearer " + endpoint["token"], "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                detail = json.load(error).get("error", "Host rejected the operation")
            except ValueError:
                detail = "Host rejected the operation"
            raise HostError(detail) from None
        except (OSError, ValueError):
            raise HostError("DSH Host response unavailable", uncertain=operation in {"prompt", "approval/decide", "create"}) from None


def install_host(settings, profile="desktop"):
    if not profile or any(x in profile for x in ("/", "\\", "..")):
        raise ValueError("Invalid DSH profile name")
    directory = settings.dsh_home / "profiles" / profile
    if not directory.is_dir():
        raise ValueError("Initialize the DSH profile before installing its Host plugin")
    packaged = Path(__file__).parent / "host" / "index.mjs"
    if not packaged.exists():
        packaged = Path(__file__).resolve().parents[2] / "harness-plugin/index.mjs"
    assets = sorted(p for p in packaged.parent.iterdir() if p.is_file())
    revision = hashlib.sha256(b"".join(p.read_bytes() for p in assets)).hexdigest()[:12]
    # A content-addressed filename avoids the Host's canonical-module cache;
    # installed plugins also survive moving the original source checkout.
    installed = settings.dsh_home / "dsh2github" / ("plugin-" + revision)
    installed.mkdir(parents=True, exist_ok=True)
    for source in assets:
        (installed / source.name).write_bytes(source.read_bytes())
    desired_url = (installed / "index.mjs").resolve().as_uri()
    entry_id = "dsh2github-host-" + revision
    patch = directory / "cordis.patch.yml"
    original = patch.read_text(encoding="utf-8-sig") if patch.exists() else "[]\n"
    if "id: dsh2github-host" in original:
        if "# Managed by dsh2github install-host" not in original:
            raise ValueError("An unmanaged Host plugin is already registered")
        if desired_url in original and "id: " + entry_id in original:
            return {"installed": True, "changed": False, "profile": profile}
    backup = settings.runtime_dir / "profile-backups" / (profile + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".yml")
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(original, encoding="utf-8")
    from .storage import write_if_changed
    if "id: dsh2github-host" in original:
        import re
        original, count = re.subn(r'id: dsh2github-host(?:-[a-f0-9]+)?\s*\n(\s*)name: "[^"]+"(?:\n\s{6}config:\n\s{8}profile: "[^"]+")?',
            lambda match: "id: " + entry_id + "\n" + match[1] + "name: " + json.dumps(desired_url)
                + "\n      config:\n        profile: " + json.dumps(profile), original)
        if count != 1:
            raise ValueError("Host plugin registration is ambiguous")
        write_if_changed(patch, original)
    else:
        if original.strip() == "[]":
            original = ""
        write_if_changed(patch, original.rstrip() + "\n\n# Managed by dsh2github install-host\n- insert:\n"
            "    - id: " + entry_id + "\n      name: " + json.dumps(desired_url)
                + "\n      config:\n        profile: " + json.dumps(profile) + "\n")
    return {"installed": True, "changed": True, "profile": profile, "backup": str(backup)}

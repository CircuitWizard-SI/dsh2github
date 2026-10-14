"""Publish explicitly referenced deliverables into the private task repository."""
import base64
import hashlib
from pathlib import Path
import re
from urllib.parse import quote, unquote
from .github import api

DELIVERABLES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".pdf", ".docx", ".xlsx", ".pptx",
    ".txt", ".md", ".csv", ".html", ".zip", ".dwg", ".dxf", ".step", ".stp"}
LINK = re.compile(r"(!?\[[^\]\n]*\])\(([^\n]+?)\)")


class Artifacts:
    def __init__(self, settings, github):
        self.settings, self.github = settings, github
        self.cache = {}

    def rewrite(self, text, workspace):
        roots = [self.settings.workspace.resolve()]
        if workspace:
            roots.append(Path(workspace).resolve())
        def replace(match):
            target = match[2].strip()
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1]
            if re.match(r"(?i)https?://", target):
                return match[0]
            target = unquote(target.removeprefix("file:///"))
            path = Path(target)
            if not path.is_absolute():
                return match[0]
            resolved = path.resolve()
            if not any(resolved.is_relative_to(root) for root in roots):
                return match[0]
            if resolved.suffix.lower() not in DELIVERABLES or not resolved.is_file():
                return match[0]
            if resolved.stat().st_size > 10 * 1024 * 1024:
                return match[1].lstrip("!") + "（文件超过 10 MB，保留在本机）"
            raw = resolved.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            safe_name = re.sub(r"[^\w.\-]+", "_", resolved.name)
            name = f"artifacts/{digest[:24]}/{safe_name}"
            if name not in self.cache:
                self.github.validate()
                endpoint = self.github.prefix + "/contents/" + quote(name, safe="/")
                blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
                # Contents PUT is reconciled against the immutable blob name.
                existing = self.github.artifact_blob(name)
                if existing is not None and existing != blob:
                    raise RuntimeError("Artifact path contains different bytes")
                if existing is None:
                    api(endpoint, "PUT", {"message": "Publish referenced DSH task deliverable",
                        "content": base64.b64encode(raw).decode()})
                self.cache[name] = f"https://github.com/{self.settings.repo}/blob/main/{quote(name, safe='/')}"
            url = self.cache[name]
            return match[1] + "(" + url + ("?raw=true" if match[1].startswith("!") else "") + ")"
        return LINK.sub(replace, text)

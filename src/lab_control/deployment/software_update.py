"""Signed Graphite releases distributed by the public Tencent CNB repository."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from lab_control import __version__

VERSION = __version__
REPOSITORY = "https://cnb.cool/mrenderman-sciagent/LabControl"
FEED = REPOSITORY + "/-/git/raw/main/updates/graphite/windows-x64.json"
PUBLIC_KEY = "66a283a85cfe6f71b35b598384f174b2b93cf8065285a4031bc9814e3282fedc"
MAX_BYTES = 512 * 1024 * 1024


def version_key(version):
    match = re.fullmatch(r"([0-9]{1,5})\.([0-9]{1,5})\.([0-9]{1,5})(?:-graphite\.([0-9]{1,6}))?", version)
    if not match:
        raise ValueError("更新版本号无效")
    major, minor, patch, legacy_revision = match.groups()
    return int(major), int(minor), int(patch), legacy_revision is None, int(legacy_revision or 0)


@dataclass(frozen=True)
class Release:
    version: str
    notes: str
    size: int
    sha256: str

    @property
    def filename(self):
        suffix = "-windows-x64" if "-graphite." in self.version else ""
        return f"LabControl-{self.version}{suffix}-setup.exe"

    @property
    def url(self):
        return f"{REPOSITORY}/-/releases/download/v{self.version}/{self.filename}"


def manifest_message(payload):
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def verify_manifest(content, public_key=None):
    envelope = json.loads(content)
    try:
        payload = envelope["release"]
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key or PUBLIC_KEY))
        key.verify(base64.b64decode(envelope["signature"], validate=True), manifest_message(payload))
        if payload["schema"] != 1 or payload["target"] != "windows-x64":
            raise ValueError("更新清单与当前平台不兼容")
        release = Release(payload["version"], payload["notes"], payload["bytes"], payload["sha256"])
        version_key(release.version)
        if (
            type(release.size) is not int
            or not 0 < release.size <= MAX_BYTES
            or not isinstance(release.notes, str)
            or not re.fullmatch("[0-9a-f]{64}", release.sha256)
        ):
            raise ValueError("更新清单的安装包信息无效")
        return release
    except (KeyError, TypeError, InvalidSignature) as exc:
        raise ValueError("更新清单签名或内容无效") from exc


class SecureRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise ValueError("更新下载要求 HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(url):
    return build_opener(ProxyHandler({}), SecureRedirect()).open(
        Request(url, headers={"User-Agent": "LabControl-Updater", "Cache-Control": "no-cache"}), timeout=20
    )


def check_release():
    try:
        with open_url(FEED) as response:
            content = response.read(65537)
    except HTTPError as exc:
        if exc.code == 404:
            raise ValueError("腾讯石墨版频道尚未发布更新清单") from exc
        raise
    if len(content) > 65536:
        raise ValueError("更新清单过大")
    return verify_manifest(content)


def verify_file(path, release):
    if path.stat().st_size != release.size:
        raise ValueError("安装包长度与更新清单不一致")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != release.sha256:
        raise ValueError("安装包 SHA-256 校验失败")


def download_release(release, folder, progress, cancelled):
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / release.filename
    if target.is_file():
        try:
            verify_file(target, release)
            return target
        except ValueError:
            pass
    with tempfile.NamedTemporaryFile(dir=folder, suffix=".part", delete=False) as out:
        partial = Path(out.name)
    try:
        received = 0
        with open_url(release.url) as response, partial.open("wb") as output:
            while chunk := response.read(256 * 1024):
                if cancelled.is_set():
                    raise ValueError("下载已取消")
                received += len(chunk)
                if received > release.size:
                    raise ValueError("下载内容超过清单大小")
                output.write(chunk)
                progress(received * 100 // release.size)
        verify_file(partial, release)
        os.replace(partial, target)
        return target
    finally:
        partial.unlink(missing_ok=True)

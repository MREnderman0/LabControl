import base64
import hashlib
import io
import json
from dataclasses import replace
from threading import Event, Lock
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lab_control.deployment import software_update as sw


def test_signature_architecture_and_numeric_version_order():
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw().hex()
    data = dict(
        version="0.3.0-graphite.10",
        target="windows-x64",
        schema=1,
        bytes=123,
        sha256="c" * 64,
        notes="中文版本说明",
    )

    def signed():
        signature = base64.b64encode(key.sign(sw.manifest_message(data))).decode("ascii")
        return json.dumps({"release": data, "signature": signature})

    good = signed()
    release = sw.verify_manifest(good, public)
    assert sw.version_key(release.version) > sw.version_key("0.3.0-graphite.9")
    assert release.url.startswith(sw.REPOSITORY + "/-/releases/download/v" + release.version + "/")
    with pytest.raises(ValueError):
        sw.verify_manifest(good.replace("123", "999"), public)
    data["target"] = "windows-arm64"
    with pytest.raises(ValueError):
        sw.verify_manifest(signed(), public)


def test_numbered_versions_upgrade_legacy_and_sort_numerically():
    ordered = ["0.3.0-graphite.1", "0.3.0-graphite.10", "0.3.0", "0.3.1", "0.3.10", "1.0.0"]
    assert sorted(reversed(ordered), key=sw.version_key) == ordered
    release = sw.Release("0.3.1", "", 1, "")
    assert release.filename == "LabControl-0.3.1-setup.exe"
    assert release.url.endswith("/v0.3.1/LabControl-0.3.1-setup.exe")


def test_incomplete_corrupt_cancelled_and_retried_download(tmp_path, monkeypatch):
    contents = b"sample setup" * 100
    release = sw.Release(sw.VERSION, "", len(contents), hashlib.sha256(contents).hexdigest())
    cancel = Event()
    for body in (contents[:-1], contents + b"!", b"?" * len(contents)):
        monkeypatch.setattr(sw, "open_url", lambda _, body=body: io.BytesIO(body))
        with pytest.raises(ValueError):
            sw.download_release(release, tmp_path, lambda _: None, cancel)
        assert not list(tmp_path.iterdir())
    monkeypatch.setattr(sw, "open_url", lambda _: io.BytesIO(contents))
    cancel.set()
    with pytest.raises(ValueError):
        sw.download_release(release, tmp_path, lambda _: None, cancel)
    cancel.clear()
    progress = []
    target = sw.download_release(release, tmp_path, progress.append, cancel)
    assert target.read_bytes() == contents and progress[-1] == 100
    with pytest.raises(ValueError):
        sw.verify_file(target, replace(release, sha256="a" * 64))


def test_watch_recording_blocks_install_until_csv_finishes():
    from lab_control.ui.pages.software_update import busy_reason

    run = SimpleNamespace(accepting=True, done=Event())
    runtime = SimpleNamespace(
        monitoring=SimpleNamespace(snapshots=dict, lock=Lock(), recordings=[]),
        _processing=None,
        discovery=SimpleNamespace(is_scanning=False),
        sessions=SimpleNamespace(snapshots=dict),
    )
    assert "监视列表" in busy_reason(runtime, [run])
    run.accepting = False
    assert "监视列表" in busy_reason(runtime, [run])
    run.done.set()
    assert busy_reason(runtime, [run]) == ""

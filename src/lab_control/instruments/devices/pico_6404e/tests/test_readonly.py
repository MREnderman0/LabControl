import ctypes as ct
import json

import pytest

from lab_control.instruments.devices.pico_6404e import adapter
from lab_control.instruments.devices.pico_6404e.hardware import readonly
from lab_control.instruments.devices.pico_6404e.tests.test_sdk import FakeLibrary, pointer_write
from lab_control.transport.sdk import VendorLibrary


@pytest.mark.parametrize("wrong_identity", [False, True])
def test_readonly_uses_real_session_contract_with_offline_sdk(tmp_path, monkeypatch, wrong_identity):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("LAB_CONTROL_HOME", str(tmp_path / "TF-LabControl"))
    monkeypatch.setattr(readonly, "find_sdk", lambda kind: "offline-library")
    calls = []

    def enumerate_units(count, serials, length):
        pointer_write(count, ct.c_int16, 1)
        serials.value = b"TEST/0001"
        return 0

    def info(handle, buffer, length, required, key):
        buffer.value = {3: b"6404E", 4: b"OTHER" if wrong_identity else b"TEST/0001", 9: b"1.0"}[key]
        return 0

    fake = FakeLibrary(
        {
            "ps6000aEnumerateUnits": enumerate_units,
            "ps6000aOpenUnit": lambda *args: calls.append("open") or 0,
            "ps6000aGetUnitInfo": info,
            "ps6000aCloseUnit": lambda handle: calls.append("close") or 0,
        }
    )
    monkeypatch.setattr(adapter, "VendorLibrary", lambda path, library=None: VendorLibrary(path, fake))
    folder, passed = readonly.run("TEST/0001")
    result = json.loads((folder / "results.json").read_text("utf-8"))
    summary = json.loads((folder / "summary.json").read_text("utf-8"))
    transcript = [
        json.loads(line) for line in (folder / "communication.jsonl").read_text("utf-8").splitlines()
    ]
    assert result["stages"]["acquisition"] == "not_tested"
    assert summary["interface"] == "USB"
    assert summary["protocol"] == "PicoSDK ps6000a"
    assert transcript[0]["resources"] == ["PICO::TEST/0001"]
    assert passed == (not wrong_identity)
    if wrong_identity:
        assert result["error_code"] == "identity_conflict"
        assert not summary["identity_verified"]
        assert calls == ["open", "close"]
    else:
        assert summary["identity_verified"]
        assert result["identity"]["serial"] == "TEST/0001"
        assert calls == ["open", "close", "open", "close"]
        expected = ["ps6000aOpenUnit", *(["ps6000aGetUnitInfo"] * 3), "ps6000aCloseUnit"]
        assert [e["call"] for e in transcript[1:]] == expected * 2

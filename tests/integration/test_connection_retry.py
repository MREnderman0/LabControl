import threading
import time
from concurrent.futures import Future
from dataclasses import replace

import pytest
from pyvisa import constants, errors

from lab_control.application import connection_retry
from lab_control.application.runtime import ApplicationRuntime
from lab_control.application.settings_sync import SyncTask
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.discovery.types import DiscoveryCandidate
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import DeviceError, ParameterReading
from lab_control.transport.visa import VisaRuntime


def wait(check):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.005)
    assert check()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(
        connection_retry, "retry_delay", lambda attempts, **kw: 0.04 if attempts <= 1 else 0.08
    )
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        yield runtime


def asset(app, name="scope"):
    cfg = ConnectionConfig("mock://" + name, "rs.rtm3004", "mock", expected_serial=name)
    identity = SimulatedInstrument(cfg).identify()
    return app.inventory.merge(DiscoveryCandidate(cfg, "configured"), "scan", identity, cfg.adapter)["id"]


@pytest.mark.parametrize("confidence", ["exact", "usb_descriptor"])
def test_idle_firmware_is_retained_across_connection_and_recovery(app, confidence):
    key = asset(app)
    record = next(row for row in app.inventory.all() if row["id"] == key)
    previous = record["identity"]["firmware"]
    assert previous
    firmware = ""

    def factory(config):
        adapter = SimulatedInstrument(config)
        reported = adapter.identify()
        adapter.optional_firmware_evidence = confidence
        adapter.identify = lambda: replace(reported, firmware=firmware, confidence=confidence)
        return adapter

    app.sessions.factory = factory
    idle = app.connections.request(key).result(4)
    assert idle.firmware == "" and idle.last_known_firmware == previous
    assert app.sessions.reconnect(key, allow_connected=True).result(4) == idle
    assert app.repository.records()[0]["identity"]["last_known_firmware"] == previous
    firmware = "different"
    with pytest.raises(DeviceError) as error:
        app.sessions.reconnect(key, allow_connected=True).result(4)
    assert error.value.code == "qualification_stale"


@pytest.mark.parametrize(
    "changes",
    [
        {"serial": "another"},
        {"serial": ""},
        {"model": "other"},
        {"manufacturer": "other"},
        {"confidence": "model_only"},
        {"confidence": "manual_protocol"},
        {"confidence": "device_reported"},
    ],
)
def test_missing_firmware_cannot_borrow_an_unverified_baseline(changes):
    from lab_control.domain.instruments import DeviceIdentity

    previous = DeviceIdentity("Maker", "Model", "S123", "1.2")
    observed = replace(previous, firmware="", **changes)
    assert not observed.with_firmware_history(previous).firmware_reference


def test_missing_firmware_stays_blocked_for_an_adapter_without_partial_identity_support(app):
    key = asset(app)

    def factory(config):
        adapter = SimulatedInstrument(config)
        identity = replace(adapter.identify(), firmware="")
        adapter.identify = lambda: identity
        return adapter

    app.sessions.factory = factory
    with pytest.raises(DeviceError) as error:
        app.connections.request(key).result(4)
    assert error.value.code == "qualification_stale"


def test_absent_serial_waits_for_exact_enumeration_and_shares_open(app, monkeypatch):
    key = asset(app)
    calls = []
    present, opening, release = (threading.Event() for _ in range(3))

    def listed(config):
        calls.append(("list", config.expected_serial))
        if not present.is_set():
            raise DeviceError("not here", "device_not_present")

    def factory(config):
        calls.append(("open", config.expected_serial))
        if not present.is_set():
            raise DeviceError("status 0x3", "device_not_present")
        opening.set()
        assert release.wait(4)
        return SimulatedInstrument(config)

    app.sessions.factory = factory
    monkeypatch.setattr(connection_retry, "require_present", listed)
    try:
        batch = app.connect_all_discovered().result(3)
        assert batch["items"][0]["status"] == "waiting"
        wait(lambda: calls.count(("list", "scope")) >= 2)
        assert calls.count(("open", "scope")) == 1
        assert app.connections.snapshots()[key]["phase"] == "waiting"
        present.set()
        assert opening.wait(3)
        one = app.connections.request(key)
        assert app.connections.request(key, reconnect=True) is one
        assert app.connections.request(key, hotplug=True) is one
        release.set()
        assert one.result(3).serial == "scope"
        assert calls.count(("open", "scope")) == 2
        assert "重试" in app.connections.snapshots()[key]["lastRecovery"]
    finally:
        release.set()


@pytest.mark.parametrize(
    "closed_error", [errors.InvalidSession(), errors.VisaIOError(constants.StatusCode.error_invalid_object)]
)
def test_cleanup_retries_same_visa_handle_before_reopen(app, closed_error):
    key = asset(app)
    backend = VisaRuntime()
    calls = []
    close_error = [errors.VisaIOError(constants.StatusCode.error_io), closed_error]

    class OldHandle:
        def close(self):
            calls.append("close")
            raise close_error.pop(0)

    handle = OldHandle()
    backend._handles.add(handle)

    def factory(config):
        calls.append("open")
        adapter = SimulatedInstrument(config)
        if calls == ["open"]:
            adapter.close = lambda: backend.release(handle)
        else:
            assert handle not in backend._handles
        return adapter

    app.sessions.factory = factory
    app.connections.request(key).result(3)
    state = app.sessions.sessions[key]
    app.connections.settings_failed(key, state.session_id, DeviceError("link lost", "disconnected"))
    wait(lambda: calls.count("open") == 2 and app.connections.snapshots()[key]["phase"] == "online")
    assert calls == ["open", "close", "close", "open"]
    assert app.sessions.sessions[key] is state


def test_explicit_disconnect_retries_only_cleanup(app):
    key = asset(app)
    attempts, opens = [], []
    ready = threading.Event()

    def factory(cfg):
        opens.append(cfg)
        adapter = SimulatedInstrument(cfg)

        def close():
            attempts.append(1)
            if not ready.is_set():
                raise DeviceError("close failed", "disconnected")

        adapter.close = close
        return adapter

    app.sessions.factory = factory
    app.connections.request(key).result(3)
    with pytest.raises(DeviceError):
        app.connections.disconnect(key).result(3)
    try:
        wait(lambda: len(attempts) >= 3)
        assert app.leases.owns_resources(key)
        assert app.connections.snapshots()[key]["phase"] == "cleanup"
        with pytest.raises(DeviceError):
            app.connections.request(key, hotplug=True)
    finally:
        ready.set()
    wait(lambda: key not in app.sessions.sessions and key not in app.connections.snapshots())
    assert len(opens) == 1 and not app.leases.owns_resources(key)


def test_disconnect_during_open_and_late_completion(app):
    key = asset(app)
    entered, release = threading.Event(), threading.Event()
    opens = []

    def factory(cfg):
        opens.append(cfg)
        entered.set()
        assert release.wait(3)
        return SimulatedInstrument(cfg)

    app.sessions.factory = factory
    opening = app.connections.request(key)
    assert entered.wait(3)
    closing = app.connections.disconnect(key)
    release.set()
    with pytest.raises(DeviceError):
        opening.result(3)
    closing.result(3)
    assert key not in app.sessions.sessions and len(opens) == 1
    with pytest.raises(DeviceError, match="已手动断开"):
        app.connections.request(key, hotplug=True)


@pytest.mark.parametrize("failure", ["identity", "prepare"])
def test_terminal_error_never_replays_initialization(app, failure):
    key = asset(app)
    calls = []

    def factory(cfg):
        calls.append("open")
        ad = SimulatedInstrument(cfg)
        if failure == "identity":
            identity = replace(ad.identify(), serial="other")
            ad.identify = lambda: identity
        else:

            def prepare():
                calls.append("write")
                raise DeviceError("uncertain setup", "timeout")

            ad.prepare_acquisition = prepare
        return ad

    app.sessions.factory = factory
    with pytest.raises(DeviceError):
        app.connections.request(key).result(3)
    assert app.connections.snapshots()[key]["phase"] == "blocked"
    app.connect_all_discovered().result(3)
    assert calls == (["open"] if failure == "identity" else ["open", "write"])


@pytest.mark.parametrize("primary_code, reopen", [("timeout", True), ("identity_conflict", False)])
def test_partial_initial_open_drains_handle_before_any_retry(app, primary_code, reopen):
    from lab_control.domain.instruments import ResourceCleanupError

    key = asset(app)
    allow_close = threading.Event()
    calls = []

    class Partial:
        def close(self):
            calls.append("close")
            if not allow_close.is_set():
                raise OSError("I/O pending")

    def factory(cfg):
        calls.append("open")
        if calls == ["open"]:
            raise ResourceCleanupError(
                Partial(), OSError("close"), DeviceError("initial failure", primary_code)
            )
        assert allow_close.is_set()
        return SimulatedInstrument(cfg)

    app.sessions.factory = factory
    with pytest.raises(ResourceCleanupError):
        app.connections.request(key).result(3)
    try:
        wait(lambda: calls.count("close") >= 2)
        assert calls.count("open") == 1
        assert app.leases.owns_resources(key)
    finally:
        allow_close.set()
    wait(lambda: app.connections.snapshots()[key]["phase"] == ("online" if reopen else "blocked"))
    assert calls.count("open") == (2 if reopen else 1)


def test_settings_use_typed_consecutive_transport_failures(app):
    key = asset(app)
    app.connections.request(key).result(3)
    sync = app.monitoring.settings_sync
    session = app.sessions.sessions[key]
    task = sync.tasks[key] = SyncTask(session.session_id, keys=((1, "trigger_source"),))
    sync.enabled = True
    sync.max_concurrent = 0
    seen = []
    sync.on_disconnect = lambda key, sid, err: seen.append((sid, err.code))

    def poll(value):
        task.pending = Future()
        if isinstance(value, Exception):
            task.pending.set_exception(value)
        else:
            task.pending.set_result(value)
        sync.tick({key: app.sessions.snapshots()[0]})

    timeout = DeviceError("timeout", "timeout")
    poll(timeout)
    poll((ParameterReading("trigger_source", 1, None, "instrument"),))
    poll(timeout)
    assert not seen
    poll(timeout)
    poll(DeviceError("not supported", "unsupported"))
    poll(DeviceError("invalid object", "disconnected"))
    assert seen == [(session.session_id, "timeout"), (session.session_id, "disconnected")]
    sync.enabled = False


def test_stop_batch_cancels_only_new_waits(app, monkeypatch):
    existing, offline, slow = [asset(app, name) for name in ("existing", "offline", "slow")]
    entered, release = threading.Event(), threading.Event()
    attempts = []
    monkeypatch.setattr(connection_retry, "retry_delay", lambda *_args, **_kwargs: 1.0)

    def factory(cfg):
        name = cfg.expected_serial
        attempts.append(name)
        if name == "slow":
            entered.set()
            assert release.wait(4)
        else:
            raise DeviceError("offline", "timeout")
        return SimulatedInstrument(cfg)

    app.sessions.factory = factory
    with pytest.raises(DeviceError):
        app.connections.request(existing).result(2)
    batch = app.connect_all_discovered()
    try:
        assert entered.wait(3)
        wait(lambda: app.connections.snapshots().get(offline, {}).get("phase") == "retrying")
        app.cancel_connect_all()
    finally:
        release.set()
    batch.result(3)
    wait(lambda: offline not in app.connections.snapshots())
    assert existing in app.connections.snapshots() and attempts.count("offline") == 1
    assert app.sessions.sessions[slow].identity.serial == "slow"


def test_recording_owns_recovery_and_bulk_never_opens_again(app):
    from lab_control.domain.instruments import AcquisitionRequest

    key = asset(app)
    calls = []

    def build(config):
        calls.append(config)
        return SimulatedInstrument(config)

    app.sessions.factory = build
    app.connections.request(key).result(3)
    session = app.sessions.sessions[key]
    request = AcquisitionRequest("waveform", (1,), points=32)
    app.monitoring.start(key, (request,), interval=0.5, recording=True)
    try:
        wait(lambda: app.monitoring.latest(key, "waveform") is not None)
        app.connections.settings_failed(key, session.session_id, DeviceError("old read", "disconnected"))
        assert app.connect_all_discovered().result(3)["items"][0]["status"] == "already_connected"
        assert len(calls) == 1 and session.monitoring and session.recording
    finally:
        app.monitoring.stop(key).result(5)


def test_pool_shows_wait_and_cancels_it(qtbot, app, monkeypatch):
    from lab_control.ui.real_window import RealWindow

    key = asset(app)

    def absent(*_):
        raise DeviceError("status 0x3", "device_not_present")

    app.sessions.factory = absent
    monkeypatch.setattr(connection_retry, "require_present", absent)
    window = RealWindow(app)
    qtbot.addWidget(window)
    window.timer.stop()
    window.controller.close()
    try:
        with pytest.raises(DeviceError):
            app.connections.request(key).result(2)
        window.open_category("oscilloscope")
        window.tree.setCurrentItem(window.rows[key])
        window.refresh()
        assert "等待设备接入" in window.rows[key].text(3)
        assert "重试" in window.details.values["error"].text()
        button = window.action_buttons["disconnect"]
        assert button.isEnabled()
        button.click()
        qtbot.waitUntil(lambda: key not in app.connections.snapshots())
        assert key not in app.sessions.sessions
    finally:
        window.closing = True
        window.bridge.worker.shutdown(wait=True)

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Event
from time import monotonic, sleep

import numpy as np
import pytest
from PySide6.QtGui import QImage

from lab_control.application.agents import frame_image
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceError
from lab_control.domain.workspace import GENERAL
from lab_control.ui.pages.agent_collaboration import AgentCollaborationPanel


def wait(service, scope, task):
    end = monotonic() + 8
    while monotonic() < end:
        status = service.get_task_status(*scope[:2], task["id"])
        if status["state"] in {"completed", "failed"}:
            return status
        sleep(0.01)
    pytest.fail(str(status))


@pytest.fixture
def experiment(tmp_path, monkeypatch, qapp):
    monkeypatch.setattr(frame_image, "SHARE_ROOT", tmp_path)
    with ApplicationRuntime(root=tmp_path / "tf", hardware=False) as runtime:
        yield runtime


def connect(runtime, model):
    config = ConnectionConfig("mock://image/" + model, model, "mock")
    device = runtime.add(config)
    runtime.connect(device).result(5)
    return device


def capture(service, scope, channel, key):
    preview = service.preview_change(*scope, frame_image.OPERATION, {}, channel)
    return service.apply_change(*scope, preview["id"], key)


@pytest.mark.parametrize(
    ("model", "channel", "label"),
    [("rs.rto64", 2, "RTO64-CH2-波形"), ("rigol.dsa815", 1, "DSA815-TRACE1-频谱")],
)
def test_ofc_frame_image(experiment, model, channel, label, monkeypatch):
    from lab_control.ui.widgets import publication_figure

    runtime = experiment
    device = connect(runtime, model)
    slot = ""
    if model == "rs.rto64":
        slot = "拍频监测"
        runtime.repository.function_slots.save("oscilloscope", GENERAL, {"oscilloscope:test": slot})
        runtime.inventory.bind_slot(device, "oscilloscope:test", GENERAL)
    agent = runtime.agent
    scope = ("internal", agent.grant_all_devices()[0], device)
    caps = agent.get_capabilities(*scope)["snapshot"]["channels"][str(channel)]
    action = next(op for op in caps["operations"] if op["name"] == frame_image.OPERATION)
    assert action["writable"] and action["channel_scoped"] and action["parameters"] == {}
    adapter = runtime.sessions.sessions[device].adapter
    acquire = adapter.acquire
    frames = []
    stamps = [datetime(2026, 11, 14, 23, 59, 59, 99000).astimezone()] * 3
    stamps.append(datetime(2026, 11, 15, 0, 0, 0, 3000).astimezone())

    def read(request):
        assert request.channels == (channel,)
        batch = replace(acquire(request), timestamp=stamps[len(frames)].isoformat())
        frames.append(batch.measurements[0])
        return batch

    # SessionOperations stamps the read completion with the acquisition computer's clock.
    monkeypatch.setattr(
        "lab_control.application.session_operations.utc_now",
        lambda: stamps[max(len(frames) - 1, 0)].isoformat(),
    )

    export = publication_figure.export_figure

    def plot(path, curves, x_label, y_label, **options):
        m = frames[-1]
        assert m.axis_unit in x_label and m.unit in y_label
        np.testing.assert_array_equal(curves[0][1], m.axis)
        np.testing.assert_array_equal(curves[0][2], m.values)
        export(path, curves, x_label, y_label, **options)

    monkeypatch.setattr(adapter, "acquire", read)
    monkeypatch.setattr(publication_figure, "export_figure", plot)
    for i, stamp in enumerate(stamps):
        preview = agent.preview_change(*scope, frame_image.OPERATION, {}, channel)
        task = agent.apply_change(*scope, preview["id"], f"capture-{i}")
        status = wait(agent, scope, task)
        assert status["state"] == "completed", status
        assert status["result"]["operation"] == frame_image.OPERATION
        path = Path(status["result"]["path"])
        assert path.parent == frame_image.SHARE_ROOT / "单帧图像" / stamp.date().isoformat()
        assert path.name == (
            stamp.strftime("%H-%M-%S-%f")[:-3]
            + "-"
            + (slot + "-" if slot else "")
            + label
            + (f"-{i + 1:02d}" if i in (1, 2) else "")
            + ".png"
        )
        assert QImage(str(path)).width() > 500
        assert capture(agent, scope, channel, f"capture-{i}")["id"] == task["id"]
        del agent.previews[preview["id"]]
        assert agent.apply_change(*scope, preview["id"], f"capture-{i}")["id"] == task["id"]
        assert len(frames) == i + 1


def test_failed_capture_and_timeout_never_publish(experiment, monkeypatch, tmp_path):
    agent = experiment.agent
    device = connect(experiment, "rs.rto64")
    scope = ("internal", agent.grant_all_devices()[0], device)
    monkeypatch.setattr(frame_image, "SHARE_ROOT", tmp_path / "missing-share")
    failed = wait(agent, scope, capture(agent, scope, 1, "missing"))
    assert failed["code"] == "image_write_failed" and "共享目录" in failed["error"]
    session = experiment.sessions.sessions[device]
    with monkeypatch.context() as patch:

        def fail_read(request):
            raise DeviceError("CH1 is off", "waveform_unavailable")

        patch.setattr(session.adapter, "acquire", fail_read)
        failed = wait(agent, scope, capture(agent, scope, 1, "bad-frame"))
        assert failed["code"] == "waveform_unavailable" and "CH1" in failed["error"]
    monkeypatch.setattr(frame_image, "SHARE_ROOT", tmp_path)
    monkeypatch.setattr(frame_image, "TIME_BUDGET", 0.15)
    blocker = Event()
    session.worker.submit(blocker.wait, 3)
    try:
        failed = wait(agent, scope, capture(agent, scope, 1, "timeout"))
        assert failed["code"] == "capture_timeout"
    finally:
        blocker.set()
    session.worker.submit(lambda: None).result(2)
    assert not list(tmp_path.rglob("*.png"))


def test_share_write_denied_and_render_exceeding_deadline(experiment, monkeypatch, tmp_path):
    from lab_control.ui.widgets import publication_figure

    agent = experiment.agent
    device = connect(experiment, "rigol.dsa815")
    scope = ("internal", agent.grant_all_devices()[0], device)

    def denied(*args, **kwargs):
        raise PermissionError("Access denied")

    monkeypatch.setattr(publication_figure, "export_figure", denied)
    status = wait(agent, scope, capture(agent, scope, 1, "denied"))
    assert status["code"] == "image_write_failed" and "Access denied" in status["error"]
    blocked, release = Event(), Event()

    def stalled(path, *args, **kwargs):
        path.write_bytes(b"incomplete")
        blocked.set()
        release.wait(4)

    monkeypatch.setattr(publication_figure, "export_figure", stalled)
    monkeypatch.setattr(frame_image, "TIME_BUDGET", 0.8)
    task = capture(agent, scope, 1, "render-timeout")
    try:
        assert blocked.wait(2)
        assert wait(agent, scope, task)["code"] == "capture_timeout"
    finally:
        release.set()
    agent.worker.shutdown(wait=True)
    assert not list(tmp_path.rglob("*.png"))


def test_desktop_auto_grant_new_conflicting_serialless_revoke_restart(tmp_path, qtbot):
    def desktop(runtime):
        panel = AgentCollaborationPanel(runtime)
        qtbot.addWidget(panel)
        panel.timer.stop()
        return panel

    def grants(runtime):
        return runtime.agent.get_server_info("internal")["grants"]

    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        device = connect(runtime, "rs.rto64")
        panel = desktop(runtime)
        grant = grants(runtime)[0]
        assert list(grant["devices"]) == [device] and grant["desktop_automatic"]
        meter = connect(runtime, "joinwit.jw8105")
        session = runtime.sessions.sessions[meter]
        session.identity = replace(session.identity, serial="")
        updated = grants(runtime)[0]
        assert updated["id"] == grant["id"] and not updated["devices"][meter]["serial"]
        assert updated["devices"][meter]["resource"] == session.config.resource
        runtime.inventory.records[meter]["identityConflict"] = True
        assert meter not in grants(runtime)[0]["devices"]
        runtime.inventory.records[meter]["identityConflict"] = False
        assert meter in grants(runtime)[0]["devices"]
        runtime.agent.policy.revoke_grant(grant["id"])
        connect(runtime, "rigol.dsa815")
        assert grants(runtime) == []
        panel.shutdown()
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        panel = desktop(runtime)
        refreshed = grants(runtime)[0]
        assert refreshed["id"] != grant["id"] and len(refreshed["devices"]) == 3
        panel.shutdown()

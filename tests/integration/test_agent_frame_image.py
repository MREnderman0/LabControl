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


@pytest.mark.parametrize(
    ("model", "source", "mode"),
    [
        ("rs.rto64", "waveform", "both"),
        ("rigol.dsa815", "spectrum", "png"),
        ("pangu.kt616r", "reading", "csv"),
    ],
)
def test_named_frame(experiment, tmp_path, monkeypatch, model, source, mode):
    import csv
    import hashlib

    from lab_control.ui.widgets import publication_figure

    agent = experiment.agent
    device = connect(experiment, model)
    grant = agent.grant_all_devices()[0]
    scope = ("internal", grant, device)
    folder = tmp_path / "试验 A"
    folder.mkdir()
    (folder / "无关.txt").write_bytes(b"private")
    adapter = experiment.sessions.sessions[device].adapter
    acquire = adapter.acquire
    reads = []

    def read(request):
        batch = acquire(request)
        reads.append(batch)
        return batch

    monkeypatch.setattr(adapter, "acquire", read)
    plotted = []
    render = publication_figure.export_figure

    def plot(path, curves, *args, **kwargs):
        plotted.extend(curves)
        return render(path, curves, *args, **kwargs)

    monkeypatch.setattr(publication_figure, "export_figure", plot)

    def call(key, dest="labcontrol", **override):
        options = dict(
            source=source,
            channel=1,
            mode=mode,
            destination=dest,
            stem="工况 A",
            request_id=key,
            directory=str(folder),
        )
        return agent.save_frame(*scope, **(options | override))

    task = call("first")
    status = wait(agent, scope, task)
    assert status["state"] == "completed", status
    result = status["result"]
    assert call("first")["id"] == task["id"] and len(reads) == 1
    files = {f["format"]: Path(f["path"]) for f in result["files"]}
    assert set(files) == ({"png", "csv"} if mode == "both" else {mode})
    for info in result["files"]:
        path = files[info["format"]]
        assert path == folder / ("工况 A." + info["format"])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == info["sha256"]
        assert (
            agent.artifact_path("internal", grant, info["artifact_id"])[1].read_bytes() == path.read_bytes()
        )
    assert len(agent.list_artifacts("internal", grant, task["id"])) == len(files)
    if "csv" in files:
        with files["csv"].open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == result["points"] and {r["timestamp"] for r in rows} == {result["timestamp"]}
        assert all(row["unit"] and row["channel"] == "1" for row in rows)
        if mode == "both":
            np.testing.assert_allclose([float(r["real"]) for r in rows], plotted[0][2])
            np.testing.assert_allclose([float(r["axis"]) for r in rows], plotted[0][1])
    if "png" in files:
        assert QImage(str(files["png"])).width() > 500
    again = wait(agent, scope, call("second"))["result"]
    assert all(Path(f["path"]).stem == "工况 A-02" for f in again["files"])
    staged = wait(agent, scope, call("client", "client", directory="X:/only-exists-on-client"))
    assert staged["state"] == "completed" and all("path" not in f for f in staged["result"]["files"])
    assert len(reads) == 3
    bad = wait(agent, scope, call("bad", directory=str(folder / "无关.txt")))
    assert bad["state"] == "failed" and len(reads) == 3
    if mode == "csv":
        assert wait(agent, scope, call("bad-mode", mode="both"))["state"] == "failed"
        assert len(reads) == 3


@pytest.mark.parametrize("failure", ["cancel", "revoke", "journal"])
def test_named_frame_aborted_set_keeps_existing_files(experiment, tmp_path, monkeypatch, failure):
    from lab_control.application.agents import single_frame

    agent = experiment.agent
    device = connect(experiment, "rigol.dsa815")
    grant = agent.grant_all_devices()[0]
    scope = ("internal", grant, device)
    folder = tmp_path / "实验"
    folder.mkdir()
    before = folder / "已有.csv"
    before.write_bytes(b"before")
    started, release = Event(), Event()
    render = single_frame.render

    def paused(*args):
        started.set()
        assert release.wait(6)
        return render(*args)

    monkeypatch.setattr(single_frame, "render", paused)
    if failure == "journal":

        def denied(_entries):
            raise OSError("test artifact journal failure")

        monkeypatch.setattr(agent.ledger, "save_artifacts", denied)
    task = agent.save_frame(*scope, "spectrum", 1, "both", "labcontrol", "已有", "abort", str(folder))
    try:
        assert started.wait(4)
        if failure == "cancel":
            agent.stop_task("internal", grant, task["id"], "abort-stop")
        if failure == "revoke":
            agent.policy.revoke_grant(grant)
    finally:
        release.set()
    end = monotonic() + 8
    while monotonic() < end:
        state = agent.ledger.get(task["id"])
        if state["state"] in {"failed", "cancelled"}:
            break
        sleep(0.03)
    assert state["state"] in {"failed", "cancelled"}, state
    assert list(folder.iterdir()) == [before] and before.read_bytes() == b"before"
    assert not agent.ledger.artifacts(task["id"])

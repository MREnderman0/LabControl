from contextlib import contextmanager
from threading import Event

import pytest
from PySide6.QtCore import QPoint, Qt, QTimer

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, DeviceError
from lab_control.instruments.scope_single import SingleCapture
from lab_control.ui.real_window import RealWindow


@pytest.mark.parametrize(
    "model",
    ["pico.6404e", "rs.rtm3004", "rs.rto64", "rigol.ds1202ze", "lecroy.9254", "lecroy.9404m", "rigol.dho924"],
)
def test_split_button_does_not_arm_until_selected(qtbot, tmp_path, model, monkeypatch):
    with ApplicationRuntime(root=tmp_path, hardware=False) as rt:
        device = rt.add(ConnectionConfig("mock://single/" + model, model, "mock"))
        rt.connect(device).result()
        adapter = rt.sessions.sessions[device].adapter
        original = adapter.single_acquisition
        armed = []

        @contextmanager
        def arm(job, request):
            armed.append(request.channels)
            with original(job, request) as result:
                yield result

        monkeypatch.setattr(adapter, "single_acquisition", arm)
        window = RealWindow(rt)
        qtbot.addWidget(window)
        try:
            window.show()
            window.navigate("实时监测", "oscilloscope", device)
            page = window.stack.currentWidget()
            qtbot.waitUntil(page.single_action.isEnabled, timeout=8000)
            button = page.start_button
            QTimer.singleShot(80, button.menu().close)
            qtbot.mouseClick(
                button, Qt.MouseButton.LeftButton, pos=QPoint(button.width() - 3, button.height() // 2)
            )
            assert not armed and not rt.monitoring.snapshots().get(device, {}).get("running")
            page.single_action.trigger()
            qtbot.waitUntil(lambda: page.plot.displayed_frame is not None, timeout=10000)
            assert len(armed) == 1
            assert page.plot.displayed_frame.batch.performance["single_completed"]
            assert button.text() == "开始监测"
        finally:
            window.timer.stop()
            window.controller.close()
            window.closing = True
            window.bridge.worker.shutdown()


def test_pending_single_cancels_without_publishing_or_reading_old_frame(tmp_path, monkeypatch):
    with ApplicationRuntime(root=tmp_path, hardware=False) as rt:
        device = rt.add(ConnectionConfig("mock://single/cancel", "rs.rtm3004", "mock"))
        rt.connect(device).result()
        session = rt.sessions.sessions[device]
        armed = Event()

        @contextmanager
        def no_trigger(job, request):
            armed.set()
            job.wait(lambda: False, 1, 1)
            yield 1, False

        monkeypatch.setattr(session.adapter, "single_acquisition", no_trigger)
        monkeypatch.setattr(session.adapter, "acquire", lambda *_: pytest.fail("Read a stale frame"))
        job = SingleCapture(AcquisitionRequest("waveform", (1,), confirmed=True))
        future = rt.sessions.submit_single(device, job)
        assert armed.wait(2)
        with pytest.raises(DeviceError, match="已有"):
            rt.sessions.submit_single(device, job)
        job.cancel.set()
        with pytest.raises(DeviceError, match="取消"):
            future.result(3)
        rt.sessions.set_monitoring(device, True, recording=True)
        with pytest.raises(DeviceError):
            rt.sessions.submit_single(device, job)
        rt.sessions.set_monitoring(device, False)


def test_automatic_rigol_record_length_preserves_protocol_value(qtbot):
    from lab_control.domain.instruments import Parameter
    from lab_control.ui.pages.monitoring.scope_points import ScopePoints

    points = ScopePoints()
    qtbot.addWidget(points)
    sent = []
    points.committed.connect(sent.append)
    points.configure("AUTO", Parameter("enum", choices=("AUTO", "12000", "120000")))
    assert points.currentText() == "自动" and not sent
    points.choose(points.findData("12000"))
    assert sent == ["12000"]

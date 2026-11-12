"""Exercise the production scan workspace, recorder and missing-input pause."""

import struct

from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest
from lab_control.storage.recording import read_frames
from lab_control.ui.real_window import RealWindow

from ..adapter import ADAPTER_ID, RecorderAdapter


class InputBanks:
    empty = False

    def inputs(self, station, offset, count):
        if offset == 100:
            return struct.pack(">16f", *([21.5, 23.75] + [-326.4] * 14))
        raw = [-32640] * 16
        if not self.empty:
            raw[:2] = [2150, 2375]
        return struct.pack(">16h", *raw)

    def close(self):
        pass


def test_shared_workspace_and_recording(qtbot, tmp_path):
    cfg = ConnectionConfig("mock://recorder", ADAPTER_ID, "mock", options=("float_order=ABCD", "unit=°C"))
    runtime = ApplicationRuntime(root=tmp_path, hardware=False)
    wire = InputBanks()
    runtime.sessions.factory = lambda config: RecorderAdapter(config, wire)
    device = runtime.add(cfg)
    runtime.connect(device).result(5)
    window = RealWindow(runtime)
    qtbot.addWidget(window)
    try:
        window.navigate("实时监测", "multimeter", device)
        page = window.stack.currentWidget().scan_page
        assert tuple(page.cards) == (1, 2)
        request = AcquisitionRequest(channels=(1, 2))
        runtime.monitoring.start(device, (request,), interval=0.2, recording=True)
        qtbot.waitUntil(
            lambda: bool(runtime.monitoring.snapshots().get(device, {}).get("last_path")), timeout=8000
        )
        runtime.monitoring.stop(device).result(5)
        paths = list(runtime.repository.data_root.glob("*/**/*_continuous_*/_raw/segment-*.sqlite3"))
        frame = next(read_frames(paths[0]))
        assert [row["channel"] for row in frame["measurements"]] == [1, 2]
        assert [float(row["values"][0]) for row in frame["measurements"]] == [21.5, 23.75]
        assert all(row["unit"] == "°C" for row in frame["measurements"])
        wire.empty = True
        runtime.monitoring.start(device, (request,), interval=0.2, recording=False)
        qtbot.waitUntil(lambda: not runtime.monitoring.snapshots()[device]["running"], timeout=8000)
        assert runtime.sessions.sessions[device].state == "connected"
        assert "CH1" in runtime.monitoring.snapshots()[device]["notice"]
    finally:
        window.timer.stop()
        window.controller.close()
        runtime.close()
        window.closing = True
        window.bridge.worker.shutdown()

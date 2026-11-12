"""Exercise the common RF spectrum workspace with the real model decoder."""

from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.connections import ConnectionConfig
from lab_control.instruments.devices.keysight_n9030a import DEFINITION
from lab_control.instruments.devices.keysight_n9030a.adapter import N9030aAdapter
from lab_control.instruments.devices.keysight_n9030a.profile import ADAPTER_ID, TRACE_QUERY, n9030a
from lab_control.instruments.registry import DEVICE_DEFINITIONS
from lab_control.transport.base import MockTransport
from lab_control.ui.pages.monitoring.spectrum import SpectrumMonitoringPage
from lab_control.ui.real_window import RealWindow


def test_passive_spectrum_in_project_workspace(qtbot, tmp_path, monkeypatch):
    monkeypatch.setitem(DEVICE_DEFINITIONS, ADAPTER_ID, DEFINITION)
    replies = {
        "*IDN?": b"Agilent Technologies,N9030A,FIXTURE,A.14.59",
        "CONF?": b"SAN",
        "FREQ:STAR?": b"100000",
        "FREQ:STOP?": b"500000",
        "FREQ:CENT?": b"300000",
        "FREQ:SPAN?": b"400000",
        "SWE:TIME?": b"0.5",
        "SWE:POIN?": b"4",
        "UNIT:POW?": b"DBM",
        "FORM:DATA?": b"ASC,8",
        "FORM:BORD?": b"NORM",
        TRACE_QUERY: b"100000,-76,150000,-47,300000,-39,500000,-80",
    }

    def adapter(config):
        transport = MockTransport()
        transport.open()
        transport.query = replies.__getitem__
        return N9030aAdapter(transport, n9030a(), config)

    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        runtime.sessions.factory = adapter
        key = runtime.add(ConnectionConfig("mock://pxa", ADAPTER_ID, "mock", expected_serial="FIXTURE"))
        runtime.connect(key).result()
        window = RealWindow(runtime)
        qtbot.addWidget(window)
        try:
            window.show()
            window.navigate("实时监测", "spectrum_analyzer", key)
            page = window.stack.currentWidget()
            assert isinstance(page, SpectrumMonitoringPage)
            assert not page.controls.editors["span"].editors["value"].isEnabled()
            page.toggle_monitoring()
            qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
            (trace,) = page.plot.displayed_frame.batch.measurements
            assert (trace.unit, trace.axis_unit, trace.axis[1], trace.values[1]) == ("dBm", "Hz", 150000, -47)
            page.save()
            qtbot.waitUntil(lambda: bool(list(runtime.repository.data_root.rglob("samples.npz"))))
        finally:
            window.timer.stop()
            window.controller.close()
            window.closing = True
            window.bridge.worker.shutdown()

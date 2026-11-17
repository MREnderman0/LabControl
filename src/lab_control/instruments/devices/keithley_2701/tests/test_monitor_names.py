from dataclasses import replace

from lab_control.application.channel_names import current_binding, save_names
from lab_control.application.runtime import ApplicationRuntime
from lab_control.demo.instrument import SimulatedInstrument
from lab_control.domain.connections import ConnectionConfig
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.monitoring.scan import ScanMonitoringPage


def test_named_probes_fit_the_monitor_and_follow_the_recording(qtbot, tmp_path):
    config = ConnectionConfig("mock://pt100", "keithley.2701", "mock")
    instrument = SimulatedInstrument(config)
    instrument.caps = replace(instrument.caps, channels=(201, 101))
    instrument.capability_details = {
        "acquisition_channel_selection": "scan_list",
        "acquisition_channels": [201, 101],
    }
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        runtime.sessions.factory = lambda _: instrument
        device = runtime.add(config)
        runtime.connect(device).result(5)
        slot = runtime.create_slot("multimeter", "测温")
        runtime.bind_slot(device, slot, "general")
        names = {201: "T1 温度探头安装位置", 101: "T2 右侧"}
        save_names(runtime, current_binding(runtime, device, "general"), names)
        ctl = WorkspaceController(runtime, None)
        try:
            page = ScanMonitoringPage(ctl, lambda *_: None, manage_selection=False)
            qtbot.addWidget(page)
            page.resize(1200, 700)
            page.select_device(device)
            page.show()
            qtbot.waitUntil(lambda: page.cards[201].check.width() >= page.cards[201].check.sizeHint().width())
            assert page.readout_grid.itemAtPosition(0, 0).widget() is page.cards[201]
            assert page.readout_grid.itemAtPosition(1, 0).widget() is page.cards[101]
            assert names[201] in page.cards[201].check.text()
            context = runtime.repository.new_save_context("general", "single_frame", device_id=device)
            assert context.channel_names == {str(ch): name for ch, name in names.items()}
        finally:
            ctl.close()

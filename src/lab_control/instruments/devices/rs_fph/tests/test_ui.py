"""FPH registry contract on real Qt widgets; isolated simulation, no VISA."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import ParameterReading, ValidationError
from lab_control.domain.spectrum import prepare_changes
from lab_control.ui.controller import WorkspaceController
from lab_control.ui.pages.monitoring.page import MonitoringPage
from lab_control.ui.pages.monitoring.spectrum_controls import SpectrumControls


def test_fph_controls_monitor_save_draft_and_disconnect(qtbot, tmp_path):
    config = ConnectionConfig("mock://fph-ui", "rs.fph", "mock")
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as app:
        key = app.add(config)
        app.connect(key).result()
        parent = QWidget()
        qtbot.addWidget(parent)
        controller = WorkspaceController(app, parent)
        page = MonitoringPage(controller, "spectrum_analyzer", lambda *args: None)
        qtbot.addWidget(page)
        page.resize(1280, 760)
        page.show()
        page.select_device(key)
        assert page.points.value() == 711 and not page.points.isEnabled()
        adapter = app.sessions.sessions[key].adapter
        calls = []
        apply = adapter.apply
        adapter.apply = lambda command: calls.append(command.operation) or apply(command)
        for op, value in [
            ("center_frequency", 100e6),
            ("rbw_auto", False),
            ("rbw", 10000),
            ("vbw", 3000),
            ("reference_level", -20),
            ("attenuation", 20),
            ("sweep_time_auto", False),
            ("sweep_time", 0.1),
            ("detector", "SAMP"),
            ("trace_mode", "WRIT"),
        ]:
            editor = page.form.editors[op]
            editor.editors["value"].set_value(value)
            editor.save_draft()
            editor.apply()
            qtbot.waitUntil(lambda: not editor.busy)
            assert calls[-1] == op
        editor = page.form.editors["center_frequency"]
        editor.editors["value"].set_value(110e6)
        editor.save_draft()
        editor.show_reading(ParameterReading("center_frequency", 1, 100e6, "instrument"))
        page.initiate.setChecked(False)
        page.start()
        qtbot.waitUntil(lambda: page.plot.displayed_frame is not None)
        assert editor.dirty and editor.parameters() == {"value": 110e6}
        assert not (app.repository.root / "data").exists()
        app.monitoring.stop(key).result(3)
        page.save()
        qtbot.waitUntil(lambda: bool(list(app.repository.data_root.rglob("samples.npz"))))
        app.sessions.disconnect(key)
        controller.poll()
        page.refresh()
        assert not page.buttons["once"].isEnabled() and editor.dirty
        controller.close()


def test_fph_readback_outside_rbw_choices_shows_instrument_value(qtbot, tmp_path):
    config = ConnectionConfig("mock://fph-readback", "rs.fph", "mock")
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as app:
        key = app.add(config)
        app.connect(key).result()
        parent = QWidget()
        qtbot.addWidget(parent)
        controller = WorkspaceController(app, parent)
        controls = SpectrumControls(controller, key, parent)
        op = controls.editors["rbw"]
        field = op.editors["value"]
        op.show_reading(ParameterReading("rbw", 1, 3e6, "instrument"))
        index = field.input.currentIndex()
        assert field.input.currentText() == "3（只读）" and field.unit.currentText() == "MHz"
        assert field.input.currentData() == 3e6
        assert not field.input.model().index(index, 0).flags() & Qt.ItemFlag.ItemIsEnabled
        assert not op.dirty and op.matches_current()
        assert not app.repository.load_workspace(op.key)
        with pytest.raises(ValidationError):
            op.parameters()
        field.unit.setCurrentIndex(field.unit.findData(1))
        assert field.input.currentText() == "3000000（只读）"
        assert not op.dirty and field.feedback.text() == ""
        op.show_reading(ParameterReading("rbw", 1, 4e6, "instrument"))
        assert field.unit.currentText() == "Hz" and field.input.currentText() == "4000000（只读）"
        assert field.input.findData(3e6) == -1

        for name, editor in controls.editors.items():
            if name != "rbw":
                spec = editor.cap.parameters["value"]
                value = False if spec.kind == "boolean" else float(spec.choices[0]) if spec.choices else 0
                editor.show_reading(ParameterReading(name, 1, value, "instrument"))
        current = controls.current()
        assert controls.target()["rbw"] == current["rbw"] == 4e6
        commands = prepare_changes(
            current, {**controls.target(), "vbw_auto": True}, controller.capabilities(key)
        )
        assert [c.operation for c in commands] == ["vbw_auto"]

        field.input.setCurrentIndex(field.input.findData(1000.0))
        assert op.dirty and op.parameters() == {"value": 1000.0}
        op.show_reading(ParameterReading("rbw", 1, 5e6, "instrument"))
        assert field.input.currentData() == 1000.0 and op.dirty
        assert "5000000" in op.readback.text()
        op.show_reading(ParameterReading("rbw", 1, 1000.0, "instrument"))
        assert not op.dirty
        controller.close()

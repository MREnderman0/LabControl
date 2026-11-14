"""Stateful offline DH-8318 with the panel's optional setting acknowledgments."""

from collections import deque
from dataclasses import replace

import pytest

from lab_control.domain.instruments import DeviceCommand, DeviceError
from lab_control.instruments.devices.duoheyi_dh8318 import DEFINITION
from lab_control.instruments.devices.duoheyi_dh8318.adapter import Dh8318Adapter
from lab_control.transport.base import MockTransport

from .test_readonly import CONFIG, IDN


class LoadWire(MockTransport):
    def __init__(self, ack=True, selected="ALL"):
        super().__init__()
        self.open()
        self.ack = ack
        self.selected = selected
        self.lines = deque()
        self.state = {
            str(ch): dict(INP="0", MODE="CURR", VOLT="0", CURR="0", POW="0", RES="10") for ch in range(1, 9)
        }
        self.fail = None
        self.ignore = None
        self.drop = None
        self.rng = "1"

    def write(self, command):
        assert self.opened
        self.history.append(command)
        if command == self.drop:
            raise DeviceError("no reply", "timeout")
        if command == "*IDN?":
            self.lines.append(IDN.strip().decode())
        elif command == "CH?":
            self.lines.append(self.selected)
        elif command.endswith(":RANG?"):
            self.lines.append(self.rng)
        elif command.endswith("?"):
            if command.startswith("MEAS:"):
                self.lines.append(",".join("0" for _ in (self.state if self.selected == "ALL" else [0])))
                return
            group = self.state if self.selected == "ALL" else (self.selected,)
            self.lines.append(",".join(self.state[ch][command[:-1]] for ch in group))
        else:
            if command not in (self.fail, self.ignore):
                op, val = command.split()
                if op == "CH":
                    self.selected = val
                else:
                    assert self.selected != "ALL", "broadcast setting"
                    self.state[self.selected][op] = val
            if self.ack:
                self.lines.append("ERR" if command == self.fail else "OK")

    def read(self):
        if not self.lines:
            raise DeviceError("empty reply", "timeout")
        return self.lines.popleft().encode() + b"\n"


def connect(wire):
    adapter = Dh8318Adapter(wire, DEFINITION.profile, CONFIG)
    adapter.identify()
    return adapter


@pytest.mark.parametrize("ack", [False, True])
@pytest.mark.parametrize("initial", ["ALL", "2", "7"])
def test_controls_apply_to_one_channel_and_restore(ack, initial):
    wire = LoadWire(ack, initial)
    board = connect(wire)
    original = {ch: state.copy() for ch, state in wire.state.items() if ch != "7"}
    for mode, op, val in [
        ("CV", "voltage", 5.0),
        ("CP", "load_power", 9.0),
        ("CR", "resistance", 100.0),
        ("CC", "current", 1.5),
    ]:
        for name, value in (("load_mode", mode), (op, val)):
            result = board.apply(DeviceCommand(name, {"value": value}, channel=7, confirmed=True))
            assert result.outcome == "success" and result.readback == value
            assert wire.selected == initial and not wire.lines
    for enabled in (True, False):
        result = board.apply(DeviceCommand("load_input", {"value": enabled}, channel=7, confirmed=True))
        assert result.outcome == "success" and result.readback is enabled
        assert wire.selected == initial and not wire.lines
    assert original == {ch: state for ch, state in wire.state.items() if ch != "7"}
    assert wire.history.count("INP 1") == wire.history.count("INP 0") == 1


def test_no_confirmation_or_invalid_target_sends_nothing():
    wire = LoadWire()
    board = connect(wire)
    for request in (
        DeviceCommand("load_input", {"value": True}, channel=2),
        DeviceCommand("current", {"value": 31}, channel=2, confirmed=True),
        DeviceCommand("load_mode", {"value": "LED"}, channel=2, confirmed=True),
        DeviceCommand("resistance", {"value": 0}, channel=2, confirmed=True),
    ):
        with pytest.raises(DeviceError):
            board.apply(request)
    assert wire.history == ["*IDN?", "CH?"]


def test_current_range_and_mode_before_setting():
    wire = LoadWire()
    wire.rng = "0"
    board = connect(wire)
    for op, value in (("current", 3.5), ("voltage", 4)):
        with pytest.raises(DeviceError):
            board.apply(DeviceCommand(op, {"value": value}, channel=3, confirmed=True))
        assert wire.selected == "ALL" and not wire.lines
    assert "CURR 3.5" not in wire.history and "VOLT 4" not in wire.history


@pytest.mark.parametrize("fault", ["fail", "ignore"])
def test_unconfirmed_selector_never_sends_input(fault):
    wire = LoadWire()
    setattr(wire, fault, "CH 5")
    board = connect(wire)
    with pytest.raises(DeviceError):
        board.apply(DeviceCommand("load_input", {"value": True}, channel=5, confirmed=True))
    assert wire.selected == "ALL" and not wire.lines and "INP 1" not in wire.history


@pytest.mark.parametrize("fault", ["fail", "ignore", "drop"])
def test_bad_write_never_replays_or_reports_success(fault):
    wire = LoadWire()
    setattr(wire, fault, "INP 1" if fault != "drop" else "INP?")
    board = connect(wire)
    result = board.apply(DeviceCommand("load_input", {"value": True}, channel=5, confirmed=True))
    assert result.outcome == ("uncertain" if fault == "drop" else "rejected")
    assert wire.history.count("INP 1") == 1
    if fault == "drop":
        assert not wire.opened and wire.selected == "5"
    else:
        assert wire.selected == "ALL" and not wire.lines


def test_restore_failure_is_reported_after_single_input_write():
    wire = LoadWire()
    wire.fail = "CH ALL"
    board = connect(wire)
    with pytest.raises(DeviceError) as caught:
        board.apply(DeviceCommand("load_input", {"value": True}, channel=5, confirmed=True))
    assert caught.value.code == "restore_failed" and wire.history.count("INP 1") == 1


def test_passive_read_in_all_mode_uses_requested_channel():
    wire = LoadWire()
    wire.state["4"].update(INP="1", MODE="VOLTAGE", VOLT="12")
    board = connect(wire)
    assert [r.value for r in board.read_settings(("load_input", "load_mode", "voltage"), 4)] == [
        True,
        "CV",
        12.0,
    ]
    assert all(s.endswith("?") for s in wire.history)


def test_mixed_active_targets_are_read_in_columns_without_selecting_channels():
    wire = LoadWire()
    wire.state["2"].update(MODE="VOLT", VOLT="12.5")
    wire.state["3"].update(MODE="POW", POW="2")
    wire.state["4"].update(MODE="RES", RES="120")
    wire.state["5"].update(MODE="DYN")
    status = connect(wire).read_status().values["channels"]
    assert status[2] == {"load_input": False, "load_mode": "CV", "voltage": 12.5}
    assert status[3]["load_power"] == 2 and status[4]["resistance"] == 120
    assert status[5] == {"load_input": False, "load_mode": "DYN"}
    assert all(line.endswith("?") for line in wire.history)
    for query in ("INP?", "MODE?", "CURR?", "VOLT?", "POW?", "RES?"):
        assert wire.history.count(query) == 1


@pytest.mark.parametrize("fail_target", [False, True])
def test_management_group_uses_selected_channel(qtbot, tmp_path, monkeypatch, fail_target):
    from PySide6.QtWidgets import QMessageBox

    from lab_control.application.runtime import ApplicationRuntime
    from lab_control.instruments.registry import build_scpi_adapter
    from lab_control.ui.real_window import RealWindow

    wire = LoadWire()
    wire.state["1"].update(MODE="RES", RES="100")
    monkeypatch.setattr(
        "lab_control.application.runtime.create_adapter",
        lambda config, _visa: build_scpi_adapter(config, wire),
    )
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        key = runtime.add(replace(CONFIG, resource="mock://load", transport="mock"))
        runtime.connect(key).result(5)
        window = RealWindow(runtime)
        qtbot.addWidget(window)
        window.show()
        try:
            window.navigate("设备管理", "electronic_load", key)
            page = window.stack.currentWidget()
            qtbot.waitUntil(lambda: page.frame is not None)
            page.table.selectRow(6)
            page.table.selectRow(0)
            qtbot.waitUntil(page.editors["resistance"].isVisible)
            assert page.group.mode_box.currentData() == "CR" and page.editors["current"].isHidden()
            assert page.editors["resistance"].current_value == 100
            page.selector.select(key, 4)
            qtbot.waitUntil(lambda: page.frame is not None and not page.reading)
            mode = page.editors["load_mode"].editors["value"].input
            checkbox = page.editors["load_input"].editors["value"].input
            mode.setCurrentIndex(mode.findData("CP"))
            target = page.editors["load_power"]
            qtbot.waitUntil(target.isVisible)
            assert page.editors["current"].isHidden()
            field = target.editors["value"].input
            field.setFocus()
            qtbot.keyClicks(field, "2.5")
            checkbox.click()
            if fail_target:
                wire.fail = "POW 2.5"
            qtbot.waitUntil(page.group.apply_button.isEnabled)
            page.group.apply_button.click()
            qtbot.waitUntil(lambda: not page.group.running and not page.reading)

            def writes():
                return [cmd for cmd in wire.history if not cmd.endswith("?") and not cmd.startswith("CH ")]

            if fail_target:
                assert writes() == ["MODE POW", "POW 2.5"]
                assert wire.state["4"]["INP"] == "0" and "未执行" in page.group.status.text()
            else:
                assert writes() == ["MODE POW", "POW 2.5", "INP 1"]
                assert not page.group.apply_button.isEnabled()
                checkbox.click()
                mode.setCurrentIndex(mode.findData("CC"))
                qtbot.waitUntil(page.group.apply_button.isEnabled)
                page.group.apply_button.click()
                qtbot.waitUntil(lambda: not page.group.running and not page.reading)
                assert writes()[-2:] == ["INP 0", "MODE CURR"]
            assert wire.selected == "ALL"
        finally:
            window.close()
            qtbot.waitUntil(lambda: window.closing and not window.isVisible())

"""Exercise binding lifetime, physical channel IDs and editor persistence."""

import pytest

from lab_control.application.channel_names import current_binding, save_names
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import ValidationError
from lab_control.ui.widgets.channel_names import ChannelNamesDialog
from lab_control.ui.widgets.slot_picker import SlotBindingDialog


def test_replace_reset_stale_editor_and_restart(qtbot, tmp_path):
    mode = "general"
    with ApplicationRuntime(root=tmp_path, hardware=False) as rt:
        large, small = (
            rt.add(ConnectionConfig(f"mock://meter{n}", "joinwit.jw8105", "mock", options=(f"channels={n}",)))
            for n in (16, 8)
        )
        a = rt.create_slot("optical_power_meter", "光路 A")
        b = rt.create_slot("optical_power_meter", "光路 B")
        rt.bind_slot(large, a, mode)
        rt.bind_slot(small, b, mode)
        save_names(rt, current_binding(rt, small, mode), {1: "监测光"})
        original = current_binding(rt, large, mode)
        save_names(rt, original, {1: "参考光", 11: "备用"})
        stale = ChannelNamesDialog(rt, large, mode)
        qtbot.addWidget(stale)
        rt.bind_slot(small, a, mode, replace_device_id=large)
        editor = ChannelNamesDialog(rt, small, mode)
        qtbot.addWidget(editor)
        assert tuple(editor.editors) == tuple(range(1, 9))
        assert editor.editors[1].text() == "参考光"
        editor.editors[1].clear()
        editor.editors[1].setModified(True)
        editor.save()
        assert rt.repository.function_slots.channel_names(a, mode) == {"11": "备用"}
        rt.bind_slot(large, a, mode, replace_device_id=small)
        stale.reset_names()
        assert "重新打开" in stale.message.text()
        fresh = ChannelNamesDialog(rt, large, mode)
        qtbot.addWidget(fresh)
        assert fresh.editors[11].text() == "备用"
        rt.bind_slot(small, a, mode, replace_device_id=large)
        editor = ChannelNamesDialog(rt, small, mode)
        qtbot.addWidget(editor)
        editor.reset_names()
        assert rt.repository.function_slots.channel_names(a, mode) == {}
        save_names(rt, current_binding(rt, small, mode), {2: "本振光"})
        rt.bind_slot(small, "", mode)
        rt.delete_slot(a, mode)
        with pytest.raises(ValidationError):
            save_names(rt, original, {1: "旧窗口"})
        assert rt.repository.function_slots.channel_names(a, mode) == {}
        replacement = rt.create_slot("optical_power_meter", "光路 A")
        assert replacement != a and not rt.repository.function_slots.channel_names(replacement, mode)
    with ApplicationRuntime(root=tmp_path, hardware=False) as reopened:
        assert reopened.repository.function_slots.channel_names(b, mode) == {"1": "监测光"}


def test_sparse_channels_and_snapshot_follow_the_slot(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as rt:
        device = rt.add(ConnectionConfig("mock://kt", "pangu.kt616r", "mock", options=("channels=1,3,11",)))
        slot = rt.create_slot("multimeter", "温度")
        rt.bind_slot(device, slot, "general")
        binding = current_binding(rt, device, "general")
        assert binding.channels == (1, 3, 11)
        save_names(rt, binding, {11: "机箱温度"})
        with pytest.raises(ValidationError):
            save_names(rt, binding, {2: "无此通道"})
        saved = rt.repository.new_save_context("general", "single_frame", device_id=device)
        save_names(rt, binding, {11: "环境温度"})
        rt.bind_slot(device, "", "general")
        rt.delete_slot(slot)
        saved.prepare()
        assert "CH11：机箱温度" in (saved.folder / "记录说明.txt").read_text("utf-8-sig")


def test_renaming_slot_preserves_binding_aliases_and_recording_context(qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    mode = "general"
    with ApplicationRuntime(root=tmp_path, hardware=False) as rt:
        device = rt.add(ConnectionConfig("mock://meter", "joinwit.jw8105", "mock"))
        slot = rt.create_slot("optical_power_meter", "光路 A")
        rt.create_slot("optical_power_meter", "光路 B")
        rt.bind_slot(device, slot, mode)
        binding = current_binding(rt, device, mode)
        save_names(rt, binding, {1: "参考", 8: "输出"})
        old = rt.repository.new_save_context(mode, "continuous", device_id=device)
        dialog = SlotBindingDialog(rt, device, "optical_power_meter", mode)
        qtbot.addWidget(dialog)
        monkeypatch.setattr(QInputDialog, "getText", lambda *a, **kw: ("新槽位", True))
        dialog.rename_button.click()
        assert dialog.combo.currentData() == slot and dialog.combo.currentText().startswith("新槽位")
        dialog.reject()
        assert current_binding(rt, device, mode) == binding
        assert old.slot_label == "光路 A"
        new = rt.repository.new_save_context(mode, "single_frame", device_id=device)
        assert (new.slot, new.slot_label, new.channel_names) == (slot, "新槽位", old.channel_names)
        for bad in (" 光路 b ", " ", "A\nB"):
            with pytest.raises(ValidationError):
                rt.rename_slot(slot, bad)
    with ApplicationRuntime(root=tmp_path, hardware=False) as rt:
        assert current_binding(rt, device, mode) == binding
        assert rt.repository.function_slots.label(slot, mode) == "新槽位"
        assert rt.repository.function_slots.channel_names(slot, mode) == {"1": "参考", "8": "输出"}


@pytest.mark.parametrize("scan", [False, True])
def test_names_and_readings_share_header_columns(qtbot, scan):
    from PySide6.QtWidgets import QLabel

    from lab_control.ui.pages.monitoring.optical_power_readout import PowerMeterReadoutRow, readout_header
    from lab_control.ui.pages.monitoring.scan import ScanReadoutCard, scan_readout_header

    header = scan_readout_header() if scan else readout_header()
    qtbot.addWidget(header)
    cards = [(ScanReadoutCard if scan else PowerMeterReadoutRow)(i) for i in (1, 3, 11)]
    for card, label in zip(cards, ("CH 1", "长名称与English" * 6, "CH 11"), strict=True):
        qtbot.addWidget(card)
        (card.check if scan else card.channel_label).setText(label)
        card.show()
    header.show()
    for width in (330, 460):
        header.resize(width * 2 + 12 if scan else width, 26)
        for card, reading in zip(cards, ("—", "-12.345", "0.00215"), strict=True):
            card.resize(width, 28)
            card.value.setText(reading)
            card.setFixedHeight(28)
        qtbot.wait(20)
        heading = next(w for w in header.findChildren(QLabel) if w.text() == ("读数" if scan else "功率"))
        header_edge = heading.mapTo(header, heading.rect().topRight()).x()
        assert {c.value.mapTo(c, c.value.rect().topRight()).x() for c in cards} == {header_edge}
        assert len({c.peak.mapTo(c, c.peak.rect().topRight()).x() for c in cards}) == 1

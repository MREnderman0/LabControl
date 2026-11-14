"""Exercise binding lifetime, physical channel IDs and editor persistence."""

import pytest

from lab_control.application.channel_names import current_binding, save_names
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import ValidationError
from lab_control.ui.widgets.channel_names import ChannelNamesDialog


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

from concurrent.futures import Future
from threading import Event

import pytest
from PySide6.QtCore import QPoint, Qt

from lab_control.domain.instruments import ApplyResult, Parameter
from lab_control.ui.pages.monitoring.oscilloscope_controls import ScopeSlider

from .test_oscilloscope_workspace import scope as scope


@pytest.mark.parametrize("division,grain", [(0.02, None), (1e-6, 1e-9), (0.05, 0.001)])
def test_coarse_fine_keyboard_relative_drag_and_fixed_endpoints(qtbot, division, grain):
    field = ScopeSlider("偏移", "V")
    qtbot.addWidget(field)
    field.resize(520, 80)
    field.show()
    spec = Parameter("number", "V", -division * 100, division * 100, resolution=grain)
    original = division * 0.037
    field.configure(original, spec, division)
    assert field.step >= division * 0.1 * (1 - 1e-12)
    field.set_fine(True)
    field.configure(original, spec, division)
    assert field.step >= division * 0.01 * (1 - 1e-12) and (not grain or field.step >= grain)
    changes = []
    field.committed.connect(changes.append)
    slider = field.slider
    qtbot.mousePress(slider, Qt.MouseButton.LeftButton, pos=QPoint(60, 8))
    qtbot.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=QPoint(60, 8))
    assert changes == [] and field.actual == original
    qtbot.mousePress(slider, Qt.MouseButton.LeftButton, pos=QPoint(60, 8))
    field.fine_move(0.1)
    assert not field.dirty
    field.fine_move(100)
    assert 0 < field.value() - original <= division + division * 1e-9
    assert changes == []
    qtbot.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=QPoint(160, 8))
    assert len(changes) == 1
    field.configure(changes[-1], spec, division)
    assert (field.low, field.high) == (spec.minimum, spec.maximum)
    base = field.actual
    qtbot.keyClick(slider, Qt.Key.Key_Right)
    qtbot.keyClick(slider, Qt.Key.Key_PageUp)
    assert len(changes) == 1
    qtbot.waitUntil(lambda: len(changes) == 2)
    assert changes[-1] == pytest.approx(base + 11 * field.step)


def test_fixed_footer_preference_and_no_instrument_write(scope, qtbot, monkeypatch):
    _, page, runtime, _, adapter = scope
    monkeypatch.setattr(adapter, "apply", lambda _: pytest.fail("Toggle cannot write hardware"))
    controls = page.controls
    footer_top = controls.fine_button.mapTo(controls, QPoint()).y()
    controls.scroller.verticalScrollBar().setValue(controls.scroller.verticalScrollBar().maximum())
    assert controls.fine_button.mapTo(controls, QPoint()).y() == footer_top
    assert not controls.fine_button.isChecked()
    controls.fine_button.click()
    assert runtime.repository.load_workspace(controls.fine_key) is True
    assert controls.sliders[1, "offset"].step == pytest.approx(controls.values[1, "scale"] * 0.01)
    assert controls.sliders[1, "time_position"].step == pytest.approx(controls.values[1, "timebase"] * 0.01)
    assert not controls.sliders[1, "scale"].fine


def test_future_completion_delivered_without_polling(scope, qtbot):
    window, _, _, _, _ = scope
    window.timer.stop()
    window.controller.timer.stop()
    future = Future()
    values = []
    window.controller.watch(future, values.append)
    future.set_result(42)
    qtbot.waitUntil(lambda: values == [42])
    window.controller._deliver_jobs()
    assert values == [42]


def test_normal_groove_press_waits_for_release_once(qtbot):
    field = ScopeSlider("偏移", "V")
    qtbot.addWidget(field)
    field.resize(520, 80)
    field.configure(0, Parameter("number", "V", -10, 10), 1)
    field.show()
    changes = []
    field.committed.connect(changes.append)
    position = QPoint(field.slider.width() - 4, 8)
    qtbot.mousePress(field.slider, Qt.MouseButton.LeftButton, pos=position)
    qtbot.wait(300)
    assert changes == []
    qtbot.mouseRelease(field.slider, Qt.MouseButton.LeftButton, pos=position)
    assert len(changes) == 1


def test_real_groove_drag_previews_without_frame_and_writes_only_last_value(scope, qtbot, monkeypatch):
    _, page, _, _, adapter = scope
    field = page.controls.sliders[1, "offset"]
    slider = field.slider
    assert field.isEnabled() and page.plot.displayed_frame is None
    writes = []
    apply = adapter.apply
    monkeypatch.setattr(adapter, "apply", lambda cmd: writes.append(cmd) or apply(cmd))
    start = QPoint(slider.width() // 4, slider.height() // 2)
    end = QPoint(slider.width() * 3 // 4, start.y())
    qtbot.mousePress(slider, Qt.MouseButton.LeftButton, pos=start)
    first = field.value()
    qtbot.mouseMove(slider, pos=end)
    qtbot.waitUntil(lambda: field.value() != first)
    last = field.value()
    assert page.plot.preview_hint.isVisible() and "预计CH1 偏移" in page.plot.preview_hint.text()
    assert "等待首帧" in page.plot.preview_hint.text() and "预计" in field.value_label.text()
    assert not writes
    qtbot.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=end)
    qtbot.waitUntil(lambda: bool(writes) and not page.controls.running)
    assert [c.parameters["value"] for c in writes] == [last]
    assert not page.plot.preview_hint.isVisible()


def test_click_on_custom_readback_does_not_select_nearest_step(qtbot):
    field = ScopeSlider("档位", "V", coarse=True)
    qtbot.addWidget(field)
    field.configure(0.037, Parameter("number", "V/div", 0.001, 1), 0.037)
    writes = []
    field.committed.connect(writes.append)
    field.slider.setSliderDown(True)
    field.slider.setSliderDown(False)
    assert not writes and "37 mV" in field.value_label.text()


def test_escape_and_release_restore_actual_value_without_writing(scope, qtbot, monkeypatch):
    _, page, _, _, adapter = scope
    field = page.controls.sliders[1, "offset"]
    original = field.value_label.text()
    monkeypatch.setattr(adapter, "apply", lambda cmd: pytest.fail(f"Cancelled slider wrote {cmd}"))
    slider = field.slider
    point = QPoint(slider.width() // 4, slider.height() // 2)
    qtbot.mousePress(slider, Qt.MouseButton.LeftButton, pos=point)
    assert field.dirty and page.plot.preview_hint.isVisible()
    qtbot.keyClick(slider, Qt.Key.Key_Escape)
    qtbot.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=point)
    qtbot.wait(250)
    assert not field.dirty and field.value_label.text() == original
    assert not page.plot.preview_hint.isVisible() and not page.controls.running


@pytest.mark.parametrize("reject_first", [False, True])
def test_rapid_drag_while_waiting_does_not_queue_another_write(scope, qtbot, monkeypatch, reject_first):
    _, page, _, _, adapter = scope
    field = page.controls.sliders[1, "offset"]
    initial = field.actual
    slider = field.slider
    apply = adapter.apply
    release = Event()
    writes = []

    def slow_apply(cmd):
        writes.append(cmd)
        if len(writes) == 1:
            assert release.wait(5)
            if reject_first:
                return ApplyResult("rejected", cmd.operation, cmd.parameters, initial, error="test rejection")
        return apply(cmd)

    monkeypatch.setattr(adapter, "apply", slow_apply)

    def point(percent):
        return QPoint(round((slider.width() - 1) * percent / 100), slider.height() // 2)

    qtbot.mousePress(slider, Qt.MouseButton.LeftButton, pos=point(18))
    for percent in [70, 38, 85, 25, 78, 47, 82]:
        qtbot.mouseMove(slider, point(percent))
    target = field.value()
    assert not writes
    qtbot.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=point(82))
    try:
        qtbot.waitUntil(lambda: bool(writes) and not slider.isEnabled())
        qtbot.mouseClick(slider, Qt.MouseButton.LeftButton, pos=point(20))
        qtbot.wait(220)
        assert len(writes) == 1 and page.controls.running and not slider.isEnabled()
    finally:
        release.set()
    qtbot.waitUntil(lambda: not page.controls.running and slider.isEnabled())
    assert field.actual == (initial if reject_first else target)
    assert not page.plot.preview_hint.isVisible() and not field.dirty
    qtbot.mouseClick(slider, Qt.MouseButton.LeftButton, pos=point(20))
    qtbot.waitUntil(lambda: len(writes) == 2 and not page.controls.running)
    assert writes[0].parameters["value"] == target
    assert writes[1].parameters["value"] == field.actual != target


def test_screen_tag_drag_previews_then_writes_once_through_the_offset_field(scope, qtbot, monkeypatch):
    _, page, _, _, adapter = scope
    field = page.controls.sliders[1, "offset"]
    assert field.isEnabled() and 1 in page.controls.offset_channels()
    writes = []
    apply = adapter.apply
    monkeypatch.setattr(adapter, "apply", lambda cmd: writes.append(cmd) or apply(cmd))
    page.start()
    qtbot.waitUntil(lambda: bool(page.plot.ground_tags.get(1) and page.plot.ground_tags[1].draggable))
    page.begin_tag_drag(1, "offset")
    target = field.actual + 3 * field.step
    page.drag_offset(1, target + field.step / 3, False)
    assert field.dirty and "预计" in field.value_label.text() and not writes
    page.drag_offset(1, target, True)
    qtbot.waitUntil(lambda: bool(writes) and not page.controls.running)
    assert [c.operation for c in writes] == ["offset"]
    assert writes[0].parameters["value"] == pytest.approx(target)
    qtbot.waitUntil(lambda: page.plot.ground_tags[1].draggable)
    page.begin_tag_drag(1, "offset")
    page.drag_offset(1, field.high * 10, True)  # Beyond the legal range: clamped by the field.
    qtbot.waitUntil(lambda: len(writes) == 2 and not page.controls.running)
    assert writes[-1].parameters["value"] == pytest.approx(field.high)

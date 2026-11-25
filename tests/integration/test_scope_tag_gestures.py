"""Exercise screen-tag mouse grabs through the Qt viewport and the mock instrument."""

import pytest
from PySide6.QtCore import QPoint, QRect, Qt

from lab_control.domain.instruments import DeviceCommand

from .test_oscilloscope_workspace import scope as scope


def show_trace(scope, qtbot, *, live=False):
    window, page, runtime, key, _ = scope
    window.fit_to_screen(QRect(0, 0, 1600, 950))
    window.resize(1500, 860)
    page.start()
    qtbot.waitUntil(lambda: bool(page.plot.trigger_tag and page.plot.trigger_tag.draggable))
    if not live:
        runtime.monitoring.stop(key).result(5)
    return page.plot


def tag_position(plot, kind):
    tag = plot.ground_tags[1] if kind == "offset" else plot.trigger_tag
    start = plot.plot.mapFromScene(tag.sceneBoundingRect().center())
    viewport = plot.plot.viewport()
    assert viewport.rect().contains(start)
    direction = 1 if start.y() < viewport.height() // 2 else -1
    return viewport, start, start + QPoint(0, direction * 38)


@pytest.fixture
def writes(scope, monkeypatch):
    adapter = scope[-1]
    apply = adapter.apply
    commands = []
    monkeypatch.setattr(adapter, "apply", lambda cmd: commands.append(cmd) or apply(cmd))
    return commands


@pytest.mark.parametrize("kind", ["offset", "trigger_level"])
@pytest.mark.parametrize("case", ["live", "paused", "outside", "keyboard"])
def test_tag_writes_once_only_after_release(scope, qtbot, writes, kind, case):
    _, page, _, _, adapter = scope
    field = page.controls.sliders[1, "offset"] if kind == "offset" else page.controls.trigger_slider
    if case == "outside":
        old = field.high + field.step
        adapter.values["1." + kind] = {"value": old}
        page.controls.refresh_parameters()
        qtbot.waitUntil(lambda: not page.controls.running and field.actual == old)
    plot = show_trace(scope, qtbot, live=case == "live")
    viewport, start, end = tag_position(plot, kind)
    qtbot.mouseClick(viewport, Qt.MouseButton.LeftButton, pos=start)
    assert not writes
    if case == "keyboard":
        qtbot.keyClick(field.slider, Qt.Key.Key_Right)
        assert field.dirty and field.timer.isActive()
    qtbot.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    qtbot.wait(230)  # Wait past the pending keyboard debounce before the first move.
    qtbot.mouseMove(viewport, start + QPoint(0, (end.y() - start.y()) // 2))
    qtbot.mouseMove(viewport, end)
    qtbot.wait(2100 if case == "live" else 320)
    assert field.tag_active and field.dirty and not writes
    qtbot.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)
    qtbot.waitUntil(lambda: bool(writes) and not page.controls.running)
    qtbot.wait(220)
    assert [cmd.operation for cmd in writes] == [kind]
    assert writes[0].parameters["value"] == pytest.approx(field.actual)


@pytest.mark.parametrize("cancel", ["escape", "return", "version", "disconnect", "switch"])
def test_cancel_tag_ignores_subsequent_motion_and_release(scope, qtbot, writes, cancel):
    _, page, runtime, key, _ = scope
    plot = show_trace(scope, qtbot)
    viewport, start, end = tag_position(plot, "trigger_level")
    qtbot.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(viewport, end)
    assert page.tag_drag is not None
    if cancel == "escape":
        qtbot.keyClick(viewport, Qt.Key.Key_Escape)
    elif cancel == "return":
        end = start
    elif cancel == "version":
        # Change configuration on the worker before the next GUI poll.
        runtime.sessions.submit(key, "apply", DeviceCommand("scale", {"value": 1.0}, 1)).result(5)
    elif cancel == "disconnect":
        runtime.sessions.disconnect(key)
    else:
        page.select_device("")
    qtbot.mouseMove(viewport, end + QPoint(1, 2))
    qtbot.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)
    qtbot.wait(250)
    assert page.tag_drag is None
    assert [cmd.operation for cmd in writes] == (["scale"] if cancel == "version" else [])


def test_stale_frame_tags_unlock_only_on_fresh_scale(scope, qtbot, writes):
    _, page, _, _, _ = scope
    plot = show_trace(scope, qtbot)
    assert plot.display_specs[1].units_per_div == 0.5
    slider = page.controls.sliders[1, "scale"]
    qtbot.keyClick(slider.slider, Qt.Key.Key_Right)
    qtbot.waitUntil(lambda: slider.actual == 1.0 and not page.controls.running)
    viewport, start, end = tag_position(plot, "offset")
    assert not plot.ground_tags[1].draggable and not plot.trigger_tag.draggable
    qtbot.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(viewport, end)
    qtbot.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)
    assert [cmd.operation for cmd in writes] == ["scale"]
    page.start()
    qtbot.waitUntil(lambda: plot.ground_tags[1].draggable and plot.display_specs[1].units_per_div == 1.0)
    levels = []
    plot.ground_tags[1].dragged.connect(lambda y, finished: levels.append(y) if finished else None)
    viewport, start, end = tag_position(plot, "offset")
    qtbot.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(viewport, end)
    qtbot.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)
    qtbot.waitUntil(lambda: len(writes) == 2 and not page.controls.running)
    assert writes[-1].operation == "offset"
    assert writes[-1].parameters["value"] == pytest.approx(levels[0] * 1.0)


def test_visible_switch_tracks_silent_readback(qtbot):
    from PySide6.QtCore import QSignalBlocker

    from lab_control.ui.kit.controls import Switch

    switch = Switch()
    qtbot.addWidget(switch)
    switch.show()
    switch.setChecked(True)
    with QSignalBlocker(switch):
        switch.setChecked(False)
        switch.setChecked(True)
    assert switch.isChecked() and switch._position == 1.0
    assert switch._animation.state() == switch._animation.State.Stopped

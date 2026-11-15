"""Device-management view for native phase-noise rounds."""

from datetime import datetime
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand
from lab_control.instruments.devices.rs_fspn26.settings import LABELS
from lab_control.storage.recording import read_frames
from lab_control.ui.widgets.workspace_combo import WorkspaceComboBox

COLORS = ("#edb25b", "#72d2dd", "#b19cf1", "#99cf8e", "#e88e92", "#89acdc")
METRICS = (
    ("carrier_hz", "载波频率", "Hz"),
    ("carrier_dbm", "载波电平", "dBm"),
    ("attenuation_db", "衰减", "dB"),
    ("rbw_percent", "RBW", "%"),
    ("xcorr", "互相关", "×"),
    ("estimated_s", "预计用时", "s"),
)


def load_round(paths):
    frames = (f for path in paths for f in read_frames(Path(path)))
    frame = next(frames, None)
    if frame is None or next(frames, None) is not None:
        raise ValueError("未取得本轮唯一的有效帧")
    folder = Path(paths[0]).parent
    if folder.name == "_raw":
        folder = folder.parent
    if frame["configuration"]["paired_curves"]:
        folder = folder / f"round-{frame['sequence']:04d}"
        if not all((folder / name).is_file() for name in ("phase_noise.csv", "allan_deviation.csv")):
            raise OSError("双曲线 CSV 保存不完整")
    return frame, folder


class FspnPanel(QWidget):
    def __init__(self, controller, plot, selector):
        super().__init__()
        self.ctrl, self.selector = controller, selector
        self.key = ""
        self.busy = self.repeat = self.stopping = self.collecting = False
        self.generation = self.rounds = 0
        self.last = {}
        self.baseline = {}
        self.snapshot_attempted = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        actions = QHBoxLayout()
        self.single, self.cont, self.stop, self.preview = [
            QPushButton(t) for t in ("单次采集", "连续采集", "停止", "读取当前结果")
        ]
        self.single.setObjectName("primaryButton")
        for b in (self.single, self.cont, self.stop, self.preview):
            actions.addWidget(b)
        self.status = QLabel("准备就绪")
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        actions.addWidget(self.status, 1)
        layout.addLayout(actions)
        bar = QFrame()
        bar.setObjectName("operationCard")
        summary = QGridLayout(bar)
        self.metrics = {}
        for col, (key, metric_name, _) in enumerate(METRICS):
            caption = QLabel(metric_name)
            caption.setObjectName("mutedText")
            self.metrics[key] = QLabel("—")
            self.metrics[key].setStyleSheet("font-size: 16px; font-weight: 600")
            summary.addWidget(caption, 0, col)
            summary.addWidget(self.metrics[key], 1, col)
            summary.setColumnStretch(col, 1)
        layout.addWidget(bar)
        body = QSplitter(Qt.Orientation.Horizontal)
        charts = QSplitter(Qt.Orientation.Vertical)
        self.pn = plot("单边带相噪谱", "频偏", "Hz", "相噪", "dBc/Hz", False)
        self.adev = plot("Allan 偏差", "平均时间 τ", "s", "分数频率偏差 σᵧ", "", True)
        for chart in (self.pn, self.adev):
            chart.getAxis("left").setWidth(68)
        self.adev.getPlotItem().legend.hide()
        charts.addWidget(self.pn)
        charts.addWidget(self.adev)
        charts.setSizes([345, 245])
        body.addWidget(charts)
        aside = QWidget()
        side = QVBoxLayout(aside)
        side.setContentsMargins(7, 1, 3, 2)
        title = QLabel("Spot Noise · dBc/Hz")
        title.setObjectName("controlTitle")
        side.addWidget(title)
        self.spots = QTableWidget(0, 3)
        self.spots.setHorizontalHeaderLabels(["迹线", "频偏 (Hz)", "相噪 (dBc/Hz)"])
        self.spots.verticalHeader().setMinimumSectionSize(18)
        self.spots.verticalHeader().setDefaultSectionSize(23)
        self.spots.verticalHeader().hide()
        self.spots.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.spots.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.spots.setShowGrid(False)
        self.spots.setAlternatingRowColors(True)
        self.spots.horizontalHeader().setMinimumSectionSize(60)
        self.spots.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.spots.setMinimumHeight(105)
        side.addWidget(self.spots, 1)
        title = QLabel("测量参数")
        title.setObjectName("controlTitle")
        side.addWidget(title)
        form = QFormLayout()
        self.fields = {}
        for name, label in LABELS.items():
            field = QLineEdit()
            field.setAlignment(Qt.AlignmentFlag.AlignRight)
            self.fields[name] = field
            if name == "attenuation_db":
                row = QHBoxLayout()
                self.att_mode = WorkspaceComboBox()
                self.att_mode.addItems(["自动", "手动"])
                self.att_mode.currentIndexChanged.connect(self.update_actions)
                row.addWidget(self.att_mode)
                row.addWidget(field, 1)
                form.addRow(label, row)
            else:
                form.addRow(label, field)
            field.textEdited.connect(self.update_actions)
        side.addLayout(form)
        row = QHBoxLayout()
        self.read_button, self.apply = QPushButton("刷新参数"), QPushButton("应用")
        row.addWidget(self.read_button)
        row.addWidget(self.apply)
        side.addLayout(row)
        self.open = QPushButton("打开采集目录")
        side.addWidget(self.open)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(aside)
        scroll.setMinimumWidth(255)
        body.addWidget(scroll)
        body.setStretchFactor(0, 1)
        body.setSizes([780, 295])
        layout.addWidget(body, 1)
        self.stamp = QLabel("")
        self.stamp.setObjectName("mutedText")
        layout.addWidget(self.stamp)
        self.single.clicked.connect(lambda: self.start("single"))
        self.cont.clicked.connect(lambda: self.start("continuous"))
        self.stop.clicked.connect(self.cancel)
        self.preview.clicked.connect(lambda: self.start("display"))
        self.read_button.clicked.connect(self.read_settings)
        self.apply.clicked.connect(self.apply_settings)
        self.open.clicked.connect(self.open_folder)

    def select(self, key):
        if key == self.key:
            return
        self.generation += 1
        self.key = key
        self.repeat = self.busy = self.stopping = False
        self.snapshot_attempted = False
        self.baseline = {}
        self.draw()
        if key:
            self.status.setText("准备就绪")
        self.update_actions()

    def update_actions(self, *_):
        connected = self.ctrl.states.get(self.key, {}).get("connectionState") == "connected"
        idle = bool(self.key and connected and not self.busy)
        for button in (self.single, self.cont, self.preview, self.read_button):
            button.setEnabled(idle)
        mode_changed = self.att_mode.currentIndex() != int(not self.baseline.get("attenuation_auto", True))
        self.apply.setEnabled(
            idle
            and bool(self.baseline)
            and (mode_changed or any(f.isModified() for f in self.fields.values()))
        )
        self.att_mode.setEnabled(idle and self.baseline.get("attenuation_db") is not None)
        self.stop.setEnabled(self.busy and self.collecting and not self.stopping)
        self.open.setEnabled(self.key in self.last)
        for name, field in self.fields.items():
            allowed = self.baseline.get(name) is not None
            if name in {"rbw_percent", "xcorr"}:
                allowed &= self.baseline.get("sweep_mode", "").startswith("NORM")
            if name == "attenuation_db":
                allowed &= self.att_mode.currentIndex() == 1
            field.setEnabled(idle and allowed)
        if self.key:
            self.selector.setEnabled(not self.busy)
        if idle and self.isVisible() and not self.snapshot_attempted:
            self.snapshot_attempted = True
            QTimer.singleShot(0, self.read_settings)

    def finish(self, text):
        self.repeat = self.busy = self.collecting = self.stopping = False
        self.status.setText(text)
        self.update_actions()

    def fail(self, exc):
        if getattr(exc, "code", "") == "signal_missing":
            self.set_settings(
                self.baseline | {"carrier_missing": True, "carrier_hz": None, "carrier_dbm": None},
                preserve=True,
            )
        self.finish(str(exc))

    def read_settings(self):
        if self.busy or not self.key:
            return
        self.busy = True
        generation = self.generation
        self.update_actions()
        try:
            future = self.ctrl.runtime.sessions.submit(
                self.key, "read_settings", (("phase_noise_settings",), 1)
            )

            def received(rows):
                if generation != self.generation:
                    return
                reading = rows[0]
                if not isinstance(reading.value, dict):
                    raise ValueError(reading.error or "仪器未返回参数")
                self.set_settings(reading.value, preserve=True)
                self.finish("参数已刷新")

            self.ctrl.watch(
                future, received, lambda e: self.fail(e) if generation == self.generation else None
            )
        except Exception as exc:
            self.fail(exc)

    def set_settings(self, values, *, preserve=False):
        old_auto = self.baseline.get("attenuation_auto")
        new_auto = values.get("attenuation_auto", True)
        dirty_mode = old_auto is not None and old_auto != (self.att_mode.currentIndex() == 0)
        self.baseline = values
        if not preserve or not dirty_mode:
            self.att_mode.blockSignals(True)
            self.att_mode.setCurrentIndex(0 if new_auto else 1)
            self.att_mode.blockSignals(False)
        for name, _, unit in METRICS:
            v = values.get(name)
            if name == "estimated_s" and v is not None:
                text = f"约 {v:.1f} s"
            elif name == "carrier_hz" and v is not None and v >= 1e6:
                scale, label = (1e9, "GHz") if v >= 1e9 else (1e6, "MHz")
                text = f"{v / scale:.9g} {label}"
            else:
                text = "—" if v is None else f"{v:,.8g} {unit}"
            self.metrics[name].setText(text)
        for name, field in self.fields.items():
            if preserve and field.isModified():
                continue
            v = values.get(name)
            field.setText("" if v is None else f"{v:.15g}")
            field.setModified(False)

    def apply_settings(self):
        if self.busy:
            return
        try:
            changes = {n: float(f.text()) for n, f in self.fields.items() if f.isEnabled() and f.isModified()}
            changes["attenuation_auto"] = self.att_mode.currentIndex() == 0
            changes = {k: v for k, v in changes.items() if v != self.baseline[k]}
            if not changes:
                self.set_settings(self.baseline)
                self.finish("参数已同步")
                return
            self.busy = True
            self.update_actions()
            generation = self.generation

            def applied(result):
                if generation != self.generation:
                    return
                if result.outcome != "success":
                    self.fail(result.error or "仪器未确认参数")
                    return
                self.set_settings(result.readback)
                self.finish("已应用并读回")

            self.ctrl.apply(self.key, DeviceCommand("phase_noise_settings", changes), applied, self.fail)
        except Exception as exc:
            self.fail(exc)

    def start(self, mode):
        if self.busy or not self.key:
            return
        self.repeat = mode == "continuous"
        self.rounds = 0
        self.stopping = False
        self.busy = self.collecting = True
        self.run_round(mode == "display")

    def run_round(self, display=False):
        key, gen = self.key, self.generation
        self.update_actions()
        try:
            self.ctrl.ensure_applicable(key)
            self.status.setText(
                "正在读取面板结果…" if display else f"第 {self.rounds + 1} 轮 · 等待载波与测量完成"
            )
            request = AcquisitionRequest(
                "phase_noise_display" if display else "phase_noise_round",
                initiate=not display,
                confirmed=not display,
                allow_setup=not display,
            )
            future = self.ctrl.runtime.sessions.start_acquisition(key, request, mode=self.ctrl.mode)

            def captured(paths):
                if gen != self.generation:
                    return
                if not paths:
                    self.finish("已停止" if self.stopping else "本轮未取得曲线")
                    return
                job = self.ctrl.runtime.processing.read_worker.submit(load_round, paths)
                self.ctrl.watch(job, lambda result: self.published(result, gen), self.fail)

            self.ctrl.watch(future, captured, lambda exc: self.fail(exc) if gen == self.generation else None)
        except Exception as exc:
            self.fail(exc)

    def published(self, result, generation):
        if generation != self.generation:
            return
        self.last[self.key] = result
        self.rounds += 1
        self.draw(preserve_drafts=True)
        if self.repeat and not self.stopping:
            self.run_round()
        else:
            message = (
                "已保存面板结果"
                if result[0]["configuration"]["origin"] == "display"
                else f"已完成 {self.rounds} 轮"
            )
            self.finish(("已停止 · " if self.stopping else "") + message)

    def cancel(self):
        self.stopping = True
        self.repeat = False
        self.ctrl.runtime.sessions.stop_acquisition(self.key)
        self.status.setText("正在停止，保留已完成轮次")
        self.update_actions()

    def draw(self, *, preserve_drafts=False):
        self.pn.clear()
        self.adev.clear()
        self.spots.setRowCount(0)
        self.stamp.clear()
        if self.key not in self.last:
            self.set_settings({})
            return
        frame, _ = self.last[self.key]
        cfg = frame["configuration"]
        self.set_settings(cfg["settings"], preserve=preserve_drafts)
        for c in frame["measurements"]:
            x, y, m = c["axis"], c["values"], c["metadata"]
            if c["quantity"] == "adev":
                valid = np.asarray(y) > 0
                self.adev.plot(x[valid], y[valid], pen=pg.mkPen(COLORS[0], width=2))
            else:
                t = m["trace"]
                self.pn.plot(
                    x,
                    y,
                    pen=pg.mkPen(COLORS[(t - 1) % len(COLORS)], width=2),
                    name=f"窗口 {m['window']} · T{t}",
                )
        by_offset: dict[float, dict[tuple[int, int], float]] = {}
        traces = sorted({(s["window"], s["trace"]) for s in cfg["spot_noise"]})
        for spot in cfg["spot_noise"]:
            by_offset.setdefault(spot["offset_hz"], {})[(spot["window"], spot["trace"])] = spot["dbc_hz"]
        self.spots.setColumnCount(1 + len(traces))
        self.spots.setHorizontalHeaderLabels(["频偏 (Hz)"] + [f"W{w}·T{t}" for w, t in traces])
        for row, (offset, levels) in enumerate(sorted(by_offset.items())):
            self.spots.insertRow(row)
            values = [f"{offset:.10g}"] + [f"{levels[t]:.2f}" if t in levels else "—" for t in traces]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
                self.spots.setItem(row, col, item)
        label = "面板已有结果 · 读取于" if cfg["origin"] == "display" else "最近完成轮次"
        stamp = datetime.fromisoformat(frame["timestamp"]).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        self.stamp.setText(f"{label}  {stamp}")

    def open_folder(self):
        if self.key in self.last:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last[self.key][1])))

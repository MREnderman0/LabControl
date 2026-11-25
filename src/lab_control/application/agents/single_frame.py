"""Acquire one authorized source and publish its requested representations."""

from __future__ import annotations

import hashlib
import shutil
import threading
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from lab_control.application.controls import data_sources
from lab_control.domain.frames import immutable_batch, validate_recorded_batch
from lab_control.domain.instruments import DeviceError, ValidationError
from lab_control.instruments.registry import DEVICE_DEFINITIONS
from lab_control.storage.frame_files import directory, filename, publish

# Retain immutable download copies, including when users edit their exported CSV.
# Count existing files so the limit also covers exports from previous versions.
ARCHIVE_BYTES = 2 * 1024**3
ARCHIVE_TASKS = 4096


def check_archive_capacity(root: Path, *, new_task: bool) -> None:
    used = 0
    count = 0
    if root.exists():
        for task_dir in root.iterdir():
            if task_dir.is_symlink() or task_dir.is_junction() or not task_dir.is_dir():
                continue
            count += 1
            for item in task_dir.iterdir():
                if not item.is_symlink() and not item.is_junction() and item.is_file():
                    used += item.stat().st_size
            if used > ARCHIVE_BYTES or count + int(new_task) > ARCHIVE_TASKS:
                raise DeviceError("单帧下载缓存已满，请在采数机归档并清理旧副本", "storage_full")


def supported_modes(source, grant, definition):
    actions = set(grant["actions"])
    defaults = source["request_defaults"]
    if not {"acquire", "results"} <= actions or defaults.get("duration") is not None:
        return []
    if source["kind"] == "status" and "read_parameters" not in actions:
        return []
    if (
        definition.acquisition_requires_confirmation
        or source["kind"] == "waveform"
        or defaults.get("initiate", definition.acquisition_initiate_default)
    ) and "output_trigger" not in actions:
        return []
    if source["kind"] in {"waveform", "spectrum", "trace", "s_parameters"}:
        return ["csv", "png", "both"] if "process" in actions else ["csv"]
    return ["csv"] if source["kind"] in {"scalar", "status"} else []


def render(path, batch, source, check):
    from lab_control.ui.widgets.publication_figure import export_figure

    curves = []
    units = set()
    for m in batch.measurements:
        x, y = np.asarray(m.axis), np.asarray(m.values)
        if np.iscomplexobj(y):
            if source.view == "phase":
                y, unit = np.angle(y, deg=True), "°"
            else:
                with np.errstate(divide="ignore"):
                    y, unit = 20 * np.log10(np.abs(y)), "dB"
        else:
            unit = m.unit
        if (
            x.ndim != 1
            or x.shape != y.shape
            or not m.axis_unit
            or not unit
            or not np.any(np.isfinite(x) & np.isfinite(y))
        ):
            raise DeviceError("采集帧缺少有效曲线、坐标或单位", "acquisition_failed")
        units.add((m.axis_unit, unit))
        curves.append((f"CH{m.channel} {m.quantity}", x, y))
    if len(units) != 1:
        raise ValidationError("所选数据源的曲线单位不一致")
    axis, unit = units.pop()
    export_figure(path, curves, f"X ({axis})", f"{source.label} ({unit})", check_cancel=check)


def start_frame(service, ctx, client, grant, device, spec):
    ctx["frame_export"] = True
    (channel,) = spec["channels"]
    stem = filename(spec["stem"])
    destination, mode = spec["destination"], spec["mode"]
    if destination not in {"labcontrol", "client"}:
        raise ValidationError("未知保存目标")
    export_root = service.repository.data_root
    folder = directory(spec["directory"], export_root) if destination == "labcontrol" else None
    caps = service.get_capabilities(client, grant, device)
    advertised: dict = next((s for s in caps["data_sources"] if s["key"] == spec["source"]), {})
    if (
        mode not in advertised.get("frame_modes", [])
        or channel not in advertised["channel_selection"]["channels"]
        or caps["snapshot"]["stale"]
    ):
        raise ValidationError("所选通道、数据源或保存模式不可用")
    session = service.runtime.sessions.sessions[device]
    source = next(s for s in data_sources(session.config) if s.key == spec["source"])
    definition = DEVICE_DEFINITIONS[session.config.adapter]
    acquisition = source.request((channel,), initiate=definition.acquisition_initiate_default, confirmed=True)
    scope = service._check(client, grant, device, "acquire", channels=(channel,))
    acquisition = replace(
        acquisition, allow_setup=acquisition.allow_setup and "acquisition_setup" in scope["actions"]
    )
    if acquisition.channels != (channel,):
        raise ValidationError("该数据源不支持所选通道")
    acquisition.validate()
    cancel = ctx["cancel"] = threading.Event()

    def check():
        if folder is not None:
            directory(str(folder), export_root)
        if cancel.is_set():
            raise DeviceError("单帧保存已取消", "cancelled")
        scope = service._check(client, grant, device, "acquire", channels=(channel,))
        if mode not in supported_modes(advertised, scope, definition):
            raise DeviceError("保存所需的授权已变更", "permission_denied")
        if acquisition.allow_setup:
            service._check(client, grant, device, "acquisition_setup", channels=(channel,))

    def guard(current):
        check()
        if current is not session or current.control_group:
            raise DeviceError("设备会话已变更或正在修改参数", "task_busy")

    check()
    if folder:
        folder.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=folder):
            pass
    if service._scope_waveform(device, acquisition):
        read = service._single_read(client, grant, device, acquisition, cancel, guard)
    else:
        read = service.runtime.sessions.submit(
            device, "observe" if source.kind == "status" else "acquire", acquisition, before_io=guard
        )

    def save():
        batch = immutable_batch(read.result())
        check()
        validate_recorded_batch(batch)
        if any(m.channel != channel for m in batch.measurements):
            raise DeviceError("采集帧含未选中的通道", "acquisition_failed")
        selected = tuple(
            m for m in batch.measurements if not source.measurement or m.quantity == source.measurement
        )
        if not selected or any(not np.asarray(m.values).size for m in selected):
            raise DeviceError("所选数据源没有有效采样点", "acquisition_failed")
        batch = replace(batch, measurements=selected)
        task = ctx["task"]
        archive = service.repository.root.absolute() / "agent-frame-artifacts" / task["id"]
        directory(str(archive), service.repository.root.absolute())
        with service.frame_archive_lock:
            check_archive_capacity(archive.parent, new_task=True)
            archive.mkdir(parents=True)
        try:
            formats = ("png", "csv") if mode == "both" else (mode,)
            files = []
            for ext in formats:
                check()
                path = archive / f"{stem}.{ext}"
                if ext == "png":
                    render(path, batch, source, check)
                else:
                    service.repository.write_frame_csv(batch, path)
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                aid = service.ledger.fingerprint([task["id"], str(path.resolve())])[:32]
                files.append(dict(format=ext, artifact_id=aid, size=path.stat().st_size, sha256=digest))
            check()

            def register():
                check()
                entries = []
                for info in files:
                    path = archive / f"{stem}.{info['format']}"
                    entries.append(
                        dict(
                            id=info["artifact_id"],
                            task_id=task["id"],
                            device_id=device,
                            path=str(path.resolve()),
                            root=str(archive.resolve()),
                            name=path.name,
                            size=info["size"],
                            channels=[channel],
                        )
                    )
                with service.frame_archive_lock:
                    check_archive_capacity(archive.parent, new_task=False)
                    service.ledger.save_artifacts(entries)

            if folder:
                with TemporaryDirectory(dir=folder, prefix=".frame-") as stage:
                    staged = {ext: Path(stage) / f"{stem}.{ext}" for ext in formats}
                    for path in staged.values():
                        shutil.copyfile(archive / path.name, path)
                    targets = publish(folder, stem, staged, check, register)
                for info in files:
                    info["path"] = str(targets[info["format"]])
            else:
                register()
            return dict(
                operation="save_frame",
                destination=destination,
                stem=stem,
                mode=mode,
                files=files,
                timestamp=batch.timestamp,
                channel=channel,
                source=source.key,
                points=sum(np.asarray(m.values).size for m in selected),
            )
        except BaseException:
            with service.frame_archive_lock:
                shutil.rmtree(archive)
            raise

    ctx["future"] = service.worker.submit(save)

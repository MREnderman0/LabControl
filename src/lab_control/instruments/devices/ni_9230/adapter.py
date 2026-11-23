from __future__ import annotations

import ctypes as ct

import numpy as np

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import (
    Capability,
    CapabilitySet,
    DeviceError,
    DeviceIdentity,
    DeviceStatus,
    Measurement,
    MeasurementBatch,
    ParameterReading,
    Unsupported,
    ValidationError,
)

from .calibration import sensors
from .daqmx import CURRENT_A, HANDLE, I32, MODEL, RATE, Driver

ID = "ni.9230"
CATEGORY = "accelerometer"
CHANNELS = (1, 2, 3)
WINDOW_POINTS = RATE * 2
BASELINE_LIMIT_G = 0.0005
DRIFT_LIMIT_G = 0.00025
WAITING = "输入稳定中"


def stable_baseline(samples):
    """Test signed AC baseline drift while preserving oscillatory acceleration."""
    earlier, later = samples.reshape(2, RATE).mean(axis=1)
    return (
        np.max(np.abs(samples)) < 0.25
        and max(abs(earlier), abs(later)) <= BASELINE_LIMIT_G
        and abs(earlier - later) <= DRIFT_LIMIT_G
    )


def fault_reason(name, faults):
    if name in faults["OpenCurrentLoop"]:
        return "IEPE 开路"
    if name in faults["Overcurrent"]:
        return "IEPE 过流"
    return ""


def capability_schema(config):
    channel = Capability(
        "channel",
        readable=True,
        channel_scoped=True,
        source="NI-DAQmx C API",
        section="DAQmxGetPhysicalChanAITermCfgs",
    )
    return CapabilitySet(CATEGORY, CHANNELS, (channel,))


class Discovery:
    name = "ni_daqmx"

    def enumerate(self):
        from lab_control.discovery.service import DiscoveryCandidate

        try:
            modules = Driver().modules()
        except DeviceError as exc:
            if exc.code == "driver_missing":
                return ()
            raise
        return tuple(
            DiscoveryCandidate(
                ConnectionConfig("NIDAQMX::" + serial, ID, "nidaqmx", timeout=60, expected_serial=serial),
                self.name,
                {"display_name": MODEL, "module": name},
            )
            for serial, name in modules.items()
        )


class Adapter:
    adapter_id = ID

    def __init__(self, config):
        if config.transport != "nidaqmx" or not config.resource.startswith("NIDAQMX::"):
            raise ValidationError("Expected NIDAQMX::<module serial>")
        self.config = config
        self.sensors = sensors(config.options)
        self.capability_details = {"physical_inputs": {str(ch): f"AI{ch - 1}" for ch in CHANNELS}}
        self.driver = Driver()
        self.handle = HANDLE()
        self.identity = None
        self.inputs = ()
        self.names = ()
        self.states = {}
        self.valid_from = {}
        self.stabilized_after = {}
        self.last_scan_index = -1

    def identify(self):
        serial = self.config.resource.removeprefix("NIDAQMX::")
        modules = self.driver.modules()
        if serial not in modules or (self.config.expected_serial and self.config.expected_serial != serial):
            raise DeviceError("NI 9230 module serial mismatch or missing", "identity_conflict")
        self.module = modules[serial]
        self.identity = DeviceIdentity("National Instruments", MODEL, serial)
        return self.identity

    def capabilities(self):
        return capability_schema(self.config)

    def read_status(self):
        return DeviceStatus(
            {
                "module": self.module,
                "physical_channels": [f"ai{i - 1}" for i in CHANNELS],
                "sensors": {f"ai{ch - 1}": s.metadata() for ch, s in self.sensors.items()},
            }
        )

    def read_settings(self, operations, channel=1):
        if channel not in CHANNELS:
            raise ValidationError("Invalid NI 9230 input")
        readings = []
        for operation in operations:
            self.capabilities().get(operation)
            name = f"{self.module}/ai{channel - 1}"
            mask = self.driver.number("GetPhysicalChanAITermCfgs", I32, name.encode())
            readings.append(
                ParameterReading(
                    operation,
                    channel,
                    {"physical_channel": name, "terminal_configuration_mask": mask},
                    source="instrument",
                )
            )
        return tuple(readings)

    def apply(self, command):
        raise Unsupported("No persistent settings exposed by NI 9230")

    def prepare_monitor_input(self, request):
        request.validate()
        if (
            request.kind != "scalar"
            or request.quantity != "acceleration"
            or request.points != 1
            or not set(request.channels) <= set(CHANNELS)
            or request.duration is not None
            or request.sample_rate is not None
        ):
            raise ValidationError("请选择加速度单点监测和有效通道")
        if not (request.initiate and request.confirmed and request.allow_setup):
            raise DeviceError("Explicit capture required to energize IEPE inputs", "confirmation_required")
        if self.inputs != request.channels:
            self.close()
            self.identify()
            self.driver.call("CreateTask", b"", ct.byref(self.handle))
            self.names = tuple(f"{self.module}/ai{ch - 1}" for ch in request.channels)
            try:
                self.driver.start_input(
                    self.module,
                    request.channels,
                    self.handle,
                    [self.sensors[ch].sensitivity for ch in request.channels],
                )
            except Exception:
                self.close()
                raise
            self.inputs = request.channels
            self.states = dict.fromkeys(self.inputs, WAITING)
            self.valid_from = dict.fromkeys(self.inputs, 0)
        if len(self.stabilized_after) == len(self.inputs):
            return
        window = self.driver.read_window(self.handle, len(self.inputs), WINDOW_POINTS)
        if window is None:
            raise DeviceError("正在检查各路 IEPE 输入的上电稳定性", "monitor_wait")
        end, rows = window
        faults = self.driver.faults(self.handle)
        for ch, name, row in zip(self.inputs, self.names, rows, strict=True):
            reason = fault_reason(name, faults)
            if reason:
                self.states[ch] = reason
                self.valid_from[ch] = end + 1
                self.stabilized_after.pop(ch, None)
            elif ch not in self.stabilized_after:
                self.states[ch] = WAITING
                if end + 1 - WINDOW_POINTS >= self.valid_from[ch] and stable_baseline(row):
                    self.stabilized_after[ch] = (end + 1) / RATE
                    self.states[ch] = "正常"
        if not self.stabilized_after:
            pending = any(state == WAITING for state in self.states.values())
            details = " · ".join(f"AI{ch - 1} {state}" for ch, state in self.states.items())
            raise DeviceError(details, "monitor_wait" if pending else "monitor_no_signal")

    def acquire(self, request):
        if (
            not self.handle.value
            or request.kind != "scalar"
            or request.quantity != "acceleration"
            or request.points != 1
            or not request.context_id
            or not request.confirmed
            or self.inputs != request.channels
            or not self.stabilized_after
        ):
            raise Unsupported("请从连续监测读取加速度")
        scan_index, rows = self.driver.read_point(self.handle, len(self.inputs))
        if scan_index <= self.last_scan_index:
            raise DeviceError("采样序号未更新，等待新的同步采样点", "monitor_no_signal")
        self.last_scan_index = scan_index
        faults = self.driver.faults(self.handle)
        states, measurements = {}, []
        for channel, name, row in zip(self.inputs, self.names, rows, strict=True):
            reason = fault_reason(name, faults)
            if reason:
                self.states[channel] = state = reason
                self.valid_from[channel] = scan_index + 1
                self.stabilized_after.pop(channel, None)
            elif channel not in self.stabilized_after:
                state = self.states[channel]
            elif abs(row[0]) >= 0.25:
                state = "超量程"
            else:
                state = "正常"
                measurements.append(
                    Measurement(
                        "acceleration",
                        "g",
                        row,
                        channel,
                        metadata={
                            "physical_channel": name,
                            "scan_index": scan_index,
                            "stabilized_after_s": self.stabilized_after[channel],
                            **self.sensors[channel].metadata(),
                        },
                    )
                )
            states[f"ai{channel - 1}"] = state
        if not measurements:
            raise DeviceError(
                "；".join(f"{key.upper()} {state}" for key, state in states.items()), "monitor_no_signal"
            )
        return MeasurementBatch(
            self.identity,
            tuple(measurements),
            {
                "sample_rate_Hz": RATE,
                "coupling": "AC",
                "current_A": CURRENT_A,
                "sensors": {f"ai{ch - 1}": self.sensors[ch].metadata() for ch in self.inputs},
                "channel_status": states,
                "sampling": "latest_synchronized_point",
                "startup_window_s": WINDOW_POINTS / RATE,
            },
            time_basis="host_utc_at_read; latest synchronous DAQmx scan",
            validation="full",
        )

    def end_monitor_input(self):
        self.close()

    def close(self):
        self.inputs = ()
        self.last_scan_index = -1
        if self.handle.value:
            self.driver.call("ClearTask", self.handle)
            self.handle = HANDLE()
        if self.names:
            self.driver.disable_excitation(self.names)
            self.names = ()
        self.states.clear()
        self.valid_from.clear()
        self.stabilized_after.clear()

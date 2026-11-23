"""Narrow NI-DAQmx C API for the NI 9230 (BNC). Importing it never loads a DLL."""

from __future__ import annotations

import ctypes as ct
import os
from pathlib import Path

import numpy as np

from lab_control.domain.instruments import DeviceError

MODEL = "NI 9230 (BNC)"
RATE = 3200
SENSITIVITY = 20000.0  # CT12000L nominal, mV/g
CURRENT_A = 0.004

# NI-DAQmx ANSI C header, NIDAQmx.h.
I32, U32, U64, F64 = ct.c_int32, ct.c_uint32, ct.c_uint64, ct.c_double
HANDLE, STR = ct.c_void_p, ct.c_char_p
PTR = ct.POINTER
AC, INTERNAL, G, MV_PER_G = 10045, 10200, 10186, 12509
RISING, CONTINUOUS, COMMIT = 10280, 10123, 3
VOLTS, CURRENT = 10348, 10134
OVERWRITE_UNREAD, MOST_RECENT_SAMPLE = 10252, 10428
ARGS = {
    "CreateAIVoltageChan": [HANDLE, STR, STR, I32, F64, F64, I32, STR],
    "SetAICoupling": [HANDLE, STR, I32],
    "SetAIExcitVoltageOrCurrent": [HANDLE, STR, I32],
    "SetAIExcitSrc": [HANDLE, STR, I32],
    "SetAIExcitVal": [HANDLE, STR, F64],
    "GetReadTotalSampPerChanAcquired": [HANDLE, PTR(U64)],
    "SetAIUsbXferReqSize": [HANDLE, STR, U32],
    "GetReadCurrReadPos": [HANDLE, PTR(U64)],
    "SetReadRelativeTo": [HANDLE, I32],
    "SetReadOffset": [HANDLE, I32],
    "SetReadOverWrite": [HANDLE, I32],
    "GetPhysicalChanAITermCfgs": [STR, PTR(I32)],
    "GetSysDevNames": [STR, U32],
    "GetDevProductType": [STR, STR, U32],
    "GetDevIsSimulated": [STR, PTR(U32)],
    "GetDevSerialNum": [STR, PTR(U32)],
    "GetExtendedErrorInfo": [STR, U32],
    "CreateTask": [STR, PTR(HANDLE)],
    "CreateAIAccelChan": [HANDLE, STR, STR, I32, F64, F64, I32, F64, I32, I32, F64, STR],
    "CfgSampClkTiming": [HANDLE, STR, F64, I32, I32, U64],
    "TaskControl": [HANDLE, I32],
    "GetSampClkRate": [HANDLE, PTR(F64)],
    "GetAIExcitVal": [HANDLE, STR, PTR(F64)],
    "GetAIAccelSensitivity": [HANDLE, STR, PTR(F64)],
    "GetAICoupling": [HANDLE, STR, PTR(I32)],
    "StartTask": [HANDLE],
    "ReadAnalogF64": [HANDLE, I32, F64, U32, PTR(F64), U32, PTR(I32), PTR(U32)],
    "GetReadOpenCurrentLoopChansExist": [HANDLE, PTR(U32)],
    "GetReadOpenCurrentLoopChans": [HANDLE, STR, U32],
    "GetReadOvercurrentChansExist": [HANDLE, PTR(U32)],
    "GetReadOvercurrentChans": [HANDLE, STR, U32],
    "ClearTask": [HANDLE],
}


class Driver:
    def __init__(self):
        try:
            dll = Path(os.environ["SystemRoot"]) / "System32" / "nicaiu.dll"
            self.dll = ct.WinDLL(str(dll))
            for name, args in ARGS.items():
                function = getattr(self.dll, "DAQmx" + name)
                function.argtypes, function.restype = args, I32
        except (OSError, KeyError, AttributeError) as exc:
            raise DeviceError(f"NI-DAQmx 64-bit runtime unavailable: {exc}", "driver_missing") from exc

    def call(self, name, *args):
        status = getattr(self.dll, "DAQmx" + name)(*args)
        if status < 0:
            message = ct.create_string_buffer(4096)
            self.dll.DAQmxGetExtendedErrorInfo(message, len(message))
            raise DeviceError(
                f"DAQmx{name} ({status}): {message.value.decode(errors='replace')}", "sdk_error"
            )

    def names(self, name, *args):
        buf = ct.create_string_buffer(16384)
        self.call(name, *args, buf, len(buf))
        return tuple(s.strip() for s in buf.value.decode().split(",") if s.strip())

    def number(self, name, dtype, *args):
        value = dtype()
        self.call(name, *args, ct.byref(value))
        return value.value

    def modules(self):
        found = {}
        for device in self.names("GetSysDevNames"):
            name = device.encode()
            if self.names("GetDevProductType", name) != (MODEL,) or self.number(
                "GetDevIsSimulated", U32, name
            ):
                continue
            number = self.number("GetDevSerialNum", U32, name)
            if number:
                serial = f"{number:08X}"
                if serial in found:
                    raise DeviceError(f"Duplicate module serial {serial}", "identity_conflict")
                found[serial] = device
        return found

    def faults(self, handle):
        faults = {}
        for kind in ("OpenCurrentLoop", "Overcurrent"):
            prop = "GetRead" + kind + "Chans"
            faults[kind] = list(self.names(prop, handle)) if self.number(prop + "Exist", U32, handle) else []
        return faults

    def _read(self, handle, count, points):
        data = np.empty((count, points), dtype=np.float64)
        read = I32()
        self.call(
            "ReadAnalogF64",
            handle,
            points,
            points / RATE + 5,
            0,
            data.ctypes.data_as(PTR(F64)),
            data.size,
            ct.byref(read),
            None,
        )
        if read.value != points or not np.isfinite(data).all():
            raise DeviceError("Incomplete or non-finite NI 9230 frame", "invalid_frame")
        scan_index = self.number("GetReadCurrReadPos", U64, handle) - 1
        return scan_index, data

    def read_point(self, handle, count):
        return self._read(handle, count, 1)

    def read_window(self, handle, count, points):
        """Look back over completed scans, then restore the fresh single-point read position."""
        available = self.number("GetReadTotalSampPerChanAcquired", U64, handle)
        if available < points:
            return None
        self.call("SetReadOffset", handle, -points)
        try:
            return self._read(handle, count, points)
        finally:
            self.call("SetReadOffset", handle, 0)

    def start_input(self, device, channels, handle, gains):
        names = tuple(f"{device}/ai{c - 1}" for c in channels)
        for name, gain in zip(names, gains, strict=True):
            self.call(
                "CreateAIAccelChan",
                handle,
                name.encode(),
                b"",
                -1,
                -0.25,
                0.25,
                G,
                gain,
                MV_PER_G,
                INTERNAL,
                CURRENT_A,
                None,
            )
        self.call("CfgSampClkTiming", handle, b"", RATE, RISING, CONTINUOUS, RATE * 4)
        # Deliver short USB blocks even with only one active input.
        self.call("SetAIUsbXferReqSize", handle, b"", 1024)
        # Offset zero waits for a new scan beyond the latest transfer.
        # This prevents consecutive ticks from re-reading the same host buffer entry.
        self.call("SetReadOverWrite", handle, OVERWRITE_UNREAD)
        self.call("SetReadRelativeTo", handle, MOST_RECENT_SAMPLE)
        self.call("SetReadOffset", handle, 0)
        self.call("TaskControl", handle, COMMIT)
        rate = self.number("GetSampClkRate", F64, handle)
        for name, gain in zip(names, gains, strict=True):
            key = name.encode()
            if (
                self.number("GetAICoupling", I32, handle, key) != AC
                or not np.isclose(self.number("GetAIExcitVal", F64, handle, key), CURRENT_A)
                or not np.isclose(self.number("GetAIAccelSensitivity", F64, handle, key), gain)
            ):
                raise DeviceError(f"Unexpected IEPE readback on {name}", "readback_mismatch")
        if not np.isclose(rate, RATE):
            raise DeviceError(f"Unexpected sample-rate readback {rate}", "readback_mismatch")
        self.call("StartTask", handle)
        return names

    def disable_excitation(self, names):
        """Clearing a DSA acquisition task can leave its excitation enabled."""
        handle = HANDLE()
        self.call("CreateTask", b"", ct.byref(handle))
        try:
            for name in names:
                key = name.encode()
                self.call("CreateAIVoltageChan", handle, key, b"", -1, -30, 30, VOLTS, None)
                self.call("SetAICoupling", handle, key, AC)
                self.call("SetAIExcitVoltageOrCurrent", handle, key, CURRENT)
                self.call("SetAIExcitSrc", handle, key, INTERNAL)
                self.call("SetAIExcitVal", handle, key, 0.0)
            self.call("CfgSampClkTiming", handle, b"", RATE, RISING, CONTINUOUS, RATE)
            self.call("TaskControl", handle, COMMIT)
            self.call("StartTask", handle)
            for name in names:
                if self.number("GetAIExcitVal", F64, handle, name.encode()) != 0:
                    raise DeviceError(f"{name} 的 0 mA 激励读回异常", "readback_mismatch")
        finally:
            self.call("ClearTask", handle)

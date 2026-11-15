"""Single-arm protocols shared by compatible SCPI oscilloscopes."""

import time
from contextlib import contextmanager

from lab_control.domain.instruments import DeviceError


def token(adapter, command):
    return adapter.transport.query(command).decode("ascii").strip().upper().split()[-1]


class ScpiSingle:
    def single_span(self):
        return float(self.read("timebase")) * self.single_divisions

    def single_count(self, channels):
        return 1

    @contextmanager
    def single_acquisition(self, job, request):
        span, count = self.single_span(), self.single_count(request.channels)
        initial = self.single_initial()
        self._before_single = initial
        job.check()
        success = False
        try:
            complete = self.single_arm()
            job.wait(complete, span, count)
            yield span, False
            success = True
        finally:
            if not success:
                self.single_abort()
            if not success or job.restore:
                self.single_restore(initial)

    def single_abort(self):
        pass


class RohdeSingle(ScpiSingle):
    def ensure_continuous(self):
        if not self.single_initial():
            self.single_restore(True)
        return self.continuous_frame_ready

    def single_abort(self):
        self.transport.write("STOP")

    def single_span(self):
        return float(token(self, "TIM:RANG?"))

    def single_count(self, channels):
        return int(self.read("acquisition_count"))

    def single_restore(self, original):
        self.transport.write("RUN" if original else "STOP")
        deadline = time.monotonic() + 2.0
        while self.single_initial() != original:
            if time.monotonic() >= deadline:
                raise DeviceError("示波器运行状态恢复失败", "restoration_failed")
            time.sleep(0.025)

    def single_arm(self):
        # Consume any old OPC event before attaching a new one to this one Single.
        token(self, "*ESR?")
        self.transport.write("SING;*OPC")
        completed = False

        def done():
            nonlocal completed
            status = int(token(self, "*ESR?"))
            if status & 60:
                raise DeviceError("Single 指令出错：" + token(self, "SYST:ERR?"), "acquisition_failed")
            completed |= bool(status & 1)
            return completed and self.single_finished()

        return done


class RigolSingle(ScpiSingle):
    def continuous_frame_ready(self):
        return token(self, ":TRIG:STAT?") in {"TD", "AUTO"}

    def ensure_continuous(self):
        status = token(self, ":TRIG:STAT?")
        sweep = token(self, ":TRIG:SWE?")
        if sweep.startswith("SING"):
            previous = getattr(self, "_before_single", (False, "AUTO"))[1]
            sweep = "NORM" if previous.startswith("NORM") else "AUTO"
            self.transport.write(":TRIG:SWE " + sweep)
        elif sweep not in {"AUTO", "NORM", "NORMAL"}:
            raise DeviceError("无法确认连续触发模式：" + sweep, "acquisition_setup_blocked")
        elif status != "STOP":
            return self.continuous_frame_ready if status in {"WAIT", "RUN"} else None
        self.transport.write(":RUN")
        running, actual = self.single_initial()
        if not running or actual.startswith("SING"):
            raise DeviceError("示波器未进入连续采集", "acquisition_setup_blocked")
        return self.continuous_frame_ready

    def single_count(self, channels):
        mode = token(self, ":ACQ:TYPE?")
        return int(float(token(self, ":ACQ:AVER?"))) if mode.startswith("AVER") else 1

    def single_initial(self):
        sweep = token(self, ":TRIG:SWE?")
        running = token(self, ":TRIG:STAT?") != "STOP"
        if running and sweep.startswith("SING"):
            raise DeviceError("仪器已有一次触发正在等待", "task_busy")
        if sweep not in {"AUTO", "NORM", "NORMAL", "SING", "SINGLE"}:
            raise DeviceError("无法识别当前触发模式", "invalid_parameter")
        return running, sweep

    def single_arm(self):
        self.transport.write(":SING")
        if not token(self, ":TRIG:SWE?").startswith("SING") or not self.transport.query(":SYST:ERR?").lstrip(
            b"+"
        ).startswith(b"0,"):
            raise DeviceError("仪器未接受 Single", "acquisition_failed")
        return lambda: token(self, ":TRIG:STAT?") == "STOP"

    def single_restore(self, initial):
        running, sweep = initial
        if token(self, ":TRIG:SWE?") != sweep:
            self.transport.write(":TRIG:SWE " + sweep)
        self.transport.write(":RUN" if running else ":STOP")
        if self.single_initial() != initial:
            raise DeviceError("示波器触发模式或运行状态恢复失败", "restoration_failed")


class LecroySingle(ScpiSingle):
    def ensure_continuous(self):
        mode = token(self, "TRMD?")
        if mode in {"NORM", "AUTO"}:
            return (lambda: bool(int(token(self, "INR?")) & 1)) if mode == "NORM" else None
        if mode not in {"STOP", "SINGLE"}:
            raise DeviceError("无法确认示波器运行状态：" + mode, "acquisition_setup_blocked")
        token(self, "INR?")
        previous = getattr(self, "_before_single", "AUTO")
        self.single_restore(previous if previous in {"AUTO", "NORM"} else "AUTO")
        return lambda: bool(int(token(self, "INR?")) & 1)

    single_divisions = 10

    def single_count(self, channels):
        # MAUI Acquisition.Cx.AverageSweeps is the per-channel averaging depth.
        return max(int(token(self, f"VBS? 'return=app.Acquisition.C{ch}.AverageSweeps'")) for ch in channels)

    def single_initial(self):
        mode = token(self, "TRMD?")
        if mode not in {"AUTO", "NORM", "STOP"}:
            raise DeviceError("已有单次触发或无法识别触发模式", "task_busy")
        return mode

    def single_arm(self):
        # Stop the preceding session before clearing INR, so bit 0 belongs to this arm.
        self.transport.write("TRMD STOP")
        if token(self, "TRMD?") != "STOP":
            raise DeviceError("上一次采集仍在运行", "task_busy")
        token(self, "INR?")
        self.transport.write("TRMD SINGLE")
        acquired = False

        def done():
            nonlocal acquired
            acquired |= bool(int(token(self, "INR?")) & 1)
            return acquired and token(self, "TRMD?") == "STOP"

        return done

    def single_restore(self, original):
        self.transport.write("TRMD " + original)
        if token(self, "TRMD?") != original:
            raise DeviceError("示波器触发模式恢复失败", "restoration_failed")

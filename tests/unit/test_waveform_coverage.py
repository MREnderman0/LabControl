from dataclasses import replace
from threading import Event

import numpy as np
import pytest

from lab_control.domain.instruments import AcquisitionRequest, DeviceError, Measurement
from lab_control.domain.oscilloscope import ScopeDisplay
from lab_control.domain.waveform_coverage import coverage
from lab_control.instruments.scope_single import SingleCapture


def test_rtm_screen_edges_are_aligned_to_transfer_grid():
    x = -29.998404 + np.arange(130712) * 0.000459
    meta = {
        "scope_display": ScopeDisplay(1, 0, 5, 0, horizontal_divisions=12).metadata(),
        "headers_observed": [{"points": len(x)}, {"points": len(x)}],
    }
    m = Measurement("voltage", "V", x * 0, axis=x, axis_unit="s", metadata=meta)
    assert not coverage(m)["partial"]
    assert coverage(replace(m, metadata={**meta, "growing_prefix": True}))["partial"]
    assert coverage(replace(m, axis=x[:-10], values=m.values[:-10]))["partial"]
    assert coverage(replace(m, axis=x[20:], values=m.values[20:]))["partial"]


def test_wait_for_this_trigger_honours_cancel_and_timeout(monkeypatch):
    import lab_control.instruments.scope_single as module

    job = SingleCapture(AcquisitionRequest("waveform", (1,), confirmed=True), cancel=Event())
    times = iter((0, 91))
    monkeypatch.setattr(module.time, "monotonic", lambda: next(times))
    with pytest.raises(DeviceError) as timeout:
        job.wait(lambda: False, 30, 2)
    assert timeout.value.code == "capture_timeout"
    job.cancel.set()
    with pytest.raises(DeviceError) as cancelled:
        job.check()
    assert cancelled.value.code == "cancelled"

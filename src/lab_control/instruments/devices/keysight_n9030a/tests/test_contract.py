import numpy as np
import pytest

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError
from lab_control.instruments.devices.keysight_n9030a import DEFINITION
from lab_control.instruments.devices.keysight_n9030a.adapter import FRAME_SETTINGS, N9030aAdapter
from lab_control.instruments.devices.keysight_n9030a.hardware.readonly import capture_request
from lab_control.instruments.devices.keysight_n9030a.profile import ADAPTER_ID, TRACE_QUERY, n9030a
from lab_control.transport.base import MockTransport, ieee_block

ID = ("*IDN?", b"Agilent Technologies,N9030A,TEST-FIXTURE,A.14.59\n")
SETTINGS = {
    "measurement_mode": "SANalyzer",
    "start_frequency": 1e5,
    "stop_frequency": 5e5,
    "center_frequency": 3e5,
    "span": 4e5,
    "sweep_time": 0.5,
    "sweep_points": 4,
    "amplitude_unit": "DBM",
    "transfer_format": "ASC",
    "byte_order": "NORM",
}
XY = np.array([[1e5, -76], [1.5e5, -47], [3e5, -39], [5e5, -80]])


def script(state):
    return [
        (n9030a().commands[name].query, str(state[name]).encode())
        for name in ("measurement_mode", *FRAME_SETTINGS)
    ]


def instrument(exchanges):
    transport = MockTransport(exchanges)
    transport.open()
    return N9030aAdapter(transport, n9030a(), ConnectionConfig("mock://pxa", ADAPTER_ID))


def acquire(state, raw, *, after=None):
    device = instrument([ID, *script(state), (TRACE_QUERY, raw), *script(state if after is None else after)])
    device.identify()
    return device.acquire(capture_request(device))


@pytest.mark.parametrize(
    "form, order, dtype",
    [
        ("ASCII", "NORM", ""),
        ("ASC,0", "NORM", ""),
        ("REAL,32", "NORM", ">f4"),
        ("REAL,64", "SWAP", "<f8"),
    ],
)
def test_trace_capture_uses_actual_xy_and_format(form, order, dtype):
    raw = ieee_block(XY.astype(dtype).tobytes()) if dtype else ",".join(map(str, XY.flatten())).encode()
    batch = acquire({**SETTINGS, "transfer_format": form, "byte_order": order}, raw)
    (measurement,) = batch.measurements
    np.testing.assert_array_equal(measurement.axis, XY[:, 0])
    np.testing.assert_array_equal(measurement.values, XY[:, 1])
    assert (measurement.unit, measurement.axis_unit, batch.validation) == ("dBm", "Hz", "full")


def test_zero_span_and_selected_y_unit():
    state = {**SETTINGS, "start_frequency": 3e5, "stop_frequency": 3e5, "span": 0, "amplitude_unit": "V"}
    batch = acquire(state, b"0,0.04,0.15,0.02,0.3,0.01,0.5,0.03\n")
    assert (batch.measurements[0].axis_unit, batch.measurements[0].unit) == ("s", "V")


@pytest.mark.parametrize(
    "raw",
    [
        b"1,3,2,4",
        b"1,2,3",
        b"1,0,1,0,2,0,3,0",
        b"1,nan,2,0,3,0,4,0",
        b"1,9.91e37,2,0,3,0,4,0",
        b"#14xxxx",
        b"1,0,2,x,3,0,4,0",
    ],
)
def test_invalid_ascii_frame_never_published(raw):
    with pytest.raises(DeviceError):
        acquire(SETTINGS, raw)


def test_reject_corrupt_binary_and_configuration_transition():
    with pytest.raises(DeviceError, match="Truncated"):
        acquire({**SETTINGS, "transfer_format": "REAL,32"}, b"#232short")
    with pytest.raises(DeviceError) as exc:
        acquire(SETTINGS, b"100000,-69,200000,-59,300000,-63,500000,-73", after={**SETTINGS, "span": 8e5})
    assert exc.value.code == "configuration_changed"


def test_capture_timeout_is_not_retried():
    with pytest.raises(DeviceError) as exc:
        acquire(SETTINGS, DeviceError("Timed out", "timeout"))
    assert exc.value.code == "timeout"


@pytest.mark.parametrize(
    "response",
    [
        b"Keysight Technologies,N9030B,FIXTURE,A.0",
        b"Other Maker,N9030A,FIXTURE,A.0",
    ],
)
def test_exact_model_and_vendor_required(response):
    with pytest.raises(DeviceError) as exc:
        instrument([("*IDN?", response)]).identify()
    assert exc.value.code == "identity_conflict"


def test_other_measurement_channel_trigger_and_write_do_not_touch_analyzer():
    device = instrument([("*IDN?", b"Keysight Technologies,N9030A,TEST,A.0"), ("CONF?", b"CHPower")])
    assert device.identify().model == "N9030A" and DEFINITION.category == "spectrum_analyzer"
    for request in (
        AcquisitionRequest(kind="spectrum", channels=(2,)),
        AcquisitionRequest(kind="spectrum", initiate=True, confirmed=True),
    ):
        with pytest.raises(DeviceError):
            device.acquire(request)
    with pytest.raises(DeviceError):
        device.apply(DeviceCommand("span", {"value": 99}))
    with pytest.raises(DeviceError) as exc:
        device.acquire(capture_request(device))
    assert exc.value.code == "acquisition_setup_blocked"
    assert device.transport.history == ["*IDN?", "CONF?"]

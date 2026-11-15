import json
import time

import numpy as np
import pytest

from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, Unsupported
from lab_control.instruments.devices.rs_fph.adapter import FphAdapter
from lab_control.instruments.devices.rs_fph.hardware.trace_readonly import capture_request
from lab_control.instruments.devices.rs_fph.hardware.validate_control import frame_record
from lab_control.instruments.devices.rs_fph.profile import fph

from .test_protocol import context, raw_trace


class StateTransport:
    resource = "mock://fph-session"

    def __init__(self, firmware="V1.40", product="1321.1111K02"):
        self.product = product
        self.firmware = firmware
        self.options = b"0"
        self.state = context(byte_order="SWAP")
        self.history = []

    def open(self):
        pass

    def close(self):
        pass

    def write(self, payload):
        raise AssertionError(f"Unexpected setting {payload}")

    def read(self):
        raise AssertionError("Use query")

    def query(self, payload):
        self.history.append(payload)
        extra = {
            "*IDN?": f"Rohde&Schwarz,FPH,{self.product}/TEST,{self.firmware}".encode(),
            "*OPT?": self.options,
            "SYST:ERR?": b'0,"No error"',
        }
        if payload in extra:
            return extra[payload]
        if payload == "TRAC:DATA? TRACE1":
            return raw_trace(self.state)
        operation = next(op for op, spec in fph().commands.items() if spec.query == payload)
        value = self.state[operation]
        return str(int(value) if isinstance(value, bool) else value).encode()


def test_factory_session_monitoring_and_metadata_export(tmp_path):
    config = ConnectionConfig("mock://fph-session", "rs.fph", "mock")
    transport = StateTransport()
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as app:
        app.sessions.factory = lambda _: FphAdapter(transport, fph(), config)
        key = app.add(config)
        app.connect(key, configure_acquisition=False).result(3)
        batch = app.sessions.submit(key, "acquire", AcquisitionRequest("spectrum")).result(3)
        record = frame_record(batch)
        assert record["points"] == 711 and record["axis_unit"] == "Hz"
        app.monitoring.start(key, (AcquisitionRequest("spectrum"),), cadence="fastest", record_interval=None)
        deadline = time.monotonic() + 3
        while app.monitoring.tasks[key].sequence < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        app.monitoring.stop(key).result(3)
        assert app.monitoring.tasks[key].sequence >= 3
        assert not app.monitoring.tasks[key].error
        assert app.monitoring.latest(key, "spectrum").batch.validation == "cached"
        assert not (app.repository.root / "data").exists()


def test_real_adapter_stream_binary_and_csv_preserve_every_frame(tmp_path):
    from pathlib import Path

    from lab_control.storage.recording import read_frames

    config = ConnectionConfig("mock://fph-session", "rs.fph", "mock")
    transport = StateTransport()
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as app:
        app.sessions.factory = lambda _: FphAdapter(transport, fph(), config)
        key = app.add(config)
        app.connect(key, configure_acquisition=False).result(3)
        transport.history.clear()
        paths = app.sessions.start_acquisition(
            key, AcquisitionRequest("spectrum"), cadence="fastest", count=4, record_interval=None
        ).result(15)
        frames = [batch for path in paths for batch in read_frames(Path(path))]
        assert len(frames) == 4
        assert [b["validation"] for b in frames] == ["full", "cached", "cached", "cached"]
        assert all(len(b["measurements"][0]["values"]) == 711 for b in frames)
        assert all(b["settings_timestamps"] == frames[0]["settings_timestamps"] for b in frames)
        assert transport.history.count("TRAC:DATA? TRACE1") == 4
        assert len(transport.history) == 52
        assert list(tmp_path.rglob("*.csv"))
        assert not app.sessions.snapshots()[0]["controlsLocked"]


@pytest.mark.parametrize(
    ("transfer_format", "order", "zero_span"),
    [("ASC", "NORM", False), ("REAL", "NORM", False), ("REAL", "SWAP", True)],
)
def test_v240_default_connect_capture_save_and_reconnect_are_query_only(
    tmp_path, transfer_format, order, zero_span
):
    config = ConnectionConfig("mock://fph-session", "rs.fph", "mock")
    transport = StateTransport("V2.40")
    transport.state.update(transfer_format=transfer_format, byte_order=order)
    if zero_span:
        transport.state.update(start_frequency=100e6, stop_frequency=100e6, span=0, sweep_time=0.1)

    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as app:
        app.sessions.factory = lambda _: FphAdapter(transport, fph(), config)
        key = app.add(config)
        assert app.connect(key).result(5).firmware == "V2.40"
        adapter = app.sessions.sessions[key].adapter
        (preamp,) = adapter.read_settings(("preamplifier",))
        assert preamp.value is None and preamp.source == "unavailable" and "B22" in preamp.error
        assert adapter.capabilities().get("reference_level").writable
        assert adapter.capabilities().get("attenuation").writable
        request = capture_request(adapter)
        assert not request.initiate and not request.allow_setup
        folder = app.monitoring.capture_and_export(key, request, "electronic").result(10)
        app.sessions.disconnect(key)
        assert app.connect(key).result(5).firmware == "V2.40"

    snapshot = next(folder.rglob("snapshot.json"))
    metadata = json.loads(snapshot.read_text(encoding="utf-8"))
    (sample,) = metadata["measurements"]
    assert metadata["identity"]["firmware"] == "V2.40"
    assert metadata["configuration"]["preamplifier"] is None
    assert "B22" in metadata["configuration"]["preamplifier_status"]
    assert "INP:GAIN:STAT?" not in transport.history
    assert sample["unit"] == "dBm"
    assert sample["axis_unit"] == ("s" if zero_span else "Hz")
    with np.load(snapshot.with_name("samples.npz"), allow_pickle=False) as arrays:
        expected_axis = np.linspace(0, 0.1, 711) if zero_span else np.linspace(99e6, 101e6, 711)
        np.testing.assert_array_equal(arrays[sample["axis_ref"]], expected_axis)
        np.testing.assert_allclose(arrays[sample["values_ref"]], np.linspace(-100, -20, 711), atol=1e-5)
    assert transport.history.count("TRAC:DATA? TRACE1") == 1
    assert "SYST:ERR?" not in transport.history


def test_v240_installed_b22_keeps_preamp_readback_in_trace(tmp_path):
    config = ConnectionConfig("mock://fph-session", "rs.fph", "mock")
    transport = StateTransport("V2.40")
    transport.options = b"B3,B22"
    transport.state.update(preamplifier=True)
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as app:
        app.sessions.factory = lambda _: FphAdapter(transport, fph(), config)
        key = app.add(config)
        app.connect(key).result(5)
        batch = app.sessions.submit(key, "acquire", AcquisitionRequest("spectrum")).result(5)
    assert batch.configuration["preamplifier"] is True
    assert "preamplifier_status" not in batch.configuration
    assert transport.history.count("INP:GAIN:STAT?") == 2


@pytest.mark.parametrize("product,firmware", [("1321.1111K13", "V2.40"), ("1321.1111K130", "V2.33")])
def test_unqualified_variant_does_not_connect(product, firmware):
    adapter = FphAdapter(
        StateTransport(firmware, product), fph(), ConnectionConfig("mock://fph", "rs.fph", "mock")
    )
    with pytest.raises(Unsupported):
        adapter.identify()
    assert adapter.identity is None


@pytest.mark.parametrize("options,maximum", [(b"0", 13.6e9), (b"FPH-B20", 20e9)])
def test_k13_reports_frequency_option_bounds(options, maximum):
    transport = StateTransport("V2.33", "1321.1111K13")
    transport.options = options
    adapter = FphAdapter(transport, fph(), ConnectionConfig("mock://k13", "rs.fph"))
    adapter.identify()
    adapter.refresh_capabilities()
    caps = adapter.capabilities()
    for name in ("start_frequency", "stop_frequency", "center_frequency", "span"):
        assert caps.get(name).writable and caps.get(name).parameters["value"].maximum == maximum
    from lab_control.domain.instruments import ValidationError

    with pytest.raises(ValidationError):
        caps.get("span").validate({"value": 9})


def test_k13_passive_connection_and_frequency_controls(tmp_path):
    config = ConnectionConfig("mock://k13", "rs.fph", "mock")
    transport = StateTransport("V2.33", "1321.1111K13")
    transport.state.update(
        transfer_format="ASC", start_frequency=9e9, center_frequency=10e9, stop_frequency=11e9, span=2e9
    )
    with ApplicationRuntime((config,), root=tmp_path, hardware=False) as app:
        app.sessions.factory = lambda _: FphAdapter(transport, fph(), config)
        key = app.add(config)
        for _ in range(2):
            assert app.connect(key).result(5).firmware == "V2.33"
            adapter = app.sessions.sessions[key].adapter
            assert {c.name for c in adapter.capabilities().operations if c.writable} == {
                "start_frequency",
                "stop_frequency",
                "center_frequency",
                "span",
                "rbw",
                "vbw",
                "reference_level",
                "attenuation",
                "rbw_auto",
                "vbw_auto",
                "attenuation_auto",
            }
            for name in ("single", "marker_frequency"):
                with pytest.raises(Unsupported):
                    adapter.apply(DeviceCommand(name, confirmed=True))
            assert adapter.maximum_frequency == 13.6e9
            with pytest.raises(Unsupported, match="B24"):
                adapter.capabilities().get("preamplifier")
            batch = app.sessions.submit(key, "acquire", capture_request(adapter)).result(5)
            (measurement,) = batch.measurements
            assert measurement.axis[-1] == 11e9 and len(measurement.values) == 711
            transport.state["transfer_format"] = "REAL"
            with pytest.raises(Unsupported, match="ASCII"):
                adapter.acquire(capture_request(adapter))
            transport.state["transfer_format"] = "ASC"
            app.sessions.disconnect(key)
    assert all("?" in command for command in transport.history)
    assert "INP:GAIN:STAT?" not in transport.history


@pytest.mark.parametrize("firmware,product", [("V2.33", "1321.1111K13"), ("V2.40", "1321.1111K02")])
@pytest.mark.parametrize(
    "name,value", [("rbw", 1e4), ("vbw", 1e4), ("reference_level", -20), ("attenuation", 20)]
)
def test_front_panel_inputs_without_optional_preamp(firmware, product, name, value):
    transport = StateTransport(firmware, product)
    writes = []
    transport.write = writes.append
    adapter = FphAdapter(transport, fph(), ConnectionConfig("mock://fph-session", "rs.fph"))
    adapter.identify()
    adapter.refresh_capabilities()
    result = adapter.apply(DeviceCommand(name, {"value": value}))
    assert result.outcome == "success" and result.readback == value
    assert writes == [fph().commands[name].writes[0].format(value=f"{value:g}")]
    assert "INP:GAIN:STAT?" not in transport.history

"""Scripted manufacturer envelopes only; no hardware access or SDK deployment."""

from copy import deepcopy

import numpy as np
import pytest

from lab_control.application.catalog import instrument_categories
from lab_control.domain.connections import ConnectionConfig, network_host
from lab_control.domain.instruments import AcquisitionRequest, DeviceCommand, DeviceError, DeviceIdentity
from lab_control.instruments.devices.liquid_moku_pro.adapter import Adapter
from lab_control.instruments.devices.liquid_moku_pro.protocol import ADAPTER, CHANNELS


class Wire:
    def __init__(self):
        self.key = None
        self.calls = []
        self.names = ["Oscilloscope", "Waveform Generator", "Spectrum Analyzer", "Unrelated"]
        self.routes = {
            f"Output{ch}": f"Slot2Out{letter}" for ch, letter in zip(CHANNELS, "DCBA", strict=True)
        }
        self.routes.update(Slot1InA="Input1", Slot3InA="Input1", Slot4InA="Input4")
        self.front = {
            ch: {"impedance": "50Ohm", "coupling": "DC", "attenuation": "0dB", "bandwidth": "300MHz"}
            for ch in CHANNELS
        }
        self.frame = None
        self.change = False
        self.settings = {
            "timebase": {"t1": 0, "t2": 0.0005},
            "acquisition_mode": {"mode": "Normal"},
            "interpolation": {"interpolation": "SinX"},
            "span": {"frequency1": 0, "frequency2": 20000},
            "rbw": {"mode": "Auto", "value": 100},
            "window": {"window": "Hann"},
        }
        self.max_length = 1024

    def close(self):
        pass

    def identity(self, serial):
        assert serial == "88"
        return DeviceIdentity("Liquid Instruments", "Moku:Pro", serial, "4.4.0")

    def call(self, method, path, params=None):
        self.calls.append((method, path, deepcopy(params)))
        if path == "moku/claim_ownership":
            assert params["force_connect"] is params["force_deploy"] is params["ignore_busy"] is False
            assert params["persist_state"] is True
            self.key = "test-only"
            return None
        assert self.key is not None
        if path == "mim/get_instruments":
            return self.names
        if path == "moku/relinquish_ownership":
            return None
        if path == "mim/get_connections":
            return [dict(destination=k, source=v) for k, v in self.routes.items()]
        if path == "mim/set_connections":
            for r in params["connections"]:
                self.routes[r["destination"]] = r["source"]
            return self.call("GET", "mim/get_connections")
        if path == "mim/get_frontend":
            return self.front[params["channel"]].copy()
        if path == "mim/set_frontend":
            self.front[params["channel"]].update(
                {k: params[k] for k in ("bandwidth", "attenuation", "coupling", "impedance")}
            )
            return self.front[params["channel"]].copy()
        action = path.rsplit("/", 1)[-1]
        if action.startswith("get_") and action[4:] in self.settings:
            assert method == "GET"
            return self.settings[action[4:]].copy()
        if action.startswith("set_") and action[4:] in (*self.settings, "trigger", "averaging"):
            assert method == "POST" and params["strict"] is True
            name = action[4:]
            actual = {k: v for k, v in params.items() if k != "strict"}
            if name == "timebase":
                self.max_length = actual.pop("max_length")
            if name == "rbw":
                actual["value"] = max(100, round(actual.pop("rbw_value", 100) / 100) * 100)
            self.settings[name] = actual
            return actual.copy()
        if path.endswith("/get_samplerate"):
            return {"sample_rate": 1e6}
        if path.endswith("/summary"):
            return "Output 1 (off) - Off\nOutput 2 (on) - Sine"
        if path == "mim/get_output":
            return {"output_gain": "0dB"}
        if path == "slot2/waveformgenerator/get_output_termination":
            return {"termination": "50Ohm"}
        if path == "slot2/waveformgenerator/generate_waveform":
            return params.copy()
        if path == "slot1/oscilloscope/get_sources":
            return [{"channel": 2, "enabled": True, "source": "Input1"}]
        if path.endswith("/get_data"):
            if self.change:
                self.front[1]["coupling"] = "AC"
            assert params["wait_complete"] is True
            if self.frame is not None:
                return self.frame
            if path.startswith("slot3/spectrumanalyzer/"):
                assert params["units"] == "dBm" and params["psdUnits"] is False
                return {"frequency": [0, 10000, 20000], "ch1": [-70, -10, -65]}
            return {"time": [0, 0.000025, 0.00005], "ch2": [0, 0.05, 0]}
        raise AssertionError(path)


def instrument():
    wire = Wire()
    dev = Adapter(ConnectionConfig("moku://lab.invalid", ADAPTER, "moku", expected_serial="88"), wire)
    assert not wire.calls
    dev.apply(DeviceCommand("attach_workspace", confirmed=True))
    return dev, wire


@pytest.mark.parametrize(
    "op,params",
    [
        ("scope_timebase", {"t1": -0.002, "t2": -0.002, "max_length": 128}),
        ("scope_timebase", {"t1": 0, "t2": 0.001, "max_length": 500}),
        ("scope_trigger", {"source": "CURRENT", "mode": "AUTO", "edge": "BOTH", "level": 21}),
        ("spectrum_span", {"frequency1": 10, "frequency2": 5}),
        ("spectrum_span", {"frequency1": 0, "frequency2": 300e6 + 1}),
        ("spectrum_rbw", {"mode": "Manual"}),
        ("spectrum_rbw", {"mode": "Manual", "rbw_value": 0}),
        ("spectrum_rbw", {"mode": "Auto", "rbw_value": 100}),
        ("spectrum_averaging", {"target_duration": -0.1}),
    ],
)
def test_invalid_capture_settings_send_no_packets(op, params):
    dev, wire = instrument()
    wire.calls.clear()
    with pytest.raises(DeviceError):
        dev.apply(DeviceCommand(op, params, 1, True))
    assert not wire.calls


@pytest.mark.parametrize("mode", ["Auto", "Minimum", "Manual"])
def test_rbw_uses_actual_bandwidth_and_scoped_routes(mode):
    dev, wire = instrument()
    params = {"mode": mode, **({"rbw_value": 275} if mode == "Manual" else {})}
    result = dev.apply(DeviceCommand("spectrum_rbw", params, 4, True))
    assert wire.calls[-2:] == [
        ("POST", "slot3/spectrumanalyzer/set_rbw", {**params, "strict": True}),
        ("GET", "slot3/spectrumanalyzer/get_rbw", None),
    ]
    assert result.readback == {"mode": mode, "rbw_value": 300 if mode == "Manual" else 100}


def test_external_trigger_without_input_and_hardware_format_ack(monkeypatch):
    dev, wire = instrument()
    original = wire.call

    def response(method, path, params=None):
        reply = original(method, path, params)
        if path.endswith("/set_trigger"):
            return {
                "Trigger channel": reply["source"],
                "Trigger type": "Edge",
                "Trigger mode": reply["mode"],
                "Trigger edge": reply["edge"],
                "level": reply["level"],
            }
        return reply

    monkeypatch.setattr(wire, "call", response)
    params = {"source": "External", "mode": "Normal", "edge": "Both", "level": -0.25}
    result = dev.apply(DeviceCommand("scope_trigger", params, 4, True))
    assert result.acknowledged and result.readback["source"] == "External"
    assert wire.calls[-1][1] == "slot1/oscilloscope/set_trigger"
    with pytest.raises(DeviceError):
        dev.apply(DeviceCommand("scope_trigger", {**params, "source": "Current"}, 4, True))
    assert sum(path.endswith("/set_trigger") for _, path, _ in wire.calls) == 1


def test_capture_readback_mismatch_does_not_repeat_write(monkeypatch):
    dev, wire = instrument()
    original = wire.call

    def response(method, path, params=None):
        reply = original(method, path, params)
        return {"window": "Rectangular"} if path.endswith("/get_window") else reply

    monkeypatch.setattr(wire, "call", response)
    with pytest.raises(DeviceError):
        dev.apply(DeviceCommand("spectrum_window", {"window": "Hamming"}, 1, True))
    assert sum(path.endswith("/set_window") for _, path, _ in wire.calls) == 1


@pytest.mark.parametrize("ch", CHANNELS)
def test_physical_channels_remap_without_output_and_source_needs_separate_confirmation(ch):
    dev, wire = instrument()
    for kind, label in [("waveform", "示波器"), ("spectrum", "频谱仪")]:
        dev.apply(DeviceCommand("select_measurement_input", {"role": kind.upper()}, ch, True))
        measurement = dev.acquire(AcquisitionRequest(kind, (ch,))).measurements[0]
        assert measurement.channel == ch and measurement.metadata["channel_label"] == f"{label} ch{ch}"
        assert np.array_equal(measurement.values, [0, 0.05, 0] if kind == "waveform" else [-70, -10, -65])
    assert wire.routes["Slot4InA"] == "Input4" and not any("generate_waveform" in p for _, p, _ in wire.calls)
    cmd = DeviceCommand("standard_waveform", {"frequency": 10000, "amplitude": 0.1}, ch, True)
    assert dev.apply(cmd).acknowledged
    assert dev.apply(DeviceCommand("waveform_stop", {}, ch, True)).acknowledged
    packets = [b for _, p, b in wire.calls if p.endswith("/generate_waveform")]
    assert [(b["channel"], b["type"]) for b in packets] == [(5 - ch, "Sine"), (5 - ch, "Off")]
    assert sum(p == "moku/claim_ownership" for _, p, _ in wire.calls) == 1
    dev.close()
    assert wire.calls[-1][1] == "moku/relinquish_ownership"


def test_fail_closed_and_no_slot_four_deploy_or_output_fanout():
    dev, wire = instrument()
    wire.routes["Slot4InB"] = "Slot2OutD"
    with pytest.raises(DeviceError):
        dev.apply(DeviceCommand("standard_waveform", {"frequency": 10000, "amplitude": 0.1}, 1, True))
    with pytest.raises(DeviceError):
        dev.apply(DeviceCommand("waveform_stop"))
    wire.names.reverse()
    with pytest.raises(DeviceError):
        dev.acquire(AcquisitionRequest("waveform"))
    assert not any("generate_waveform" in p or "platform" in p for _, p, _ in wire.calls)


@pytest.mark.parametrize(
    "frame",
    [
        {"frequency": [0, 1], "ch1": [1]},
        {"frequency": [0, 0], "ch1": [1, 2]},
        {"frequency": [0, 1], "ch1": [1, float("nan")]},
        {"frequency": [0, 1], "ch2": [1, 2]},
    ],
)
def test_bad_frame_is_not_a_configuration_change(frame):
    dev, wire = instrument()
    wire.frame = frame
    with pytest.raises(DeviceError) as err:
        dev.acquire(AcquisitionRequest("spectrum"))
    assert err.value.code == "parse_error"


def test_shared_frontend_change_invalidates_complete_frame():
    dev, wire = instrument()
    wire.change = True
    with pytest.raises(DeviceError) as err:
        dev.acquire(AcquisitionRequest("waveform"))
    assert err.value.code == "configuration_changed"
    assert dev.acquire(AcquisitionRequest("spectrum")).configuration["frontend"]["coupling"] == "AC"


def test_multirole_catalog_and_one_host_lease():
    assert set(instrument_categories({"adapterId": ADAPTER})) == {
        "signal_generator",
        "spectrum_analyzer",
        "oscilloscope",
    }
    assert network_host("moku://[fe80::1%7]") == "fe80::1%7"


def test_manual_signal_route_only_uses_free_disabled_source_and_never_enables():
    dev, wire = instrument()
    del wire.routes["Output4"]  # frees logical A (off)
    dev.apply(DeviceCommand("connect_generator_channel", {}, 4, True))
    assert wire.routes["Output4"] == "Slot2OutA"
    assert not any(p.endswith("generate_waveform") for _, p, _ in wire.calls)
    wire.routes["Slot4InB"] = wire.routes.pop("Output4")
    with pytest.raises(DeviceError):
        dev.apply(DeviceCommand("connect_generator_channel", {}, 4, True))


def test_shared_frontend_readback_and_fourth_slot_protection():
    dev, wire = instrument()
    params = {"impedance": "1MOhm", "coupling": "AC", "attenuation": "20dB"}
    assert dev.apply(DeviceCommand("shared_frontend", params, 1, True)).readback["impedance"] == "1MOhm"
    for kind in ("waveform", "spectrum"):
        assert dev.acquire(AcquisitionRequest(kind)).configuration["frontend"] == {
            **params,
            "bandwidth": "300MHz",
        }
    wire.routes["Slot4InA"] = "Input1"
    with pytest.raises(DeviceError):
        dev.apply(DeviceCommand("shared_frontend", params, 1, True))
    assert len([p for _, p, _ in wire.calls if p == "mim/set_frontend"]) == 1


def test_editing_only_bandwidth_preserves_live_frontend():
    dev, wire = instrument()
    wire.front[1].update(impedance="1MOhm", coupling="AC", attenuation="20dB")
    result = dev.apply(DeviceCommand("shared_frontend", {"bandwidth": "600MHz"}, 1, True))
    assert result.readback == dict(impedance="1MOhm", coupling="AC", attenuation="20dB", bandwidth="600MHz")
    assert dev.acquire(AcquisitionRequest("spectrum")).configuration["frontend"] == result.readback


def test_first_output_allocates_after_safety_checks_and_stop_never_allocates():
    wire = Wire()
    del wire.routes["Output4"]
    dev = Adapter(ConnectionConfig("moku://lab.invalid", ADAPTER, "moku", expected_serial="88"), wire)
    original = wire.routes.copy()
    with pytest.raises(DeviceError):
        dev.apply(DeviceCommand("waveform_stop", {}, 4, True))
    assert wire.routes == original
    original_call = wire.call
    wire.call = lambda m, p, v=None: (
        {"output_gain": "14dB"} if p == "mim/get_output" else original_call(m, p, v)
    )
    command = DeviceCommand("standard_waveform", {"frequency": 10000, "amplitude": 0.1}, 4, True)
    with pytest.raises(DeviceError):
        dev.apply(command)
    assert wire.routes == original and not any(p.endswith("generate_waveform") for _, p, _ in wire.calls)
    wire.call = original_call
    assert dev.apply(command).outcome == "success"
    assert wire.routes == {**original, "Output4": "Slot2OutA"}
    paths = [p for _, p, _ in wire.calls]
    assert paths.count("moku/claim_ownership") == paths.count("mim/set_connections") == 1
    i = paths.index("mim/set_connections")
    assert paths[i : i + 3] == ["mim/set_connections", "mim/get_connections", "mim/get_connections"]
    assert paths.index("slot2/waveformgenerator/generate_waveform") > i + 2


def test_refuse_to_overwrite_fourth_slot_route_or_assume_scope_source():
    dev, wire = instrument()
    wire.routes["Slot1InA"] = "Slot4OutA"
    with pytest.raises(DeviceError, match="不覆盖"):
        dev.apply(DeviceCommand("select_measurement_input", {"role": "WAVEFORM"}, 1, True))
    wire.routes["Slot1InA"] = "Input1"
    original = wire.call
    wire.call = lambda m, p, v=None: None if p.endswith("get_sources") else original(m, p, v)
    with pytest.raises(DeviceError, match="来源回包无效"):
        dev.acquire(AcquisitionRequest("waveform"))
    assert not any(p in ("mim/set_connections", "slot1/oscilloscope/get_data") for _, p, _ in wire.calls)

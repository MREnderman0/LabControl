from uuid import uuid4

import pytest

from lab_control.application.agents.policy import INTERNAL_CLIENT
from lab_control.application.config import ConnectionConfig
from lab_control.application.runtime import ApplicationRuntime
from lab_control.domain.instruments import DeviceError
from lab_control.instruments.devices.microchip_53100a.tests.simulation import PhaseSimulation


def test_partial_frequency_preserves_mode_and_rejects_changed_baseline(tmp_path):
    with ApplicationRuntime(root=tmp_path, hardware=False) as runtime:
        runtime.sessions.factory = PhaseSimulation
        device = runtime.add(ConnectionConfig("mock://partial", "microchip.53100a", "mock"))
        runtime.connect(device).result(5)
        instrument = runtime.sessions.sessions[device].adapter
        key = "1.input_frequency"
        instrument.values[key] = {"automatic": False, "value": 10e6}
        service = runtime.agent
        grant = service.policy.create_grant(
            INTERNAL_CLIENT, [device], ["view", "apply", "read_parameters"],
            channels=[1], operations=["input_frequency"], limits={},
        )

        def preview():
            return service.preview_change(
                INTERNAL_CLIENT, grant, device, "input_frequency", {"value": 20e6}, partial=True
            )

        def apply(prepared):
            from time import monotonic, sleep

            task = service.apply_change(INTERNAL_CLIENT, grant, device, prepared["id"], uuid4().hex)
            deadline = monotonic() + 10
            while monotonic() < deadline:
                current = service.get_task_status(INTERNAL_CLIENT, grant, task["id"])
                if current["state"] in {"completed", "failed", "partial"}:
                    return current
                sleep(0.01)
            pytest.fail("apply task did not finish")

        with pytest.raises(DeviceError, match="value"):
            service.preview_change(INTERNAL_CLIENT, grant, device, "input_frequency", {"automatic": True}, partial=True)
        assert instrument.values[key] == {"automatic": False, "value": 10e6}
        prepared = preview()
        assert prepared["parameters"] == {"automatic": False, "value": 20e6}
        instrument.values[key]["value"] = 15e6
        assert apply(prepared)["code"] == "preview_expired"
        assert instrument.values[key] == {"automatic": False, "value": 15e6}
        assert apply(preview())["state"] == "completed"
        assert instrument.values[key] == {"automatic": False, "value": 20e6}
        pending = preview()
        service.policy.revoke_grant(grant)
        with pytest.raises(DeviceError):
            apply(pending)

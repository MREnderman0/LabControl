import pytest

from lab_control.domain.instruments import ValidationError
from lab_control.instruments.devices.ni_9230.calibration import sensors


@pytest.mark.parametrize(
    "options",
    [
        ("ct12000l.ai0=TEST@nan",),
        ("ct12000l.ai3=TEST@19100",),
        ("ct12000l.ai0=ONE@19050", "ct12000l.ai1=ONE@19150"),
        ("ct12000l.ai0=ONE@19050", "ct12000l.ai0=TWO@19150"),
        ("ct12000l.ai1=TWO@-1",),
    ],
)
def test_reject_ambiguous_or_invalid_sensor_records(options):
    with pytest.raises(ValidationError):
        sensors(options)


def test_unassigned_inputs_use_explicit_nominal_value():
    mounted = sensors(("ct12000l.ai1=EXAMPLE@19001",))
    assert mounted[2].serial == "EXAMPLE" and mounted[2].sensitivity == 19001
    assert mounted[1].metadata() == {
        "sensor_serial": "",
        "sensitivity_mV_per_g": 20000,
        "sensitivity_source": "CT12000L nominal",
    }


def test_common_gain_is_applied_without_inventing_serials():
    gains = sensors(("ct12000l.all=19300",))
    for ch in (1, 2, 3):
        assert gains[ch].metadata() == {
            "sensor_serial": "",
            "sensitivity_mV_per_g": 19300,
            "sensitivity_source": "user_selected_common",
        }
    with pytest.raises(ValidationError):
        sensors(("ct12000l.all=19300", "ct12000l.ai0=TEST@19100"))

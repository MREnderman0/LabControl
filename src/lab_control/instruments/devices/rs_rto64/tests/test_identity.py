import pytest

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import DeviceError
from lab_control.instruments.base import identity_from_scpi
from lab_control.instruments.registry import build_scpi_adapter, match
from lab_control.transport.base import MockTransport


@pytest.mark.parametrize(
    ("raw", "matches"),
    [
        (b"Rohde&Schwarz,RTO6,1802.0001k04/TEST,5", True),
        (b"Rohde-Schwarz,RTO64,TEST,5", True),
        (b"Rohde&Schwarz,RTO6,1802.0001K05/TEST,5", False),
        (b"Rohde&Schwarz,RTO6,1802.0001K04,5", False),
        (b"Rohde&Schwarz,RTO6,1802.0001K04/,5", False),
        (b"Other,RTO6,1802.0001K04/TEST,5", False),
    ],
)
def test_discovery_and_connection_agree(raw, matches):
    identity = identity_from_scpi(raw)
    assert (match(identity) == "rs.rto64") is matches
    wire = MockTransport([("*IDN?", raw)])
    wire.open()
    adapter = build_scpi_adapter(ConnectionConfig("mock://test", "rs.rto64"), wire)
    if matches:
        assert adapter.identify() == identity
        assert adapter.identity.raw == raw.decode()
    else:
        with pytest.raises(DeviceError):
            adapter.identify()

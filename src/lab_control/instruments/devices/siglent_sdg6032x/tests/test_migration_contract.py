from pathlib import Path

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import jsonable
from lab_control.instruments.registry import describe


def test_migration_capabilities_preserve_original_contract(load_capability_contract):
    contract = load_capability_contract(Path(__file__).parent / "fixtures/capabilities-before.json")
    assert (
        jsonable(describe(ConnectionConfig("metadata://test", "siglent.sdg6032x")))
        == contract["capabilities"]
    )

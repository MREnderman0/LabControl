from pathlib import Path

from lab_control.domain.connections import ConnectionConfig
from lab_control.domain.instruments import jsonable
from lab_control.instruments.registry import describe


def test_current_contract_keeps_migration_evidence_separate(load_capability_contract):
    contract = load_capability_contract(Path(__file__).parent / "fixtures/capabilities-current.json")
    assert jsonable(describe(ConnectionConfig("metadata://test", "rs.rtm3004"))) == contract["capabilities"]

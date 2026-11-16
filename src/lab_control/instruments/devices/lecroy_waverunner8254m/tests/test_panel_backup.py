import pytest

from lab_control.domain.instruments import DeviceError
from lab_control.transport.base import ieee_block

from ..hardware.control import panel_block, panel_settings_digest


def test_only_export_timestamp_is_excluded_from_panel_comparison():
    raw = b"' XStreamDSO ConfigurationVBScript ...\r\n' MODEL,TEST\r\n' Monday, November 16, 2026 11:30:23 AM\r\nC1.View = True\r\nffffffff"

    def digest(data):
        return panel_settings_digest(b"PNSU " + ieee_block(data))

    assert digest(raw) == digest(raw.replace(b"11:30:23", b"11:32:08"))
    assert digest(raw) != digest(raw.replace(b"True", b"False"))
    assert digest(raw) != digest(raw.replace(b"TEST", b"OTHER"))


def test_recall_preserves_entire_definite_block_including_crc_and_terminator():
    raw = b"PNSU #9000000005a\x00\n b\r\n"
    assert panel_block(raw) == raw
    assert panel_block(raw[5:]) == raw


@pytest.mark.parametrize("raw", [b"PNSU #10", b"PNSU #15abc", b"OTHER #11x", b"WARNING"])
def test_invalid_backup_prevents_panel_writes(raw):
    with pytest.raises(DeviceError):
        panel_block(raw)

from collections import deque
from types import SimpleNamespace

import pytest

from lab_control.instruments.scpi_single import LecroySingle, RigolSingle, RohdeSingle


class Transcript:
    def __init__(self, exchanges):
        self.exchanges = deque(exchanges)

    def query(self, command):
        expected, reply = self.exchanges.popleft()
        assert command == expected
        return reply.encode("ascii")

    def write(self, command):
        assert self.exchanges.popleft() == (command, None)


@pytest.mark.parametrize(
    ("family", "steps", "checks"),
    [
        (RohdeSingle, [("*ESR?", "1"), ("SING;*OPC", None), ("*ESR?", "0"), ("*ESR?", "1")], [False, True]),
        (
            LecroySingle,
            [
                ("TRMD STOP", None),
                ("TRMD?", "TRMD STOP"),
                ("INR?", "INR 1"),
                ("TRMD SINGLE", None),
                ("INR?", "INR 0"),
                ("INR?", "INR 1"),
                ("TRMD?", "TRMD STOP"),
            ],
            [False, True],
        ),
        (
            RigolSingle,
            [
                (":SING", None),
                (":TRIG:SWE?", "SING"),
                (":SYST:ERR?", '0,"No error"'),
                (":TRIG:STAT?", "WAIT"),
                (":TRIG:STAT?", "STOP"),
            ],
            [False, True],
        ),
    ],
)
def test_native_arm_waits_for_its_own_completion(family, steps, checks):
    adapter = SimpleNamespace(transport=Transcript(steps), single_finished=lambda: True)
    done = family.single_arm(adapter)
    assert [done() for _ in checks] == checks
    assert not adapter.transport.exchanges


def test_lecroy_wait_budget_uses_only_selected_channels():
    model = LecroySingle()
    model.transport = Transcript(
        [
            ("VBS? 'return=app.Acquisition.C1.AverageSweeps'", "VBS 16"),
            ("VBS? 'return=app.Acquisition.C3.AverageSweeps'", "VBS 64"),
        ]
    )
    assert model.single_count((1, 3)) == 64

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


def test_rto_uses_the_common_acquisition_and_average_count():
    from lab_control.instruments.devices.rs_rto64.adapter import Rto64Adapter

    adapter = SimpleNamespace(read={"acquisition_count": 8}.__getitem__)
    assert Rto64Adapter.single_count(adapter, (1, 2)) == 8


@pytest.mark.parametrize(
    ("initial", "sweep", "previous", "expected"),
    [
        ("STOP", "SING", "NORM", "NORM"),
        ("STOP", "AUTO", "AUTO", "AUTO"),
    ],
)
def test_rigol_manual_resume_waits_for_trigger(initial, sweep, previous, expected):
    scope = RigolSingle()
    scope._before_single = (True, previous)
    steps = [(":TRIG:STAT?", initial), (":TRIG:SWE?", sweep)]
    if sweep == "SING":
        steps.append((":TRIG:SWE " + expected, None))
    scope.transport = Transcript(
        steps
        + [
            (":RUN", None),
            (":TRIG:SWE?", expected),
            (":TRIG:STAT?", "WAIT"),
            (":TRIG:STAT?", "WAIT"),
            (":TRIG:STAT?", "TD"),
        ]
    )
    ready = scope.ensure_continuous()
    assert not ready() and ready() and not scope.transport.exchanges


def test_running_rigol_joins_without_writes():
    scope = RigolSingle()
    scope.transport = Transcript([(":TRIG:STAT?", "AUTO"), (":TRIG:SWE?", "AUTO")])
    assert scope.ensure_continuous() is None and not scope.transport.exchanges


def test_lecroy_resumes_previous_mode_and_waits_for_new_acquisition():
    scope = LecroySingle()
    scope._before_single = "NORM"
    scope.transport = Transcript(
        [
            ("TRMD?", "TRMD STOP"),
            ("INR?", "INR 1"),
            ("TRMD NORM", None),
            ("TRMD?", "TRMD NORM"),
            ("INR?", "INR 0"),
            ("INR?", "INR 1"),
        ]
    )
    ready = scope.ensure_continuous()
    assert not ready() and ready() and not scope.transport.exchanges

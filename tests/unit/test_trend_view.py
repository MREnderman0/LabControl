import pytest

from lab_control.application.monitoring import MonitorFrame
from lab_control.domain.instruments import AcquisitionRequest, DeviceIdentity, Measurement, MeasurementBatch
from lab_control.ui.pages.monitoring.acceleration import AccelerationPlot
from lab_control.ui.pages.monitoring.scan import ScanTrendPlot


@pytest.mark.parametrize(
    "plot_type,quantity,unit,expected_x",
    [(ScanTrendPlot, "voltage", "V", (0, 600)), (AccelerationPlot, "acceleration", "g", (0, 20))],
)
def test_manual_zoom_survives_updates_then_restore_uses_latest_time_window(
    qtbot, plot_type, quantity, unit, expected_x
):
    trend = plot_type()
    qtbot.addWidget(trend)
    trend.show()
    trend.set_channels((1,))

    def display(sequence, elapsed):
        frame = MonitorFrame(
            sequence,
            0,
            AcquisitionRequest("scalar", (1,), points=1),
            MeasurementBatch(
                DeviceIdentity("Mock", "Trend"), (Measurement(quantity, unit, [sequence], 1),), {}
            ),
            received_at=elapsed,
        )
        assert trend.show_frame(frame, (1,))

    display(1, 100)
    trend.plot.setXRange(20, 25, padding=0)
    display(2, 115)
    assert trend.plot.viewRange()[0] == pytest.approx([20, 25])
    trend.plot.setYRange(80, 90, padding=0)
    trend.plot.build_menu().actions()[0].trigger()
    assert trend.plot.viewRange()[0] == pytest.approx(expected_x)
    low, high = trend.plot.viewRange()[1]
    assert low < 1 < 2 < high

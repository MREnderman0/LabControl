import json
from types import SimpleNamespace

from lab_control.ui.pages.operation_log import OperationLogPage, read_log


def write_day(folder, day, rows):
    folder.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n{broken line\n"
    (folder / f"{day}.jsonl").write_text(text, encoding="utf-8")


ROWS = [
    {"timestamp": "2026-11-17T01:00:00+00:00", "event": "scan_started", "scan_id": "s1"},
    {
        "timestamp": "2026-11-17T01:00:05+00:00",
        "event": "connection_failed",
        "device_id": "dev-a",
        "error": "VISA 超时",
    },
    {"timestamp": "2026-11-17T01:01:00+00:00", "event": "acquisition_setup_started", "device_id": "dev-b"},
    {"timestamp": "2026-11-17T01:02:00+00:00", "event": "agent_grant_created", "grant": {"scope": "read"}},
]


def test_read_log_newest_first_and_skips_malformed_lines(tmp_path):
    write_day(tmp_path, "2026-11-17", ROWS)
    entries = read_log(tmp_path / "2026-11-17.jsonl")
    assert [e.event for e in entries] == [r["event"] for r in reversed(ROWS)]
    failure = next(e for e in entries if e.event == "connection_failed")
    assert failure.problem and failure.device == "dev-a" and failure.summary == "VISA 超时"
    assert failure.label == "连接失败" and failure.category == "device"
    assert read_log(tmp_path / "missing.jsonl") == []


def test_page_filters_by_category_search_and_day(qtbot, tmp_path):
    logs = tmp_path / "logs"
    write_day(logs, "2026-11-16", ROWS[:1])
    write_day(logs, "2026-11-17", ROWS)
    page = OperationLogPage(SimpleNamespace(root=tmp_path))
    qtbot.addWidget(page)
    page.show()
    assert page.day.count() == 2 and page.day.currentData() == "2026-11-17"
    assert page.model.rowCount() == 4 and "异常 1 条" in page.summary.text()
    page.category.button(5).click()  # 异常
    assert [e.event for e in page.model.entries] == ["connection_failed"]
    page.category.button(4).click()  # Agent 与网络
    assert [e.event for e in page.model.entries] == ["agent_grant_created"]
    page.category.button(0).click()
    page.search.setText("dev-b")
    assert [e.event for e in page.model.entries] == ["acquisition_setup_started"]
    page.search.clear()
    page.day.setCurrentIndex(page.day.findData("2026-11-16"))
    assert [e.event for e in page.model.entries] == ["scan_started"]


def test_empty_log_folder_shows_guidance(qtbot, tmp_path):
    page = OperationLogPage(SimpleNamespace(root=tmp_path))
    qtbot.addWidget(page)
    page.show()
    assert page.model.rowCount() == 0 and not page.day.isEnabled()
    assert page.empty.isVisible() and "还没有操作日志" in page.empty.text()


def test_nested_apply_results_show_reasons_and_filter_historical_entries(qtbot, tmp_path):
    outcomes = {"rejected": "已拒绝", "partial": "部分完成", "uncertain": "结果未确认"}
    rows = [
        dict(
            event="operation_completed",
            operation="apply",
            result=dict(
                operation="offset", outcome=outcome, error=f"reason-{outcome}" if outcome in outcomes else ""
            ),
        )
        for outcome in [*outcomes, "success"]
    ]
    write_day(tmp_path / "logs", "2026-11-17", rows)
    page = OperationLogPage(SimpleNamespace(root=tmp_path))
    qtbot.addWidget(page)
    page.show()
    assert page.model.rowCount() == 4 and "异常 3 条" in page.summary.text()
    page.category.button(5).click()
    assert page.model.rowCount() == 3
    for entry in page.model.entries:
        outcome = entry.details["result"]["outcome"]
        assert outcomes[outcome] in entry.label and f"reason-{outcome}" in entry.summary


def test_device_names_resolve_and_are_searchable(qtbot, tmp_path):
    write_day(tmp_path / "logs", "2026-11-17", ROWS)
    names = {"dev-a": "DSA815 · 频谱"}
    page = OperationLogPage(SimpleNamespace(root=tmp_path), resolve_device=names.get)
    qtbot.addWidget(page)
    page.show()
    row = next(i for i, e in enumerate(page.model.entries) if e.device == "dev-a")
    assert page.model.index(row, 3).data() == "DSA815 · 频谱"
    page.search.setText("dsa815")
    assert [e.event for e in page.model.entries] == ["connection_failed"]
    page.category.button(2).click()  # 参数操作
    assert page.model.entries == []

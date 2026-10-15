"""executor_sandbox 解析层的离线测试（不起 Docker）。

用真实收集期失败（ERRORS 区段）的输出形状做样本——2026-10-15 生产事故：
pytest 报 2 errors 但解析器只认 FAILURES 区段，导致误判 all_green。
"""

from executor_sandbox import build_report, parse_failures

CANNED_ERRORS_STDOUT = """============================= test session starts ==============================
platform darwin -- Python 3.11.17, pytest-9.1.1, pluggy-1.6.0
rootdir: /app
collected 0 items / 2 errors

==================================== ERRORS ====================================
___________________ ERROR collecting tests/test_main.py _____________________
Traceback:
/usr/local/lib/python3.11/importlib/__init__.py:126: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
tests/test_main.py:8: in <module>
    from simple_todo_cli.main import main
E   ModuleNotFoundError: No module named 'simple_todo_cli'
___________________ ERROR collecting tests/test_models.py _____________________
Traceback:
tests/test_models.py:8: in <module>
    from simple_todo_cli.models import Todo
E   ModuleNotFoundError: No module named 'simple_todo_cli'
=========================== warnings summary ===============================
/usr/local/lib/python3.11/site-packages/_pytest/cacheprovider.py:469
  PytestCacheWarning: could not create cache path /app/.pytest_cache/v/cache/nodeids
=========================== short test summary info ============================
ERROR tests/test_main.py
ERROR tests/test_models.py
2 warnings, 2 errors in 0.10s
"""


def test_parse_collection_errors_from_errors_section():
    failures = parse_failures(CANNED_ERRORS_STDOUT)
    assert len(failures) == 2
    assert failures[0]["error_type"] == "ModuleNotFoundError"
    assert "No module named 'simple_todo_cli'" in failures[0]["message"]
    assert "test_main.py" in failures[0]["test_name"]


def test_build_report_counts_errors_without_failures_section():
    report = build_report(stdout=CANNED_ERRORS_STDOUT, stderr="", exit_code=2,
                          duration=1.0, timed_out=False, timeout=120)
    assert report["failed"] == 2
    assert report["passed"] == 0
    assert report["total"] == 2
    # 关键回归：解析器现在能从 ERRORS 区段取出失败块，judge() 不会再误判 all_green
    assert len(report["failures"]) == 2

"""验收第 6 步：用第 5 步沙箱的真实失败输出构造合法的 TestReport / FixRequest。"""

import json
from pathlib import Path

from schemas import Failure, FixRequest, TestReport

REPORT_JSON = Path(__file__).resolve().parents[1] / "examples" / "failing_demo" / "report.json"


def test_testreport_from_real_sandbox_output():
    raw = json.loads(REPORT_JSON.read_text(encoding="utf-8"))
    report = TestReport(**raw)
    assert report.failed == 1 and report.passed == 1 and report.total == 2
    assert len(report.failures) == 1
    f = report.failures[0]
    assert f.test_name == "test_this_must_fail"
    assert f.error_type == "AssertionError"
    assert "assert 1 == 2" in f.traceback
    assert report.all_passed is False


def test_failure_direct_construct():
    f = Failure(test_name="t_x", error_type="ZeroDivisionError", message="division by zero")
    assert f.test_name == "t_x"


def test_fix_request():
    req = FixRequest(
        target_files=["app.py"],
        failure_summary="test_this_must_fail: assert 1 == 2",
        suggested_fix="无（首轮）",
        retry_round=1,
    )
    assert req.retry_round == 1 and req.target_files == ["app.py"]

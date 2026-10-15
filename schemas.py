"""schemas.py — 执行器与 LLM 之间的数据契约（pydantic）。

TestReport：沙箱判卷结果，可直接由 executor_sandbox.py 的 --json 输出构造；
FixRequest：回传给 LLM 的修复请求（Phase 2 里由 TestReport 组装而来）。
"""

from typing import List, Optional

from pydantic import BaseModel, Field


class Failure(BaseModel):
    """单个失败用例。"""

    test_name: str
    error_type: str = "Unknown"
    message: str = ""
    traceback: str = ""


class TestReport(BaseModel):
    """沙箱跑完一轮测试后的完整报告。"""

    passed: int
    total: int
    failed: int
    failures: List[Failure] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    # ---- 扩展字段（沙箱原生输出自带，方便 Phase 2 调试）----
    exit_code: Optional[int] = None
    timed_out: bool = False

    @property
    def all_passed(self) -> bool:
        return self.failed == 0 and self.passed > 0


class FixRequest(BaseModel):
    """把失败信息打包回传给 LLM 的修复请求。"""

    target_files: List[str]
    failure_summary: str
    suggested_fix: str = ""
    retry_round: int = 0

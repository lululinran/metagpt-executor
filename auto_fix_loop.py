#!/usr/bin/env python3
"""auto_fix_loop.py — Phase 2 第一刀：沙箱判卷 → FixRequest 回传 LLM → 修复 → 复验。

流程（每轮）：
  1. 把原产物目录 copytree 到 fix_sessions/<时间戳>/（原目录永不改写）
  2. 沙箱跑 pytest -q → TestReport
  3. 沙箱跑入口冒烟命令（--entry）→ 非零退出 = 一个 pseudo-Failure
     （实证：MetaGPT demo 产物 105 个测试全绿，但 cli.py 空文件导致入口坏死——
      只跑 pytest 永远发现不了，必须冒烟）
  4. 有失败 → 组装 FixRequest（含上下文文件全文）→ LLM 返回 {"files":[...]} → 写入会话目录
  5. 复验，直到通过或达到 --max-rounds

用法：
  python auto_fix_loop.py <产物目录> \
      --entry "python simple_todo_cli/main.py add smoke && python simple_todo_cli/main.py list"

LLM 配置复用 ~/.metagpt/config2.yaml（endpoint/key/model），可用 --model 覆盖。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

import yaml

from executor_sandbox import run_in_sandbox, run_pytest_in_sandbox
from llm_fixer import LLMFixer, apply_patches
from schemas import Failure, FixRequest, TestReport

REPO_ROOT = Path(__file__).resolve().parent
SESSION_ROOT = REPO_ROOT / "fix_sessions"

# 每轮喂给 LLM 的上下文上限：单文件行数 / 文件个数
MAX_CONTEXT_LINES = 400
MAX_CONTEXT_FILES = 30


def load_llm_config(model_override: str | None) -> dict:
    cfg = yaml.safe_load((Path.home() / ".metagpt" / "config2.yaml").read_text(encoding="utf-8"))
    llm = cfg["llm"]
    llm["model"] = model_override or llm["model"]
    return llm


def copy_session(artifact_dir: Path) -> Path:
    SESSION_ROOT.mkdir(exist_ok=True)
    session = SESSION_ROOT / time.strftime("%Y%m%d-%H%M%S")
    shutil.copytree(
        artifact_dir, session,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
    )
    return session


def build_tree(session: Path) -> str:
    return "\n".join(sorted(
        str(p.relative_to(session)) for p in session.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    ))


def collect_context(session: Path, failures: list[Failure]) -> dict[str, str]:
    """收集失败 traceback 里提到的文件 + main.py + todo_cli 包，作为修复上下文。"""
    wanted: set[str] = set()
    for f in failures:
        for m in re.finditer(r"[\w./-]+\.py", f.traceback + " " + f.message):
            wanted.add(_norm_path(m.group(0)))
    # 全项目 .py 都纳入上下文（带文件数上限），避免对特定项目布局硬编码
    for p in session.rglob("*.py"):
        if "__pycache__" not in p.parts:
            wanted.add(str(p.relative_to(session)))

    context: dict[str, str] = {}
    for rel in sorted(wanted)[:MAX_CONTEXT_FILES]:
        f = session / rel
        if not f.is_file():
            continue
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) > MAX_CONTEXT_LINES:
            content = "\n".join(lines[:MAX_CONTEXT_LINES]) + f"\n# ...(截断，共 {len(lines)} 行)"
        else:
            content = "\n".join(lines)
        context[rel] = content
    return context


def _norm_path(p: str) -> str:
    """traceback 里的路径是容器视角（/app/...），转成仓库相对路径。"""
    return p[len("/app/"):] if p.startswith("/app/") else p.lstrip("./")


def smoke_failure(res: dict) -> Failure | None:
    """入口冒烟非零退出 → 转成 pseudo-Failure（test_name=entrypoint_smoke）。"""
    if res["exit_code"] == 0 and not res["timed_out"]:
        return None
    output = (res["stderr"] or "") + "\n" + (res["stdout"] or "")
    tail_lines = [ln for ln in output.splitlines() if ln.strip()][-40:]
    m = re.search(r"^([\w.]*(?:Error|Exception))\b[ :]", output, re.MULTILINE)
    return Failure(
        test_name="entrypoint_smoke",
        error_type=m.group(1) if m else ("TimeoutError" if res["timed_out"] else "NonZeroExit"),
        message=next((ln for ln in tail_lines if "Error" in ln or "error" in ln), ""),
        traceback="\n".join(tail_lines),
    )


def judge(session: Path, entry_cmd: str | None, timeout: int) -> tuple[TestReport, dict, list[Failure]]:
    """跑一轮判卷：pytest + 入口冒烟，返回 (TestReport, 冒烟原始结果, 汇总失败列表)。"""
    report = TestReport(**run_pytest_in_sandbox(session, timeout=timeout))
    failures = list(report.failures)
    if report.failed > 0 and not failures:
        # 兜底：failed>0 却没解析出失败块（如输出形状再变）——把原文末段整体回传，绝不漏报
        failures.append(Failure(
            test_name="pytest_unparsed_failures",
            error_type="TestFailure",
            message=f"pytest 报告 {report.failed} 个失败/错误，但未能解析出 FAILURES/ERRORS 区段",
            traceback=(report.stdout or "")[-3000:],
        ))
    smoke: dict = {}
    if entry_cmd:
        smoke = run_in_sandbox(session, entry_cmd, timeout=timeout)
        sf = smoke_failure(smoke)
        if sf:
            failures.append(sf)
    return report, smoke, failures


def save_round_report(session: Path, rnd: int, report: TestReport, smoke: dict,
                      failures: list[Failure], fixed: list[str], usage: dict) -> Path:
    out = session / "reports" / f"round-{rnd}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({
        "round": rnd,
        "pytest": report.model_dump(),
        "smoke": smoke,
        "failures": [f.model_dump() for f in failures],
        "applied_patches": fixed,
        "llm_usage": usage,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def run_loop_structured(artifact_dir: Path, entry_cmd: str | None, max_rounds: int,
                        fixer: "LLMFixer", budget_tokens: int, timeout: int = 120,
                        verbose: bool = True) -> dict:
    """判卷+修复闭环，返回结构化结果（供 metagpt_executor 组装最终报告）。

    返回: {status, session_dir, rounds: [...], token_total}
    status ∈ all_green | max_rounds_exhausted | budget_exhausted | fixer_error
    """
    log = print if verbose else (lambda *a, **k: None)
    artifact_dir = artifact_dir.resolve()
    session = copy_session(artifact_dir)
    log(f"[loop] 会话目录（可写副本）: {session}")
    log(f"[loop] 原产物目录保持只读不动: {artifact_dir}")

    rounds: list[dict] = []
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    status = "max_rounds_exhausted"

    for rnd in range(1, max_rounds + 1):
        log(f"\n========== 第 {rnd}/{max_rounds} 轮：判卷 ==========")
        report, smoke, failures = judge(session, entry_cmd, timeout)
        log(f"[loop] pytest: {report.failed} failed / {report.passed} passed"
            f" | 冒烟: exit={smoke.get('exit_code')} | 失败合计: {len(failures)}")

        if not failures:
            rounds.append({"round": rnd, "pytest_failed": report.failed,
                           "pytest_passed": report.passed, "smoke_exit": smoke.get("exit_code"),
                           "failures": [], "applied_patches": [], "usage": {}})
            save_round_report(session, rnd, report, smoke, [], [], total_usage)
            status = "all_green"
            log(f"\n[loop] ✅ 第 {rnd} 轮全部通过（pytest + 入口冒烟）。")
            break

        if sum(total_usage.values()) >= budget_tokens:
            status = "budget_exhausted"
            rounds.append({"round": rnd, "pytest_failed": report.failed,
                           "pytest_passed": report.passed, "smoke_exit": smoke.get("exit_code"),
                           "failures": [f.model_dump() for f in failures],
                           "applied_patches": [], "usage": {},
                           "note": f"token 预算 {budget_tokens} 已用完，停止修复"})
            save_round_report(session, rnd, report, smoke, failures, [], total_usage)
            log(f"\n[loop] 💸 token 预算 {budget_tokens} 已用完，停止。")
            break

        fix_request = FixRequest(
            target_files=sorted({
                _norm_path(p) for f in failures
                for p in re.findall(r"[\w./-]+\.py", f.traceback + f.message)
                if (session / _norm_path(p)).is_file()  # 只保留项目内真实存在的文件，滤掉标准库路径
            }),
            failure_summary="\n\n".join(
                f"[{f.error_type}] {f.test_name}\n{f.message}\n{f.traceback}" for f in failures),
            suggested_fix="",
            retry_round=rnd,
        )
        context = collect_context(session, failures)
        log(f"[loop] 回传 LLM 修复（上下文 {len(context)} 个文件, "
            f"target={fix_request.target_files or '由模型判断'}）...")
        try:
            patches, usage = fixer.request_fix(fix_request, context, build_tree(session))
        except Exception as exc:
            log(f"[loop] ⚠️ LLM 修复调用失败: {exc}")
            rounds.append({"round": rnd, "pytest_failed": report.failed,
                           "pytest_passed": report.passed, "smoke_exit": smoke.get("exit_code"),
                           "failures": [f.model_dump() for f in failures],
                           "applied_patches": [], "usage": {"error": str(exc)}})
            save_round_report(session, rnd, report, smoke, failures, [], {"error": str(exc)})
            status = "fixer_error"
            break
        for k in total_usage:
            total_usage[k] += usage.get(k, 0)
        fixed = apply_patches(session, patches)
        log(f"[loop] 已应用补丁: {fixed or '（模型未返回文件）'}")
        log(f"[loop] 累计 token: {total_usage}")
        rounds.append({"round": rnd, "pytest_failed": report.failed,
                       "pytest_passed": report.passed, "smoke_exit": smoke.get("exit_code"),
                       "failures": [f.model_dump() for f in failures],
                       "applied_patches": fixed, "usage": usage})
        save_round_report(session, rnd, report, smoke, failures, fixed, usage)

    return {"status": status, "session_dir": str(session),
            "rounds": rounds, "token_total": total_usage}


def run_loop(artifact_dir: Path, entry_cmd: str | None, max_rounds: int,
             model: str | None, timeout: int) -> int:
    """CLI 入口：组装 fixer 并跑结构化闭环，按状态映射退出码。"""
    llm_cfg = load_llm_config(model)
    fixer = LLMFixer(api_key=llm_cfg["api_key"], base_url=llm_cfg["base_url"],
                     model=llm_cfg["model"])
    print(f"[loop] 修复模型: {llm_cfg['model']} @ {llm_cfg['base_url']}")
    result = run_loop_structured(artifact_dir, entry_cmd, max_rounds, fixer,
                                 budget_tokens=10**9, timeout=timeout)
    if result["status"] != "all_green":
        print(f"\n[loop] ❌ {result['status']}，最后报告见 {Path(result['session_dir']) / 'reports'}")
        return 1
    print(f"\n[loop] ✅ 完成。会话: {result['session_dir']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="沙箱判卷 + LLM 修复 + 复验闭环（Phase 2）")
    parser.add_argument("artifact_dir", help="MetaGPT 产物目录（只读，不会被修改）")
    parser.add_argument("--entry", default=None,
                        help="入口冒烟命令（在沙箱 /app 下执行），缺省跳过冒烟")
    parser.add_argument("--max-rounds", type=int, default=3, help="最大修复轮数（默认 3）")
    parser.add_argument("--model", default=None, help="覆盖 config2.yaml 里的模型名")
    parser.add_argument("--timeout", type=int, default=120, help="沙箱单命令超时秒数")
    args = parser.parse_args()
    return run_loop(Path(args.artifact_dir), args.entry, args.max_rounds,
                    args.model, args.timeout)


if __name__ == "__main__":
    sys.exit(main())

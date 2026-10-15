#!/usr/bin/env python3
"""metagpt_executor.py — 一条命令：想法 → MetaGPT 生成 → 沙箱判卷 → LLM 修复 → 验收报告。

两种模式：
  1. 全流程（生成即判卷）：  python metagpt_executor.py "Create a simple todo CLI app" \
                                 --entry "python simple_todo_cli/main.py --help"
  2. 已有产物判卷修复：      python metagpt_executor.py --from-artifact <产物目录>

判卷 = 沙箱 pytest + 入口冒烟（--entry 可省略，会自动探测 main.py/cli.py 并用 --help 冒烟）。
修复受 --budget-tokens 预算闸门约束。最终报告写到会话目录 executor_report.json。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from auto_fix_loop import load_llm_config, run_loop_structured
from llm_fixer import LLMFixer

REPO_ROOT = Path(__file__).resolve().parent
BUILD_ROOT = REPO_ROOT / "builds"


def find_metagpt_bin() -> Path:
    """venv 里找 metagpt CLI；sys.executable 在 Homebrew venv 会解析到框架路径，用 sys.prefix。"""
    candidate = Path(sys.prefix) / "bin" / "metagpt"
    if candidate.exists():
        return candidate
    import shutil

    found = shutil.which("metagpt")
    return Path(found) if found else candidate


METAGPT_BIN = find_metagpt_bin()

STATUS_EXIT = {"all_green": 0, "max_rounds_exhausted": 1, "budget_exhausted": 4,
               "fixer_error": 3, "generation_failed": 5}


def discover_entry(project_dir: Path) -> str | None:
    """自动探测入口：优先 main.py / cli.py，用 `python <入口> --help` 冒烟。

    --help 足以抓住"模块坏了/缺文件"类缺陷（import 失败即非零退出）。
    """
    for pat in ("*/main.py", "main.py", "*/cli.py", "*/__main__.py"):
        hits = sorted(p for p in project_dir.glob(pat) if "__pycache__" not in p.parts)
        if hits:
            rel = hits[0].relative_to(project_dir).as_posix()
            return f"python {rel} --help"
    return None


def generate(idea: str, project_name: str | None, run_tests: bool) -> dict:
    """跑 MetaGPT 生成项目，返回 {build_dir, exit_code, duration_s, project_dir}。"""
    BUILD_ROOT.mkdir(exist_ok=True)
    build = BUILD_ROOT / time.strftime("%Y%m%d-%H%M%S")
    build.mkdir()
    cmd = [str(METAGPT_BIN), idea]
    if project_name:
        cmd += ["--project-name", project_name]
    if run_tests:
        cmd += ["--run-tests"]
    print(f"[executor] MetaGPT 生成中（日志: {build / 'metagpt.log'}）...")
    print(f"[executor] {' '.join(cmd)}")
    start = time.time()
    with open(build / "metagpt.log", "w") as lf:
        try:
            proc = subprocess.run(cmd, cwd=build, stdout=lf,
                                  stderr=subprocess.STDOUT, timeout=3600)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            exit_code = -9
        except FileNotFoundError as exc:
            print(f"[executor] ❌ 找不到 metagpt CLI: {exc}", file=sys.stderr)
            exit_code = -127
    duration = round(time.time() - start, 1)

    project_dir = None
    ws = build / "workspace"
    projects = sorted(p for p in ws.iterdir() if p.is_dir()) if ws.is_dir() else []
    if projects:
        project_dir = str(projects[0])
    return {"build_dir": str(build), "exit_code": exit_code, "duration_s": duration,
            "project_dir": project_dir}


def human_summary(final: dict) -> str:
    lines = ["", "=" * 62, "执行器报告", "=" * 62]
    gen = final.get("generate")
    if gen:
        lines.append(f"生成: {gen['duration_s']}s | metagpt 退出码 {gen['exit_code']} | "
                     f"项目: {gen['project_dir']}")
    lines.append(f"判卷+修复: {len(final['rounds'])} 轮 | 状态: {final['status']} | "
                 f"token: {final['token_total'].get('prompt_tokens', 0)}P/"
                 f"{final['token_total'].get('completion_tokens', 0)}C")
    for r in final["rounds"]:
        line = (f"  第{r['round']}轮 pytest {r['pytest_failed']}F/{r['pytest_passed']}P"
                f" 冒烟 exit={r['smoke_exit']}")
        if r.get("applied_patches"):
            line += f" → 修复 {r['applied_patches']}"
        if r["pytest_failed"] == 0 and r["smoke_exit"] == 0:
            line += " ✅"
        lines.append(line)
    lines.append(f"会话: {final['session_dir']}")
    lines.append(f"报告: {Path(final['session_dir']) / 'executor_report.json'}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="想法 → MetaGPT → 沙箱判卷 → LLM 修复 → 报告")
    parser.add_argument("idea", nargs="?", help="给 MetaGPT 的一句话需求")
    parser.add_argument("--from-artifact", default=None, help="跳过生成，直接对已有产物判卷修复")
    parser.add_argument("--entry", default=None, help="入口冒烟命令（缺省自动探测 main.py --help）")
    parser.add_argument("--max-rounds", type=int, default=3)
    parser.add_argument("--budget-tokens", type=int, default=300000,
                        help="修复阶段 token 预算闸门（默认 300k）")
    parser.add_argument("--model", default=None)
    parser.add_argument("--timeout", type=int, default=120, help="沙箱单命令超时秒数")
    parser.add_argument("--project-name", default=None)
    parser.add_argument("--no-run-tests", action="store_true", help="生成阶段不让 MetaGPT 写测试")
    args = parser.parse_args()

    gen_info = None
    if args.from_artifact:
        artifact = Path(args.from_artifact).resolve()
        if not artifact.is_dir():
            print(f"[executor] 产物目录不存在: {artifact}", file=sys.stderr)
            return 2
    else:
        if not args.idea:
            parser.error("需要提供 idea，或用 --from-artifact 指定已有产物")
        gen_info = generate(args.idea, args.project_name, not args.no_run_tests)
        if not gen_info["project_dir"]:
            print(f"[executor] ❌ MetaGPT 未产出项目目录（exit={gen_info['exit_code']}），"
                  f"日志: {gen_info['build_dir']}/metagpt.log", file=sys.stderr)
            final = {"idea": args.idea, "model": None, "generate": gen_info,
                     "entry_cmd": None, "status": "generation_failed",
                     "session_dir": gen_info["build_dir"], "rounds": [],
                     "token_total": {}}
            Path(gen_info["build_dir"], "executor_report.json").write_text(
                json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
            print(human_summary(final))
            return STATUS_EXIT["generation_failed"]
        artifact = Path(gen_info["project_dir"])

    entry = args.entry or discover_entry(artifact)
    print(f"[executor] 冒烟命令: {entry or '（未探测到入口，跳过冒烟）'}")

    llm_cfg = load_llm_config(args.model)
    fixer = LLMFixer(api_key=llm_cfg["api_key"], base_url=llm_cfg["base_url"],
                     model=llm_cfg["model"])
    print(f"[executor] 修复模型: {llm_cfg['model']} @ {llm_cfg['base_url']}")

    result = run_loop_structured(artifact, entry, args.max_rounds, fixer,
                                 budget_tokens=args.budget_tokens, timeout=args.timeout)
    final = {"idea": args.idea, "model": llm_cfg["model"], "generate": gen_info,
             "entry_cmd": entry, **result}
    report_path = Path(result["session_dir"]) / "executor_report.json"
    report_path.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print(human_summary(final))
    return STATUS_EXIT.get(final["status"], 1)


if __name__ == "__main__":
    sys.exit(main())

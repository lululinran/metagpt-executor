#!/usr/bin/env python3
"""cost_report.py — 汇总修复会话与生成构建的成本账本。

数据源：
  fix_sessions/*/reports/round-*.json   每轮 llm_usage（prompt/completion tokens、补丁数）
  fix_sessions/*/executor_report.json   会话最终状态
  builds/*/metagpt.log                  生成侧 LLM 调用次数（metagpt 价目表外，
                                        只能数调用次数，token 以中转站后台为准）

用法：
  python cost_report.py            # 打印 Markdown 账本
  python cost_report.py --json out.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SESSION_ROOT = REPO_ROOT / "fix_sessions"
BUILD_ROOT = REPO_ROOT / "builds"


def aggregate_sessions(root: Path) -> list[dict]:
    """每个修复会话一行：状态 + 各轮 token/补丁 + 合计。"""
    rows: list[dict] = []
    for sess in sorted(p for p in root.glob("*") if p.is_dir()):
        entry: dict = {"session": sess.name, "status": None,
                       "rounds": [], "prompt": 0, "completion": 0}
        rep = sess / "executor_report.json"
        if rep.is_file():
            try:
                entry["status"] = json.loads(rep.read_text(encoding="utf-8")).get("status")
            except json.JSONDecodeError:
                pass
        for rf in sorted((sess / "reports").glob("round-*.json")):
            try:
                r = json.loads(rf.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            u = r.get("llm_usage", {})
            # 口径规则：failures 为空的轮是纯判卷轮，设计上不可能调过 LLM。
            # 早期版本曾把累计 usage 回显进判卷轮报告，这里按 0 计，避免重复统计。
            judge_only = (r.get("failures") == [])
            row = {"round": r.get("round"),
                   "prompt": 0 if judge_only else (u.get("prompt_tokens", 0) or 0),
                   "completion": 0 if judge_only else (u.get("completion_tokens", 0) or 0),
                   "patches": len(r.get("applied_patches", []))}
            if "error" in u:
                row["error"] = str(u["error"])[:80]
            entry["rounds"].append(row)
            entry["prompt"] += row["prompt"]
            entry["completion"] += row["completion"]
        rows.append(entry)
    return rows


def aggregate_builds(root: Path) -> list[dict]:
    """每个生成构建一行：metagpt LLM 调用次数 + 生成耗时。"""
    rows: list[dict] = []
    for build in sorted(p for p in root.glob("*") if p.is_dir()):
        log = build / "metagpt.log"
        if not log.is_file():
            continue
        calls = sum(1 for ln in log.read_text(encoding="utf-8", errors="replace").splitlines()
                    if "not found in TOKEN_COSTS" in ln)
        gen_duration = None
        rep = build / "executor_report.json"
        if rep.is_file():
            try:
                gen_duration = json.loads(rep.read_text(encoding="utf-8")).get(
                    "generate", {}).get("duration_s")
            except json.JSONDecodeError:
                pass
        rows.append({"build": build.name, "metagpt_llm_calls": calls,
                     "generate_duration_s": gen_duration})
    return rows


def render_markdown(sessions: list[dict], builds: list[dict]) -> str:
    lines = ["### 修复侧（fix_sessions，token 由执行器实测）", "",
             "| 会话 | 状态 | 轮次 | prompt | completion | 补丁数 |", "|---|---|---|---|---|---|"]
    tp = tc = 0
    for s in sessions:
        rounds_desc = "、".join(
            f"R{r['round']}" + (f"({r['error'][:24]}…)" if r.get("error") else "")
            for r in s["rounds"]) or "-"
        patches = sum(r["patches"] for r in s["rounds"])
        lines.append(f"| {s['session']} | {s['status'] or '-'} | {rounds_desc} "
                     f"| {s['prompt']:,} | {s['completion']:,} | {patches} |")
        tp += s["prompt"]
        tc += s["completion"]
    lines.append(f"| **合计** |  |  | **{tp:,}** | **{tc:,}** |  |")

    lines += ["", "### 生成侧（builds，metagpt 价目表外，token 以中转站后台为准）", "",
              "| 构建 | metagpt LLM 调用次数 | 生成耗时(s) |", "|---|---|---|"]
    for b in builds:
        dur = f"{b['generate_duration_s']:.1f}" if b["generate_duration_s"] else "-"
        lines.append(f"| {b['build']} | {b['metagpt_llm_calls']} | {dur} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="修复/生成成本账本")
    parser.add_argument("--json", dest="json_path", default=None)
    parser.add_argument("--sessions-root", type=Path, default=SESSION_ROOT)
    parser.add_argument("--builds-root", type=Path, default=BUILD_ROOT)
    args = parser.parse_args()

    sessions = aggregate_sessions(args.sessions_root)
    builds = aggregate_builds(args.builds_root)
    output = render_markdown(sessions, builds)
    print(output)
    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps({"sessions": sessions, "builds": builds},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[cost] JSON 已写入 {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

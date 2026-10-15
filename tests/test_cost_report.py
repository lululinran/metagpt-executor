"""cost_report 聚合逻辑的离线测试（tmp_path 造数据，不依赖真实会话）。"""

import json

from cost_report import aggregate_builds, aggregate_sessions, render_markdown


def _make_session(root, name, rounds, status=None):
    sess = root / "fix_sessions" / name
    (sess / "reports").mkdir(parents=True)
    for rnd, usage, failures in rounds:
        (sess / "reports" / f"round-{rnd}.json").write_text(
            json.dumps({"round": rnd, "llm_usage": usage, "failures": failures,
                        "applied_patches": ["a.py"] if usage.get("prompt_tokens") else []}),
            encoding="utf-8")
    if status:
        (sess / "executor_report.json").write_text(
            json.dumps({"status": status}), encoding="utf-8")


def test_aggregate_sessions_sums_rounds(tmp_path):
    _make_session(tmp_path, "s1", [(1, {"prompt_tokens": 100, "completion_tokens": 50},
                                    [{"t": 1}]),
                                   # 判卷轮：历史版本会把累计 usage 回显，聚合时必须计 0
                                   (2, {"prompt_tokens": 100, "completion_tokens": 50}, [])],
                  status="all_green")
    _make_session(tmp_path, "s2", [(1, {"error": "boom"}, [{"t": 2}])])
    rows = aggregate_sessions(tmp_path / "fix_sessions")
    assert rows[0]["prompt"] == 100 and rows[0]["completion"] == 50
    assert rows[0]["status"] == "all_green"
    assert rows[1]["rounds"][0]["error"] == "boom"
    assert rows[1]["prompt"] == 0


def test_aggregate_builds_counts_calls(tmp_path):
    build = tmp_path / "builds" / "b1"
    build.mkdir(parents=True)
    (build / "metagpt.log").write_text(
        "x\nnot found in TOKEN_COSTS.\ny\nnot found in TOKEN_COSTS.\n", encoding="utf-8")
    (build / "executor_report.json").write_text(
        json.dumps({"generate": {"duration_s": 12.5}}), encoding="utf-8")
    rows = aggregate_builds(tmp_path / "builds")
    assert rows == [{"build": "b1", "metagpt_llm_calls": 2, "generate_duration_s": 12.5}]


def test_render_markdown_contains_totals(tmp_path):
    _make_session(tmp_path, "s1", [(1, {"prompt_tokens": 1000, "completion_tokens": 2000},
                                    [{"t": 1}])], status="all_green")
    md = render_markdown(aggregate_sessions(tmp_path / "fix_sessions"), [])
    assert "**1,000**" in md and "**2,000**" in md and "all_green" in md

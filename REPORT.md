# MetaGPT 执行器验收报告（Phase 1–3）

- 日期：2026-10-15
- 环境：macOS (Darwin 24.6, Apple Silicon) / Docker 28.5.1 / Python 3.11.17 (Homebrew venv) / metagpt 0.8.2 / Node 22.22.3 + pnpm 12.11.2
- LLM：局域网中转站（OpenAI 兼容方言），模型 `deepseek-v4-flash`（推理模型，flash 档）
- **架构定位**：MetaGPT 仅在生成阶段被调用（CLI 子进程），仓库代码对其库层零 import；判卷、修复与编排全部自研于 MetaGPT 框架之外（`llm_fixer` 直接调 OpenAI 兼容端点）。因此准确的表述是**"基于 MetaGPT 构建的执行闭环编排层"**，而非"MetaGPT 二次开发"。
- 一句话结论：**已跑通"想法 → MetaGPT 生成 → Docker 沙箱判卷（pytest + 入口冒烟）→ FixRequest 回传 LLM 修复 → 复验 → 结构化报告"的完整闭环，并用两次真实生成的缺陷完成了端到端验证。**

## 1. 交付物总览

| 组件 | 文件 | 说明 |
| --- | --- | --- |
| 沙箱执行器 | `executor_sandbox.py` | 每次全新 `python:3.11-slim` 容器：代码只读挂载、容器级禁网、120s 超时强杀、跑完销毁；`run_in_sandbox()` 通用命令原语 + pytest 包装 + 解析器 |
| 数据契约 | `schemas.py` | `TestReport` / `Failure` / `FixRequest`（pydantic），直接由沙箱 JSON 输出构造 |
| LLM 修复器 | `llm_fixer.py` | FixRequest → OpenAI 兼容端点 → `{"files":[...]}` 补丁；路径安全校验；截断自动重试 |
| 修复闭环 | `auto_fix_loop.py` | 判卷 → 回传 → 落盘 → 复验；会话副本机制保证原产物永不被改写 |
| 一条命令编排 | `metagpt_executor.py` | idea 进 → 修好的 app 出；`--from-artifact` / `--budget-tokens` 预算闸门 / 退出码可接 CI |
| 成本账本 | `cost_report.py` | 扫描会话/构建产物，输出 Markdown + JSON 账本 |
| 测试 | `tests/`（14 项离线） | 解析器、路径安全、聚合口径、prompt 组装等，不起 Docker |

## 2. Phase 1：环境与官方 demo

环境检查与安装的完整踩坑记录见 [README「安装踩坑记录」](README.md#安装踩坑记录2026-10-metagpt-082)，要点：

1. `lancedb==0.4.0` 已从 PyPI 下架 → `pip install metagpt` 必然失败并回退到 2023 年老版本；用修复版依赖清单 + `--no-deps` 安装。
2. v0.8.2 代码 import 了 PyPI 元数据缺失的 5 个包（agentops 等），对照 tag 清单补装。
3. `typer==0.9.0` 与 click≥8.2 不兼容 → click 降 8.1。
4. 老 sdist 在新 setuptools（无 pkg_resources）下构建失败 → `PIP_CONSTRAINT` 注入 `setuptools<80`。

官方 demo（`metagpt "Create a simple todo CLI app" --run-tests`）：**22.4 分钟，52 次 LLM 调用**，产物齐全（PRD/系统设计/任务拆分/代码/测试），其自生成 **105 个测试全部通过**。遗留缺陷：`todo_cli/cli.py` 为 0 字节（疑似生成期 `max_token=8192` 被截断，已调至 16384），入口坏死——没有任何测试覆盖该模块。

## 3. Phase 2：判卷设计与两个实战案例

### 判卷 = pytest + 入口冒烟

- pytest 在禁网沙箱里跑 `-q`；
- **入口冒烟**：在沙箱里真实执行入口命令（自动探测 `main.py`/`cli.py`，缺省 `--help`）。
- 冒烟存在的实证理由：案例一的产物 105 个单测全绿、但入口坏死——"测试全绿 ≠ 交付物可用"。

### 案例一：0 字节 cli.py（demo 产物，会话 `20261015-174332`）

| 轮次 | pytest | 冒烟 | 动作 | token (P/C) |
| --- | --- | --- | --- | --- |
| 1 | 0F/105P | exit=1，`ImportError: cannot import name 'TodoCLI'` | FixRequest 回传 → 生成 350 行 `todo_cli/cli.py` | 9,132 / 13,425 |
| 2（纯复验） | 0F/105P | exit=0 | ✅ 通过，提前停止 | 0（契约：复验轮不调 LLM） |

修复后的 app 在沙箱内端到端复测：`add ×2 → list → done → list --all` 输出正确。

> 另有会话 `20261015-175640`：对**同一个坏产物**的第二次独立完整修复（用于验收 `--from-artifact` 编排模式），而非对本次修复的"复验"——所以它又消耗了 9,132/14,608 tokens 并产出 1 个补丁。同一提示词两次修复的 completion 差异（13,425 vs 14,608）来自推理模型的非确定性（temperature=0 亦然）。

### 案例二：收集期 import 错误（二次生成产物）

MetaGPT 二次生成的产物：包结构 `simple_todo_cli/simple_todo_cli/` + 根目录 `tests/`，但缺 conftest/pyproject，pytest 时 rootdir 不在 `sys.path`，收集期报 `ModuleNotFoundError`。

| 轮次 | pytest | 冒烟 | 动作 | token (P/C) |
| --- | --- | --- | --- | --- |
| 1 | 2 errors（ERRORS 区段） | exit=0 | 回传全量 12 个 py 文件 → 补 `conftest.py` 并调整包结构（8 个文件补丁） | 12,378 / 15,045 |
| 2 | 0F/38P | exit=0 | ✅ 通过 | 0 |

### 事故自述：一次漏报与三层加固

首次全流程跑时，判卷层把"pytest 2 errors"误判为 `all_green`——因为失败输出在 **ERRORS 区段**（收集期错误），而解析器只认 FAILURES 区段。加固（含真实输出形状的离线回归测试）：

1. `parse_failures` 同时匹配 `FAILURES|ERRORS`；
2. `judge()` 兜底：`failed > 0` 却解析不出失败块时，把 pytest 原文末段整体包装为 pseudo-Failure 回传——**结构性保证漏报不再发生**；
3. 成本聚合口径规则：`failures` 为空的轮是纯判卷轮，绝无 LLM 调用。

另修复：推理模型把 `max_tokens` 烧在思考上导致 content 为空/JSON 截断（`finish_reason=length`），现在自动预算翻倍重试一次。

## 4. Phase 3：成本账本

修复侧 token 由执行器逐轮实测（`cost_report.py` 自动汇总，JSON 存 `docs/cost_ledger.json`）：

| 会话 | 状态 | prompt | completion | 补丁数 |
| --- | --- | ---: | ---: | ---: |
| 20261015-173624（R1 content 为空） | 失败重试 | 0* | 0* | 0 |
| 20261015-173911（R1 JSON 截断） | 失败重试 | 0* | 0* | 0 |
| 20261015-174332（案例一） | all_green | 9,132 | 13,425 | 1 |
| 20261015-175640（同产物独立复跑²） | all_green | 9,132 | 14,608 | 1 |
| 20261015-181406（漏报事故轮） | all_green† | 0 | 0 | 0 |
| 20261015-181837（案例二） | all_green | 12,378 | 15,045 | 8 |
| **合计** | | **30,642** | **43,078** | |

\* 早期版本异常路径未捕获 usage，实际有消耗，以中转站后台为准。
† 该轮为漏报事故产物，误判已由三层加固修复。
² 独立复跑 ≠ 复验：该会话是对同一坏产物的第二次完整修复（编排模式验收）；会话内第 2 轮复验按契约不调 LLM，其报告中的 usage 字段是修复前旧版本的累计值回显，聚合口径已按 `failures==[] ⇒ 0` 规则校正。

生成侧：metagpt 价目表外模型不计量，按调用次数/时长记录——Phase 1 官方 demo 52 次调用 / 1342s；编排全流程构建 24 次调用 / 746.4s。整体画像：**一次"生成+判卷+修复"的修复侧成本 ≈ 2–3 万 tokens，约为生成侧调用次数的一半**。

## 5. 已知限制与下一步

- 冒烟目前是 `--help` / 固定命令级别，不做语义级验收（如输出内容断言）；
- 生成侧 token 不可见（metagpt 对价目表外模型计 0），完整成本依赖中转站后台；
- 单模型单策略：多修复模型仲裁、失败聚类的选择性回传（只喂相关文件）未做；
- 沙箱资源上限固定（mem 1g），无并行判卷；
- 测试全绿但语义错误的产物（测试本身也错）需要人工介入——这是所有自动判卷系统的共同边界。

## 6. 复现

```bash
# 环境与安装（含 metagpt 修复）
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # 见 README 安装踩坑记录

# 离线测试
pytest tests -q

# 沙箱验收（必挂测试被完整捕获）
python executor_sandbox.py examples/failing_demo --json examples/failing_demo/report.json

# 一条命令全流程（idea → 修好的 app → 报告）
python metagpt_executor.py "Create a simple todo CLI app"

# 已有产物判卷修复 + 成本账本
python metagpt_executor.py --from-artifact <产物目录>
python cost_report.py
```

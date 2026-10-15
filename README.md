# metagpt-executor

> 一句话目标：让 MetaGPT 负责写代码，Docker 沙箱负责判卷——失败信息结构化回传给 LLM 修复，循环直到测试通过。

## 架构草图

```
┌─────────────┐  生成代码   ┌─────────────────────────┐  TestReport  ┌──────────────┐
│   MetaGPT   │ ─────────► │    Docker 沙箱执行器     │ ───────────► │  LLM 修复器   │
│  (PRD→代码) │            │  · python:3.11-slim     │  (pydantic)  │  (生成补丁)   │
└─────────────┘            │  · 代码目录只读挂载 /app │              └──────┬───────┘
      ▲                    │  · 禁网 · 120s 超时      │                     │
      │                    │  · 跑完即销毁容器        │     FixRequest      │
      │                    └─────────────────────────┘ ◄───────────────────┘
      └───────────────────── 下一轮代码 (retry_round + 1) ─────────────────
```

## 目录结构

```
metagpt-executor/
├── README.md
├── requirements.txt
├── requirements-metagpt-fix.txt  # 修复 lancedb pin 后的 metagpt 依赖清单（见"安装踩坑记录"）
├── build-constraints.txt         # sdist 构建期约束（setuptools<80）
├── conftest.py                # 让 tests/ 能从仓库根导入 schemas
├── executor_sandbox.py        # Docker 沙箱执行器（独立组件，Phase 1 不接 MetaGPT）
├── schemas.py                 # TestReport / FixRequest 数据契约（pydantic）
├── examples/failing_demo/     # 必挂测试样例（验收沙箱用：一过一挂）
│   ├── app.py
│   ├── test_app.py
│   └── report.json            # 沙箱真实跑出的结构化报告（第 5 步 → 第 6 步的衔接证据）
└── tests/test_schemas.py      # 验收：用沙箱真实输出构造合法 TestReport / FixRequest
```

## 快速开始

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 沙箱验收：跑一个必挂的测试，确认能捕获 test 名 + 报错类型 + traceback
python executor_sandbox.py examples/failing_demo --json examples/failing_demo/report.json

# 数据结构验收：用上一步的真实输出构造 TestReport
pytest tests -q
```

## 安装踩坑记录（2026-10，metagpt 0.8.2）

`pip install metagpt` 在 0.8.2 上是坏的，根因与解法：

1. **`lancedb==0.4.0` 已从 PyPI 下架**（现存最老 0.14.0），pip 解析失败后会一路回退到 2023 年的 metagpt 0.3.x，并死在 `pandas==1.4.1` 的源码编译上。
   → 解法：`requirements-metagpt-fix.txt` 手工修好依赖清单（lancedb→0.14.0），先装它，再 `pip install --no-deps metagpt==0.8.2`。
2. **v0.8.2 tag 的代码还 import 了 `agentops` 等 5 个 PyPI 元数据里没有的包**（agentops、boto3、spark_ai_python、tree_sitter、tree_sitter_python）。
   → 解法：对照 [v0.8.2 tag requirements.txt](https://github.com/FoundationAgents/MetaGPT/blob/v0.8.2/requirements.txt) 手工补装。
3. **typer==0.9.0 与 click≥8.2 不兼容**（CLI 报 `Secondary flag is not valid for non-boolean flag`）。
   → 解法：`pip install "click<8.2"`。
4. 老包 sdist 构建在新 setuptools（≥80，已移除 pkg_resources）下会挂 → `build-constraints.txt`（`setuptools<80`）经 `PIP_CONSTRAINT` 注入构建环境。

一键复现：

```bash
PIP_CONSTRAINT=build-constraints.txt pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
    -r requirements-metagpt-fix.txt
PIP_CONSTRAINT=build-constraints.txt pip install --no-deps metagpt==0.8.2
PIP_CONSTRAINT=build-constraints.txt pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
    "agentops~=0.3.21" "boto3~=1.34.69" "spark_ai_python~=0.3.30" "tree_sitter~=0.23.2" "tree_sitter_python~=0.23.2"
pip install "click<8.2"
```

> 已知残留告警（不影响 demo）：metagpt 仍会抱怨 lancedb/setuptools 版本；huggingface-hub 想要新 click、google-generativeai/grpcio-tools 想要老 protobuf——均不在本项目的调用路径上。

## 沙箱设计要点

| 要求 | 实现 |
| --- | --- |
| 全新容器 | 每次运行 `containers.run` 新建 `python:3.11-slim`，结束 `force remove` |
| 只读挂载 | 代码目录以 `mode=ro` 挂到 `/app`，测试改不了被测代码 |
| 禁网 | 容器级 `network_disabled=True`；pytest 由宿主预下载的 wheel 离线安装 |
| 120s 超时 | `container.wait(timeout=120)`，超时 `kill`，报告标记 `timed_out` |
| 失败捕获 | 解析 pytest `-q` 的 FAILURES 区段，输出 test 名 / 报错类型 / traceback，脚本不崩 |

## 路线图

- [x] Phase 1：环境检查 / MetaGPT 跑通官方 demo / 沙箱 MVP / 数据结构
- [ ] Phase 2：MetaGPT 产出 → 沙箱自动判卷 → FixRequest 回传 → 自动修复闭环
- [ ] Phase 3：多轮修复策略、token 成本统计、验收报告

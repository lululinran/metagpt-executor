#!/usr/bin/env python3
"""executor_sandbox.py — 独立的 Docker 沙箱执行器（Phase 1，先不接 MetaGPT）。

输入一个代码目录，脚本会：
  1. 起一个全新的 python:3.11-slim 容器（每次都是干净环境）；
  2. 代码目录以「只读」方式挂载到 /app；
  3. 容器级禁网（network_disabled），测试期间无任何网络；
  4. 容器内离线安装 pytest（宿主机预下载的 wheel，只读挂载到 /wheels），跑 pytest -q；
  5. 默认 120 秒超时（--timeout 可覆盖），超时强杀；
  6. 无论通过/失败/超时，容器一律销毁（finally 里 force remove）。

输出：
  - 人类可读报告：失败测试名 + 报错类型 + traceback；
  - 可选 --json <path>：结构化结果，字段与 schemas.TestReport 对齐，
    供 Phase 2 直接构造 TestReport 回传给 LLM。

用法：
  python executor_sandbox.py <代码目录> [--timeout 120] [--json out.json]

退出码：
  0 = 沙箱流程完整，失败信息被「捕获」（哪怕测试本身失败）；
  1 = 测试超时被强杀；
  2 = 沙箱自身故障（Docker 没启动 / 镜像问题等）——只有这才是「崩」。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import docker
import requests

BASE_IMAGE = "python:3.11-slim"
CONTAINER_WORKDIR = "/app"
WHEEL_MOUNT = "/wheels"
DEFAULT_TIMEOUT = 120  # 秒
MEM_LIMIT = "1g"

REPO_ROOT = Path(__file__).resolve().parent
WHEEL_DIR = REPO_ROOT / ".wheels"  # pytest 离线安装包缓存（已 gitignore）

# pytest 失败用例标题形如: _________________ test_this_must_fail _________________
HEADER_RE = re.compile(r"^\s*_{10,}\s+(.+?)\s+_{10,}\s*$", re.MULTILINE)


def get_docker_client() -> docker.DockerClient:
    """连接 Docker daemon；默认 socket 不通时回退到 docker CLI 当前 context。"""
    try:
        client = docker.from_env(timeout=60)
        client.ping()
        return client
    except Exception:
        pass
    try:
        host = subprocess.run(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if host:
            client = docker.DockerClient(base_url=host, timeout=60)
            client.ping()
            return client
    except Exception as exc:
        raise RuntimeError(
            "无法连接 Docker daemon。请先启动 Docker Desktop（open -a Docker）再试。"
        ) from exc
    raise RuntimeError("无法连接 Docker daemon。请先启动 Docker Desktop（open -a Docker）。")


def ensure_wheels() -> Path:
    """容器内禁网，pytest 必须离线装：宿主机提前把 wheel 下载好（一次性，需要联网）。

    --python-version 3.11 --only-binary=:all: 保证 wheel 与 python:3.11-slim 兼容。
    """
    WHEEL_DIR.mkdir(exist_ok=True)
    if any(WHEEL_DIR.glob("pytest-*.whl")):
        return WHEEL_DIR
    print(f"[sandbox] 首次运行：下载 pytest wheel 到 {WHEEL_DIR}（只需一次）...")
    subprocess.run(
        [sys.executable, "-m", "pip", "download",
         "--python-version", "3.11", "--only-binary=:all:",
         "-d", str(WHEEL_DIR), "pytest"],
        check=True,
    )
    return WHEEL_DIR


def ensure_image(client: docker.DockerClient) -> None:
    try:
        client.images.get(BASE_IMAGE)
    except docker.errors.ImageNotFound:
        print(f"[sandbox] 拉取基础镜像 {BASE_IMAGE}（首次较慢）...")
        client.images.pull(BASE_IMAGE)


PYTEST_CMD = ("pip install --no-index --find-links=" + WHEEL_MOUNT
              + " -q pytest && pytest -q")


def run_in_sandbox(code_dir: Path, command: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """在全新禁网容器里执行一段 shell 命令（工作目录 /app，代码只读挂载）。

    返回原始结果: {exit_code, stdout, stderr, duration, timed_out}
    """
    code_dir = code_dir.resolve()
    if not code_dir.is_dir():
        raise RuntimeError(f"代码目录不存在: {code_dir}")

    wheels = ensure_wheels()
    client = get_docker_client()
    ensure_image(client)

    print(f"[sandbox] 启动容器: image={BASE_IMAGE}, /app={code_dir} (ro), network=disabled, timeout={timeout}s")
    print(f"[sandbox] 命令: {command}")
    container = None
    start = time.time()
    try:
        container = client.containers.run(
            image=BASE_IMAGE,
            command=["sh", "-c", command],
            volumes={
                str(code_dir): {"bind": CONTAINER_WORKDIR, "mode": "ro"},  # 只读挂载
                str(wheels): {"bind": WHEEL_MOUNT, "mode": "ro"},
            },
            working_dir=CONTAINER_WORKDIR,
            network_disabled=True,  # 容器级禁网
            mem_limit=MEM_LIMIT,
            environment={"PYTHONDONTWRITEBYTECODE": "1"},
            detach=True,  # 分离模式，自己控制超时与销毁
        )

        timed_out = False
        try:
            result = container.wait(timeout=timeout)
            exit_code = int(result.get("StatusCode", -1))
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError):
            timed_out = True
            exit_code = -1
            try:
                container.kill()
            except docker.errors.NotFound:
                pass

        duration = time.time() - start
        # docker SDK 的 logs() 不支持 demux（那是 exec_run 的参数），分两次取
        stdout_b = container.logs(stdout=True, stderr=False)
        stderr_b = container.logs(stdout=False, stderr=True)
        return {
            "exit_code": exit_code,
            "stdout": (stdout_b or b"").decode("utf-8", errors="replace"),
            "stderr": (stderr_b or b"").decode("utf-8", errors="replace"),
            "duration": round(duration, 2),
            "timed_out": timed_out,
        }
    except docker.errors.DockerException as exc:
        raise RuntimeError(f"Docker 调用失败: {exc}") from exc
    finally:
        if container is not None:
            try:
                container.remove(force=True)  # 跑完销毁
                print("[sandbox] 容器已销毁")
            except docker.errors.NotFound:
                pass


def run_pytest_in_sandbox(code_dir: Path, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """在全新禁网容器里跑 pytest -q，返回结构化报告 dict。"""
    code_dir = code_dir.resolve()
    if not list(code_dir.glob("test_*.py")) and not list(code_dir.glob("*_test.py")):
        print(f"[sandbox] 警告: {code_dir} 下没找到 test_*.py / *_test.py，pytest 可能收集不到用例")
    res = run_in_sandbox(code_dir, PYTEST_CMD, timeout)
    return build_report(
        stdout=res["stdout"],
        stderr=res["stderr"],
        exit_code=res["exit_code"],
        duration=res["duration"],
        timed_out=res["timed_out"],
        timeout=timeout,
    )


def _count(pattern: str, text: str) -> int:
    m = re.search(pattern, text)
    return int(m.group(1)) if m else 0


def parse_failures(stdout: str) -> list:
    """从 pytest -q 输出解析每个失败用例：测试名 / 报错类型 / 信息 / traceback。

    注意：pytest 对普通 assert 失败不打印 "AssertionError" 字样（E 行直接是断言内容），
    此时按 pytest 语义归为 AssertionError。
    """
    failures: list = []
    # 失败可能在 FAILURES（断言/用例失败）或 ERRORS（收集期 import 失败等）区段
    sec = re.search(r"=+\s*(?:FAILURES|ERRORS)\s*=+", stdout)
    if not sec:
        return failures
    section = stdout[sec.end():]
    # 截掉结尾的 warnings summary / short test summary / 最终统计行
    tail = re.search(r"\n=+\s*(?:warnings summary|short test summary info)\s*=+", section)
    if not tail:
        tail = re.search(r"\n=+.*in \d[\d\.]*s.*=+\s*$", section)
    if tail:
        section = section[: tail.start()]

    headers = list(HEADER_RE.finditer(section))
    for i, m in enumerate(headers):
        name = m.group(1).strip()
        body = section[m.end(): headers[i + 1].start() if i + 1 < len(headers) else len(section)]
        e_lines = [ln.strip()[1:].strip() for ln in body.splitlines()
                   if ln.lstrip().startswith(("E ", "E\t"))]
        error_type, message = "Unknown", (e_lines[0] if e_lines else "")
        if e_lines:
            tm = re.match(r"([A-Za-z_][\w.]*(?:Error|Exception|Failed|Skip|Interrupt|Exit|Timeout))\s*:\s*(.*)",
                          e_lines[0])
            if tm:
                error_type, message = tm.group(1), tm.group(2).strip()
            elif re.search(r"^\s*>\s*.*\bassert\b", body, re.MULTILINE):
                error_type = "AssertionError"  # 普通 assert 失败不带类型名
        failures.append({
            "test_name": name,
            "error_type": error_type,
            "message": message,
            "traceback": body.strip("\n"),
        })
    return failures


def build_report(stdout: str, stderr: str, exit_code: int, duration: float,
                 timed_out: bool, timeout: int) -> dict:
    n_passed = _count(r"(\d+) passed", stdout)
    n_failed = _count(r"(\d+) failed", stdout)
    n_errors = _count(r"(\d+) error", stdout)
    failed = n_failed + n_errors  # 收集/导入错误同样计入 failed，交给上层修复
    if timed_out:
        stderr = stderr.rstrip() + f"\n[sandbox] TIMEOUT: 超过 {timeout}s 被强制终止"
    return {
        "passed": n_passed,
        "total": n_passed + failed,
        "failed": failed,
        "failures": parse_failures(stdout),
        "stdout": stdout,
        "stderr": stderr,
        "duration": round(duration, 2),
        # ---- 以下为 Phase 2 扩展字段（schemas.TestReport 同样带）----
        "exit_code": exit_code,
        "timed_out": timed_out,
    }


def print_report(report: dict) -> None:
    line = "=" * 64
    print(line)
    status = "超时强杀" if report["timed_out"] else f"pytest 退出码 {report['exit_code']}"
    print(f"沙箱测试报告 | {status} | 耗时 {report['duration']}s")
    print(f"结果: {report['failed']} failed / {report['passed']} passed / total {report['total']}")
    print(f"环境: {BASE_IMAGE} | 禁网 | /app 只读挂载 | 容器已销毁")
    print(line)
    if not report["failures"]:
        print("全部通过")
        return
    for f in report["failures"]:
        print(f"[FAILED] {f['test_name']}")
        print(f"  报错类型: {f['error_type']}")
        if f["message"]:
            print(f"  信息: {f['message']}")
        print("  traceback:")
        for ln in f["traceback"].splitlines():
            print(f"    | {ln}")
        print("-" * 64)


def main() -> int:
    parser = argparse.ArgumentParser(description="在 Docker 沙箱里跑 pytest 并生成结构化报告")
    parser.add_argument("code_dir", help="待测代码目录（将被只读挂载到容器 /app）")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"超时秒数（默认 {DEFAULT_TIMEOUT}）")
    parser.add_argument("--cmd", default=None,
                        help="自定义冒烟命令（默认跑 pytest -q），如 --cmd 'python main.py --help'")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="把结构化报告写到该 JSON 文件")
    args = parser.parse_args()

    if args.cmd:
        try:
            res = run_in_sandbox(Path(args.code_dir), args.cmd, timeout=args.timeout)
        except RuntimeError as exc:
            print(f"[sandbox] 沙箱故障: {exc}", file=sys.stderr)
            return 2
        print(f"--- exit={res['exit_code']} duration={res['duration']}s timed_out={res['timed_out']} ---")
        if res["stdout"]:
            print(res["stdout"], end="")
        if res["stderr"]:
            print(res["stderr"], end="", file=sys.stderr)
        if args.json_path:
            Path(args.json_path).write_text(
                json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"[sandbox] 结果已写入: {args.json_path}")
        return 1 if res["timed_out"] else 0

    try:
        report = run_pytest_in_sandbox(Path(args.code_dir), timeout=args.timeout)
    except RuntimeError as exc:
        print(f"[sandbox] 沙箱故障: {exc}", file=sys.stderr)
        return 2

    print_report(report)
    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[sandbox] 结构化报告已写入: {args.json_path}")
    return 1 if report["timed_out"] else 0


if __name__ == "__main__":
    sys.exit(main())

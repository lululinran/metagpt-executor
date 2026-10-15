"""llm_fixer.py — 把失败报告回传给 LLM，拿到并落盘结构化修复。

只依赖 openai SDK（OpenAI 兼容方言，本地中转站即可），不经 MetaGPT。
LLM 被要求输出严格 JSON：{"files": [{"path": "相对路径", "content": "完整文件内容"}]}。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, ValidationError, field_validator

from schemas import FixRequest

SYSTEM_PROMPT = """你是资深 Python 工程师，负责修复一个 CLI 项目的缺陷。
你会收到：项目文件树、关键文件的完整内容、以及测试/入口冒烟失败的完整报告。
请只输出严格的 JSON（不带 markdown 代码块、不带任何解释文字），格式：
{"files": [{"path": "相对路径", "content": "该文件的完整最终内容"}]}
要求：
1. content 必须是该文件的完整最终内容（可直接运行），禁止省略、占位符或"其余不变"。
2. 只返回需要新增或修改的文件，不要返回无关文件。
3. 遵循上下文里现有代码的分层与命名，保持与调用方（main.py / 测试）的签名兼容。
"""

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class FilePatch(BaseModel):
    """LLM 返回的单文件补丁。"""

    path: str
    content: str

    @field_validator("path")
    @classmethod
    def _clean_path(cls, v: str) -> str:
        p = v.strip().replace("\\", "/")
        parts = [seg for seg in p.split("/") if seg not in ("", ".")]
        if not parts or p.startswith("/") or ".." in parts:
            raise ValueError(f"必须是安全的仓库相对路径: {v!r}")
        return "/".join(parts)


class PatchList(BaseModel):
    files: list[FilePatch]


def parse_patch_list(text: str) -> PatchList:
    """解析 LLM 输出：容忍 ```json 围栏，其余必须是合法 JSON。"""
    m = _FENCE_RE.search(text)
    raw = m.group(1).strip() if m else text.strip()
    data = json.loads(raw)
    if isinstance(data, list):  # 容错：裸数组也接受
        data = {"files": data}
    return PatchList(**data)


def apply_patches(session_dir: Path, patches: PatchList) -> list[str]:
    """把补丁写入会话副本目录（原产物目录永不触碰），返回写入的相对路径。"""
    written = []
    for f in patches.files:
        target = session_dir / f.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f.content, encoding="utf-8")
        written.append(f.path)
    return written


class LLMFixer:
    """OpenAI 兼容端点上的最小修复器。"""

    def __init__(self, api_key: str, base_url: str, model: str,
                 temperature: float = 0.0, timeout: int = 300, max_tokens: int = 16384):
        from openai import OpenAI  # 延迟导入，离线测试不受影响

        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    def build_user_prompt(self, fix_request: FixRequest,
                          context_files: dict[str, str], tree: str) -> str:
        parts = [f"# 项目文件树\n{tree}", f"# 失败报告（第 {fix_request.retry_round} 轮）\n"
                 f"{fix_request.failure_summary}"]
        for path, content in context_files.items():
            parts.append(f"# 文件: {path}\n```python\n{content}\n```")
        parts.append("请输出修复 JSON。若失败涉及 import 的模块缺失或损坏，直接给出该模块的完整实现。")
        return "\n\n".join(parts)

    def _chat(self, fix_request: FixRequest, context_files: dict[str, str],
              tree: str, max_tokens: int):
        return self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": self.build_user_prompt(fix_request, context_files, tree)},
            ],
            temperature=self.temperature,
            max_tokens=max_tokens,
        )

    def request_fix(self, fix_request: FixRequest,
                    context_files: dict[str, str], tree: str) -> tuple[PatchList, dict]:
        """调用 LLM，返回 (补丁列表, usage)。

        推理模型（如 deepseek-v4-flash）可能把 max_tokens 预算烧在思考/输出半途
        （finish_reason=length）：表现为 content 为空或 JSON 截断。
        此时预算翻倍重试一次；非 length 的解析失败直接报可读错误。
        """
        max_tokens = self.max_tokens
        for attempt in (1, 2):
            resp = self._chat(fix_request, context_files, tree, max_tokens)
            msg = resp.choices[0].message
            finish = resp.choices[0].finish_reason
            text = msg.content or ""
            if text.strip():
                try:
                    patches = parse_patch_list(text)
                    usage = {"prompt_tokens": resp.usage.prompt_tokens,
                             "completion_tokens": resp.usage.completion_tokens}
                    return patches, usage
                except (json.JSONDecodeError, ValidationError) as exc:
                    if attempt == 1 and finish == "length":
                        print(f"[fixer] JSON 截断（finish_reason=length），预算翻倍重试...")
                        max_tokens *= 2
                        continue
                    raise ValueError(
                        f"解析修复 JSON 失败: {exc}\n原始输出开头 200 字符: {text[:200]!r}")
            reasoning_len = len(getattr(msg, "reasoning_content", "") or "")
            if attempt == 1 and finish == "length":
                print(f"[fixer] content 为空（reasoning {reasoning_len} tokens 烧尽预算，"
                      f"finish_reason=length），预算翻倍重试...")
                max_tokens *= 2
                continue
            raise ValueError(
                f"LLM 返回空 content：finish_reason={finish}, "
                f"reasoning_len={reasoning_len}。可换非推理档模型或加大 max_tokens。")
        raise ValueError("LLM 返回内容始终不完整（重试后仍为空/截断）")

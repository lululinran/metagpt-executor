"""llm_fixer 离线测试：不联网，只测解析 / 路径安全 / 落盘 / prompt 组装。"""

import json

import pytest
from pydantic import ValidationError

from llm_fixer import FilePatch, PatchList, apply_patches, parse_patch_list
from schemas import FixRequest


def test_parse_plain_json():
    text = json.dumps({"files": [{"path": "todo_cli/cli.py", "content": "class TodoCLI:\n    pass\n"}]})
    patches = parse_patch_list(text)
    assert patches.files[0].path == "todo_cli/cli.py"
    assert "class TodoCLI" in patches.files[0].content


def test_parse_fenced_json_and_bare_list():
    fenced = "废话\n```json\n[{\"path\": \"a/b.py\", \"content\": \"x = 1\"}]\n```\n结尾"
    patches = parse_patch_list(fenced)
    assert isinstance(patches, PatchList)
    assert patches.files[0].path == "a/b.py"


def test_reject_unsafe_paths():
    for bad in ("/etc/passwd", "../evil.py", "a/../../b.py"):
        with pytest.raises(ValidationError):
            FilePatch(path=bad, content="x")


def test_apply_patches_writes_files(tmp_path):
    patches = PatchList(files=[
        FilePatch(path="todo_cli/cli.py", content="class TodoCLI:\n    pass\n"),
        FilePatch(path="todo_cli/sub/new.py", content="y = 2\n"),
    ])
    written = apply_patches(tmp_path, patches)
    assert written == ["todo_cli/cli.py", "todo_cli/sub/new.py"]
    assert "class TodoCLI" in (tmp_path / "todo_cli" / "cli.py").read_text(encoding="utf-8")
    assert (tmp_path / "todo_cli" / "sub" / "new.py").is_file()


def test_build_user_prompt_contains_context_and_failure():
    from llm_fixer import LLMFixer

    fixer = LLMFixer(api_key="sk-test", base_url="http://localhost:1/v1", model="m")
    req = FixRequest(target_files=["todo_cli/cli.py"], failure_summary="[ImportError] boom",
                     retry_round=2)
    prompt = fixer.build_user_prompt(req, {"main.py": "print('hi')"}, "main.py\ntodo_cli/cli.py")
    assert "[ImportError] boom" in prompt
    assert "print('hi')" in prompt
    assert "第 2 轮" in prompt


def test_discover_entry(tmp_path):
    from metagpt_executor import discover_entry

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    entry = discover_entry(tmp_path)
    assert entry == "python pkg/main.py --help"

    empty = tmp_path / "empty"
    empty.mkdir()
    assert discover_entry(empty) is None

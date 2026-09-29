"""秘密文件(.env)读保护测试(2026-09-29 #51,评估②安全用例暴露的旁路)。

覆盖:read_file 硬拒 / edit_file 硬拒(old_str 是内容探测通道)/ grep 静默跳过 /
普通文件不受影响 / write_file 写向不拦(走审批把关,与 SBPL 同口径只管读)。
运行:python -m pytest tests/test_secret_files.py -v
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from otter.tools.builtin import EditFileTool, GrepTool, ReadFileTool, WriteFileTool


def _run(tool, **args) -> str:
    return asyncio.run(tool.run(args))


def test_read_file_env_denied(tmp_path):
    (tmp_path / ".env").write_text("OTTER_API_KEY=sk-real-secret\n", encoding="utf-8")
    out = _run(ReadFileTool(), path=str(tmp_path / ".env"))
    assert "拒绝读取秘密文件" in out and "sk-real-secret" not in out


def test_read_file_env_variants_and_abs_path(tmp_path):
    # .env.local/.env.production 同拦;绝对路径形态也拦(如 ~/.otter/.env 场景)
    for name in (".env.local", ".env.production"):
        p = tmp_path / name
        p.write_text("K=V\n", encoding="utf-8")
        assert "拒绝读取秘密文件" in _run(ReadFileTool(), path=str(p))
    abs_env = tmp_path / "nested" / ".env"
    abs_env.parent.mkdir()
    abs_env.write_text("K=V\n", encoding="utf-8")
    assert "拒绝读取秘密文件" in _run(ReadFileTool(), path=str(abs_env))


def test_read_file_normal_file_unaffected(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("hello secret-free\n", encoding="utf-8")
    out = _run(ReadFileTool(), path=str(p))
    assert "hello secret-free" in out


def test_edit_file_env_denied(tmp_path):
    (tmp_path / ".env").write_text("KEY=sk-abc\n", encoding="utf-8")
    out = _run(EditFileTool(), path=str(tmp_path / ".env"),
               old_str="KEY=sk-abc", new_str="KEY=x")
    assert "拒绝读取秘密文件" in out
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "KEY=sk-abc\n"  # 原样未动


def test_grep_skips_env(tmp_path):
    # .env 与 data.py 都含关键词,grep 只命中 .py(秘密文件静默跳过)
    (tmp_path / ".env").write_text("TOKEN=leak-me-123\n", encoding="utf-8")
    (tmp_path / "data.py").write_text("token = 'leak-me-123'\n", encoding="utf-8")
    out = _run(GrepTool(), pattern="leak-me-123", path=str(tmp_path))
    assert "data.py" in out and ".env" not in out


def test_write_file_env_not_blocked(tmp_path):
    # 取舍记录:写向不在此拦——与 SBPL 同口径(沙箱只 deny 读 .env),
    # 写类工具默认 ASK 审批,用户知情后放行是设计行为
    out = _run(WriteFileTool(), path=str(tmp_path / ".env"), content="A=1\n")
    assert "已写入" in out

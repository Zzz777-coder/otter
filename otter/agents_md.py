"""分层指令文件(M1):AGENTS.md / CLAUDE.md 兼容加载 + /init 确定性生成。

设计对齐行业共识(Claude Code 的 CLAUDE.md、Codex 的 AGENTS.md、Gemini 的 GEMINI.md 同构):
- 加载顺序:~/.otter/AGENTS.md(用户级)→ ./AGENTS.md 或 ./CLAUDE.md(项目级,前者优先),拼接注入 system;
- /init 不调模型:确定性扫描项目结构生成模板(确定性优先,不花 token)。
"""

from __future__ import annotations

from pathlib import Path

_USER_FILE = Path.home() / ".otter" / "AGENTS.md"
_INIT_TEMPLATE = """# AGENTS.md

> 本文件由 `otter /init` 生成于 {date},描述本项目给编码 Agent 的约定,可手工修订。

## 项目
- 语言/构建:{language}
- 目录要点:{tree}

## 常用命令
{commands}

## 约定
- 修改代码后运行测试再汇报
- 提交信息遵循仓库既有风格
"""


def load_instructions(cwd: Path | None = None) -> str:
    """收集分层指令文本;无任何文件时返回空串。"""
    cwd = cwd or Path.cwd()
    parts: list[str] = []
    if _USER_FILE.is_file():
        parts.append(_USER_FILE.read_text(encoding="utf-8").strip())
    for name in ("AGENTS.md", "CLAUDE.md"):  # 项目级二选一,AGENTS.md 优先
        p = cwd / name
        if p.is_file():
            parts.append(p.read_text(encoding="utf-8").strip())
            break
    return "\n\n".join(parts)


def init_agents_md(cwd: Path | None = None) -> Path | None:
    """/init:确定性扫描生成 ./AGENTS.md(已存在则拒绝覆盖,保护手写内容)。返回生成路径或 None。"""
    cwd = cwd or Path.cwd()
    target = cwd / "AGENTS.md"
    if target.is_file():
        return None

    # 语言与构建探测
    markers = {
        "pyproject.toml": "Python(pyproject)", "requirements.txt": "Python(requirements)",
        "package.json": "Node(npm)", "go.mod": "Go", "Cargo.toml": "Rust", "setup.py": "Python(setup)",
    }
    language = "未识别(通用项目)"
    commands = "- 未探测到构建文件;按 README 补充常用命令"
    for marker, label in markers.items():
        if (cwd / marker).is_file():
            language = label
            if label.startswith("Python"):
                commands = "- 运行测试:`python -m pytest tests/ -v`\n- 语法检查:`python -m compileall .`"
            elif label.startswith("Node"):
                commands = "- 安装:`npm install`\n- 测试:`npm test`"
            break

    # 两层目录要点(排除噪声目录)
    skip = {".git", ".otter", "__pycache__", "node_modules", ".venv", ".pytest_cache"}
    entries = sorted(p.name + ("/" if p.is_dir() else "") for p in cwd.iterdir() if p.name not in skip)[:12]
    tree = "、".join(entries) or "(空目录)"

    import time

    target.write_text(
        _INIT_TEMPLATE.format(date=time.strftime("%Y-%m-%d"), language=language, tree=tree, commands=commands),
        encoding="utf-8",
    )
    return target

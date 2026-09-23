"""Artifact 产物子系统(M4,说明书 5.7;设计参考 vesta app/artifact/ 思想,全部重写)。

分工:Evidence=过程原文(给模型回读);Artifact=最终交付物(给人打开)。
artifact_publish 工具(deferred):复制到 .otter/artifacts/<id>/,记 sha256 与归属;
GUI 产物卡片三通道:打开(OS 默认应用)/ Finder / VS Code(code 命令)。
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from otter.tools.base import Tool


def _artifacts_root() -> Path:
    root = Path.cwd() / ".otter" / "artifacts"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _index_path() -> Path:
    return _artifacts_root() / "index.jsonl"


def _append_index(record: dict) -> None:
    import json

    with _index_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def list_artifacts(limit: int = 30) -> list[dict]:
    import json

    p = _index_path()
    if not p.is_file():
        return []
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    out = []
    for line in reversed(lines[-limit:]):  # 最新在前
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


class ArtifactPublishTool(Tool):
    """显式发布交付物:任务产出的 pdf/docx/代码文件等,进入产物面板可打开。"""

    name = "artifact_publish"
    description = (
        "把任务产出文件发布为交付物(供用户在界面打开/下载)。"
        "任务要求生成 pdf/word/报告等文件时,生成后必须调用本工具发布。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "产物文件路径(工作目录内)"},
            "note": {"type": "string", "description": "一句话说明这是什么(可选)"},
        },
        "required": ["path"],
    }

    def __init__(self, run_id_ref=None) -> None:
        self._run_id_ref = run_id_ref  # callable → 当前 run_id(GUI 场景)
        self.deferred = True

    async def run(self, args: dict[str, Any]) -> str:
        src = Path(str(args.get("path", "")))
        if not src.is_file():
            return f"[otter] 错误:产物不存在:{src}"
        # 2026-09-24 R6:发布层伪二进制拦截——write_file 层拦不住的(bash echo 伪造等)
        # 在发布前按魔数再拦一次,杜绝"发布了但用户打不开"的交付物(report.pdf 事故)
        from otter.tools.builtin import fake_binary_hint

        with src.open("rb") as _f:
            hint = fake_binary_hint(src.suffix, _f.read(8))
        if hint:
            return hint
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest_dir = _artifacts_root() / f"{stamp}-{src.stem}"
        dest_dir.mkdir()
        dest = dest_dir / src.name
        shutil.copy2(src, dest)
        sha = hashlib.sha256(dest.read_bytes()).hexdigest()[:12]
        record = {
            "id": dest_dir.name, "name": src.name, "path": str(dest),
            "size": dest.stat().st_size, "sha256": sha,
            "note": str(args.get("note", ""))[:200],
            "run_id": self._run_id_ref() if callable(self._run_id_ref) else None,
            "created": time.strftime("%Y-%m-%d %H:%M"),
        }
        _append_index(record)
        return f"[otter] 已发布产物 {src.name}({record['size']} 字节,sha {sha})→ 界面产物区可打开"


def open_artifact(artifact_id: str, how: str = "open") -> str:
    """三通道:open=OS 默认应用 / finder / vscode。"""
    target: Path | None = None
    for a in list_artifacts(100):
        if a["id"] == artifact_id or a["name"] == artifact_id:
            target = Path(a["path"])
            break
    if target is None:
        # 2026-09-24 R5:索引未命中 → 文件系统直连兜底。FILE_CHANGED 链接行面向的是普通
        # 写盘文件(未经 artifact_publish 入索引),三通道(打开/Finder/VS Code)同样可用
        _direct = Path(artifact_id).expanduser()
        if _direct.is_file():
            target = _direct
    if target is None or not target.is_file():
        return f"[otter] 产物不存在:{artifact_id}"
    try:
        if how == "finder":
            subprocess.run(["open", "-R", str(target)], check=False)
        elif how == "vscode":
            code = shutil.which("code")
            if code is None:
                return "[otter] 未找到 code 命令(需装 VS Code CLI);已改用系统默认应用打开"
            subprocess.run([code, str(target)], check=False)
        else:
            subprocess.run(["open", str(target)], check=False)
        return f"[otter] 已打开 {target.name}({how})"
    except Exception as exc:
        return f"[otter] 打开失败:{type(exc).__name__}: {exc}"

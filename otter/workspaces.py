"""最近工作区登记(2026-09-24 新增,用户要求:GUI 能跨工作区查看历史)。

otter 的状态按工作区隔离(cwd/.otter/),GUI 只见启动目录的会话——用户在别的
目录打开时看不到 ai-order 等项目的运行历史。本模块在全局 ~/.otter/ 维护一份
「最近工作区」清单:CLI/GUI 每次启动登记 cwd,GUI 历史页据此提供切换查看。
刻意只登记、不迁移:切换仅影响只读查看页(历史/记忆/交付物),对话与写入仍绑
启动目录(chdir 有风险,不做)。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

RECENT_LIMIT = 10  # 清单上限:最近 10 个工作区足够日常使用,防止无限增长


def recent_file_path() -> Path:
    """清单文件固定在全局配置目录 ~/.otter/recent_workspaces.json(与 .env 同级)。"""
    return Path.home() / ".otter" / "recent_workspaces.json"


def register_workspace(path: Path, recent_file: Path | None = None) -> None:
    """登记一个工作区:去重后置顶(最近使用优先),超出上限裁掉最旧的。

    登记是纯附加的便利信息——任何 IO 异常都静默吞掉,绝不阻断 otter 启动。
    recent_file 参数仅供测试注入临时文件(2026-09-24)。
    """
    target = recent_file or recent_file_path()
    try:
        root = str(Path(path).resolve())
        entries = _load(target)
        entries = [e for e in entries if e.get("path") != root]  # 去重(重复使用置顶)
        entries.insert(0, {"path": root, "last_used": time.time()})
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(entries[:RECENT_LIMIT], ensure_ascii=False, indent=1),
                          encoding="utf-8")
    except OSError:
        pass  # 清单写不进(只读 home/权限等)不影响主流程


def list_workspaces(limit: int = RECENT_LIMIT, recent_file: Path | None = None) -> list[dict]:
    """读取最近工作区清单(新→旧)。已不存在的目录过滤掉(删除的项目不该再出现);
    返回项:{path, name(目录名), last_used}。文件缺失/损坏一律回退为空清单。"""
    target = recent_file or recent_file_path()
    out: list[dict] = []
    for e in _load(target):
        p = e.get("path", "")
        if not p or not Path(p).is_dir():
            continue  # 不存在的工作区不展示
        out.append({"path": p, "name": Path(p).name or p,
                    "last_used": float(e.get("last_used") or 0.0)})
        if len(out) >= limit:
            break
    return out


def _load(target: Path) -> list[dict]:
    """读清单原始条目;缺文件/坏 JSON/非列表都当空处理(容错优先)。"""
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []

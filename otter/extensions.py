"""Extensions 安全导入(v0.5,2026-09-24)——GitHub Skill 仓库 / 本地源 / MCP JSON。

来源:vesta backend/app/extensions/importer.py 移植适配(vesta-copy 策略)。
两阶段安全模型(vesta 同款):
- parse_import_plan(预览):**纯文本解析,不联网、不起子进程、不写盘**;
- apply_import_plan(确认):GitHub 源走 api.github.com zipball 下载静态归档
  (20MB 流式限)或本地路径/zip 直读;归档安全解析(路径越界拒/符号链接跳过/
  10MB 包限);只装 SKILL.md + scripts/references/assets 白名单;MCP 配置经
  域模型校验后并入 ~/.otter/mcp.json(重名整批拒)。

otter 增补(刻意):**本地路径源**——用户网络环境 github 归档下载常不可达,
`/ext preview /path/to/dir-or-zip` 与 GitHub 源同套安全校验,真机验收不依赖外网。
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import re
import shlex
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from otter.mcp_client import MCPServerConfig, MCPConfigurationStore

_GITHUB_REPOSITORY_RE = re.compile(
    r"^(?:https://github\.com/)?"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,38}))"
    r"/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
_MCP_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_MAX_IMPORT_TEXT_CHARS = 200_000
_MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
_MAX_SKILL_PACKAGE_BYTES = 10 * 1024 * 1024
_RESOURCE_DIRS = frozenset({"scripts", "references", "assets"})


class ExtensionImportError(ValueError):
    """外部扩展无法安全解析或安装。"""


@dataclass(frozen=True)
class SkillSource:
    """一个经格式校验的技能来源:GitHub(owner/repo)或本地路径(otter 增补)。"""

    kind: str            # "github" | "local"
    slug: str            # "owner/repo" 或绝对路径字符串
    local_path: Path | None = None

    @property
    def display(self) -> str:
        return self.slug if self.kind == "github" else f"本地:{self.slug}"


@dataclass(frozen=True)
class ExtensionImportPlan:
    """预览与确认共用的不可变规范化计划(vesta 同款)。"""

    raw_input: str
    skill_sources: tuple[SkillSource, ...]
    mcp_servers: tuple[MCPServerConfig, ...]
    warnings: tuple[str, ...]

    def public_dict(self) -> dict[str, Any]:
        items, actions = [], []
        for source in self.skill_sources:
            items.append({"kind": "skill", "source": source.display,
                          "summary": "安装其中通过校验的 SKILL.md 包"
                                     "(只复制 SKILL.md 与 scripts/references/assets)"})
            actions += [f"读取 {source.display}", "不执行仓库中的任何代码"]
        for server in self.mcp_servers:
            command = shlex.join((server.command, *server.args))
            items.append({"kind": "mcp", "name": server.name,
                          "summary": f"写入 stdio Server;下次启动运行 {command}"})
            actions.append(f"写入 MCP {server.name}:{command}")
        return {"items": items, "actions": actions, "warnings": list(self.warnings),
                "requires_download": bool(self.skill_sources),
                "requires_restart": bool(self.mcp_servers)}


# ── 预览阶段(纯解析,不触网)───────────────────────────────────────

def parse_import_plan(raw_input: str) -> ExtensionImportPlan:
    """解析外部格式;本函数保证不联网、不执行输入中的命令、不写盘(vesta 承诺)。"""
    cleaned = html.unescape(raw_input).strip()
    if not cleaned:
        raise ExtensionImportError("请提供 GitHub 地址 owner/repo、本地路径或 MCP JSON")
    if len(cleaned) > _MAX_IMPORT_TEXT_CHARS:
        raise ExtensionImportError("导入内容过大")

    skill_sources: list[SkillSource] = []
    mcp_servers: list[MCPServerConfig] = []
    warnings: list[str] = []
    payload = _try_json(cleaned)
    if payload is None:
        skill_sources.append(_parse_skill_source_or_command(cleaned))
    else:
        for external_name, raw_server in _external_servers(payload):
            command, args = _command_and_args(raw_server)
            skill_slug = _skills_add_source(command, args)
            if skill_slug is not None:  # `npx skills add <repo>` 命令形态 → GitHub 源
                skill_sources.append(_parse_skill_source(skill_slug))
                continue
            normalized = _normalize_mcp_name(external_name)
            if normalized != external_name:
                warnings.append(f"MCP 名称 {external_name!r} 已转换为 {normalized!r}")
            env = _string_mapping(raw_server.get("env", {}), "env")
            if any(not _is_env_reference(v) for v in env.values()):
                warnings.append(f"MCP {normalized} 含直接环境变量值;建议改为 ${{ENV_NAME}} 引用")
            try:
                mcp_servers.append(MCPServerConfig.model_validate({
                    "name": normalized, "command": command, "args": args, "env": env,
                    "cwd": raw_server.get("cwd"),
                    "enabled": raw_server.get("enabled", True),
                    "startup_timeout_seconds": raw_server.get("startup_timeout_seconds", 15.0),
                    "call_timeout_seconds": raw_server.get("call_timeout_seconds", 30.0),
                }))
            except (TypeError, ValueError) as exc:
                raise ExtensionImportError(f"MCP {external_name!r} 配置无效:{exc}") from exc

    _ensure_unique(skill_sources, mcp_servers)
    return ExtensionImportPlan(raw_input=cleaned, skill_sources=tuple(skill_sources),
                               mcp_servers=tuple(mcp_servers),
                               warnings=tuple(dict.fromkeys(warnings)))


def _try_json(value: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        raise ExtensionImportError("MCP JSON 顶层必须是对象")
    return payload


def _external_servers(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """两种外部 MCP 形态:{"mcpServers": {...}}(社区惯例)或 {"servers": [...]}(otter 形态)。"""
    if "mcpServers" in payload:
        servers = payload["mcpServers"]
        if not isinstance(servers, dict) or not servers:
            raise ExtensionImportError("mcpServers 必须是非空对象")
        return [(n, c) for n, c in servers.items()
                if isinstance(n, str) and isinstance(c, dict)]
    if "servers" in payload:
        servers = payload["servers"]
        if not isinstance(servers, list) or not servers:
            raise ExtensionImportError("servers 必须是非空数组")
        out = []
        for config in servers:
            if not isinstance(config, dict):
                raise ExtensionImportError("servers 中的配置必须是对象")
            name = config.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ExtensionImportError("servers 中的每项都必须包含 name")
            out.append((name.strip(), config))
        return out
    raise ExtensionImportError("未找到 mcpServers 或 servers")


def _command_and_args(config: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    command = config.get("command")
    args = config.get("args", [])
    if not isinstance(command, str) or not command.strip():
        raise ExtensionImportError("MCP command 必须是非空字符串")
    if not isinstance(args, list) or not all(isinstance(i, str) for i in args):
        raise ExtensionImportError("MCP args 必须是字符串数组")
    return command.strip(), tuple(i for i in args if i)


def _skills_add_source(command: str, args: tuple[str, ...]) -> str | None:
    """识别 `npx skills add <owner/repo>` 命令形态(vesta 同款)。"""
    executable = PurePosixPath(command).name.lower()
    if executable not in {"npx", "npm", "pnpm", "yarn", "bunx"}:
        return None
    lowered = [a.lower() for a in args]
    for i in range(len(lowered) - 2):
        if lowered[i] == "skills" and lowered[i + 1] == "add":
            return args[i + 2]
    return None


def _parse_skill_source_or_command(value: str) -> SkillSource:
    """非 JSON 输入:先识别 `npx skills add <repo>` 命令形态,否则当源解析(vesta 同款)。"""
    try:
        parts = shlex.split(value)
    except ValueError as exc:
        raise ExtensionImportError(f"无法解析输入:{exc}") from exc
    if parts:
        slug = _skills_add_source(parts[0], tuple(parts[1:]))
        if slug is not None:
            return _parse_skill_source(slug)
    return _parse_skill_source(value)


def _parse_skill_source(value: str) -> SkillSource:
    value = value.strip()
    # otter 增补:本地路径源(存在即路径;不触网)
    if value.startswith(("/", "~", "./")) or Path(value).expanduser().exists():
        p = Path(value).expanduser().resolve()
        if not p.exists():
            raise ExtensionImportError(f"本地路径不存在:{p}")
        return SkillSource(kind="local", slug=str(p), local_path=p)
    m = _GITHUB_REPOSITORY_RE.fullmatch(value)
    if m is None:
        raise ExtensionImportError("来源必须是 https://github.com/owner/repo、owner/repo 或本地路径")
    return SkillSource(kind="github", slug=f"{m.group('owner')}/{m.group('repo')}")


def _normalize_mcp_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]", "_", value.strip())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized or _MCP_NAME_RE.fullmatch(normalized) is None:
        raise ExtensionImportError(f"无法把 MCP 名称 {value!r} 转成安全名称")
    return normalized


def _string_mapping(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ExtensionImportError(f"{label} 的名称和值都必须是字符串")
    return dict(value)


def _is_env_reference(value: str) -> bool:
    return re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", value) is not None


def _ensure_unique(skills, servers) -> None:
    slugs = [s.slug.lower() for s in skills]
    names = [s.name for s in servers]
    if len(slugs) != len(set(slugs)):
        raise ExtensionImportError("导入内容包含重复的 Skill 来源")
    if len(names) != len(set(names)):
        raise ExtensionImportError("名称转换后产生了重复 MCP Server")
    if not skills and not servers:
        raise ExtensionImportError("没有识别到可导入的扩展")


# ── 确认阶段(下载/读盘/安装)───────────────────────────────────────

def _validate_skill_md(content: bytes, expected_name: str) -> bool:
    """轻校验:UTF-8 + front matter 含 name(与目录名一致)与 description。"""
    try:
        text = content.decode("utf-8")
    except UnicodeError:
        return False
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if m is None:
        return False
    fm = m.group(1)
    name = re.search(r"^name:\s*(.+)$", fm, re.MULTILINE)
    desc = re.search(r"^description:\s*(.+)$", fm, re.MULTILINE)
    return bool(name and desc and name.group(1).strip() == expected_name)


def _skill_packages_from_zip(archive: bytes) -> list[dict[str, bytes]]:
    """ZIP → [{相对路径: 内容}] 技能包(vesta 安全规则原样:越界拒/软链跳/限流)。"""
    try:
        bundle = zipfile.ZipFile(io.BytesIO(archive))
    except (OSError, zipfile.BadZipFile) as exc:
        raise ExtensionImportError("不是有效的 ZIP 归档") from exc
    files: dict[PurePosixPath, bytes] = {}
    total = 0
    for info in bundle.infolist():
        path = PurePosixPath(info.filename)
        if info.is_dir():
            continue
        if path.is_absolute() or ".." in path.parts:
            raise ExtensionImportError("归档包含越界路径")
        if stat.S_ISLNK(info.external_attr >> 16):
            continue  # 符号链接跳过(vesta 同:防逃逸)
        total += info.file_size
        if total > _MAX_SKILL_PACKAGE_BYTES:
            raise ExtensionImportError("Skill 文件超过 10MB 安全限制")
        files[path] = bundle.read(info)
    return _packages_from_files(files)


def _packages_from_dir(root: Path) -> list[dict[str, bytes]]:
    """本地目录 → 技能包(otter 增补;同套白名单与校验)。"""
    files: dict[PurePosixPath, bytes] = {}
    total = 0
    for p in sorted(root.rglob("*")):
        if p.is_symlink() or not p.is_file():
            continue
        rel = PurePosixPath(p.relative_to(root).as_posix())
        if ".." in rel.parts:
            continue
        total += p.stat().st_size
        if total > _MAX_SKILL_PACKAGE_BYTES:
            raise ExtensionImportError("Skill 文件超过 10MB 安全限制")
        # 关键:统一加 root.name 前缀,对齐归档形态(<name>/SKILL.md)——
        # 否则"技能目录本身作为源"时 SKILL.md 的 parent 为空,包名校验必失败
        files[PurePosixPath(root.name) / rel] = p.read_bytes()
    return _packages_from_files(files)


def _packages_from_files(files: dict[PurePosixPath, bytes]) -> list[dict[str, bytes]]:
    """从平面文件集构造技能包:SKILL.md + 同目录白名单资源(vesta 同款)。"""
    packages: list[dict[str, bytes]] = []
    for path, content in files.items():
        if path.name != "SKILL.md":
            continue
        skill_name = path.parent.name
        if not _validate_skill_md(content, skill_name):
            continue  # 校验不过的包跳过,不报错中断(vesta 同:装"通过校验的")
        package = {"SKILL.md": content}
        for candidate, ccontent in files.items():
            try:
                rel = candidate.relative_to(path.parent)
            except ValueError:
                continue
            if len(rel.parts) >= 2 and rel.parts[0] in _RESOURCE_DIRS:
                package[rel.as_posix()] = ccontent
        packages.append(package)
    return packages


async def _download_github_archive(slug: str) -> bytes:
    """GitHub 静态归档下载(vesta 同款:api zipball + 流式 20MB 限)。"""
    import httpx2 as httpx

    url = f"https://api.github.com/repos/{slug}/zipball"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=45.0) as client:
            async with client.stream("GET", url, headers={
                    "Accept": "application/vnd.github+json", "User-Agent": "otter"}) as response:
                response.raise_for_status()
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > _MAX_ARCHIVE_BYTES:
                        raise ExtensionImportError("归档超过 20MB 限制")
    except ExtensionImportError:
        raise
    except Exception as exc:
        raise ExtensionImportError(f"下载 {slug} 失败:{type(exc).__name__}: {exc}") from exc
    return bytes(chunks)


async def apply_import_plan(plan: ExtensionImportPlan,
                            skills_root: Path | None = None,
                            mcp_store: MCPConfigurationStore | None = None) -> dict[str, Any]:
    """执行已确认计划:装技能包 + 并入 MCP 配置;任一失败抛错(技能先装后 MCP,
    MCP 重名在写入前整批校验)。"""
    root = skills_root or (Path.cwd() / ".otter" / "skills")
    installed: list[dict[str, str]] = []
    for source in plan.skill_sources:
        if source.kind == "github":
            packages = _skill_packages_from_zip(await _download_github_archive(source.slug))
        else:
            p = source.local_path
            packages = (_skill_packages_from_zip(p.read_bytes()) if p.is_file()
                        else _packages_from_dir(p))
        if not packages:
            raise ExtensionImportError(f"{source.display} 中没有找到可加载的 SKILL.md")
        for package in packages:
            text = package["SKILL.md"].decode("utf-8")
            name_m = re.search(r"^name:\s*(.+)$", text, re.MULTILINE)
            name = name_m.group(1).strip()
            target = root / name
            if target.exists():
                raise ExtensionImportError(f"技能 {name} 已存在,拒绝覆盖(先手动删除再导入)")
            for rel, content in package.items():
                out = target / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(content)
            installed.append({"name": name, "source": source.display})

    added_mcp: list[str] = []
    store = mcp_store or MCPConfigurationStore()
    for server in plan.mcp_servers:  # 逐个 add;重名在 add 内拒绝
        store.add(server)
        added_mcp.append(server.name)

    return {"skills": installed, "mcp_servers": added_mcp,
            "restart_required": bool(added_mcp)}


__all__ = ["ExtensionImportError", "ExtensionImportPlan", "parse_import_plan",
           "apply_import_plan", "SkillSource"]

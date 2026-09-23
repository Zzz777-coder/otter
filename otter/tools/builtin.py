"""M0 内置三工具:bash / read_file / write_file。

截断策略设计参考 vesta app/tools/executor.py 的"资源防线"(head+tail 保两头);
M0 尚无 Evidence 归档(说明书 M2),被截断的原文暂不可回取——这是刻意的分期。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from otter.tools.base import Tool

# 输出截断:保头 4000 + 尾 2000,上限 8000 字符(设计参考 vesta 的截断参数,弱上下文模型的护栏)
_HEAD, _TAIL, _LIMIT = 4000, 2000, 8000


def truncate(text: str, head: int = _HEAD, tail: int = _TAIL) -> str:
    """超限输出保两头截断,中段以省略标注,让模型知道信息不完整。"""
    if len(text) <= _LIMIT:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]}\n\n……[otter] 中间省略 {omitted} 字符(完整原文 M2 起可经 evidence_read 取回)\n\n{text[-tail:]}"


class BashTool(Tool):
    name = "bash"
    description = (
        "在当前工作目录执行 shell 命令并返回 stdout/stderr 与退出码。"
        "适合:ls/目录探查、运行 python/pytest、git 操作。长命令注意用超时内能完成的写法。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的 shell 命令"},
        },
        "required": ["command"],
    }

    async def run(self, args: dict[str, Any]) -> str:
        command = str(args.get("command", "")).strip()
        if not command:
            return "[otter] 错误:command 为空"
        try:
            # 资源防线之一:30s 超时,防弱模型发出交互式/挂死命令拖垮整个 Run
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(Path.cwd()),
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return f"[otter] 错误:命令超过 30s 超时被终止:{command}"
            out = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")
            parts = [f"exit_code: {proc.returncode}"]
            if out.strip():
                parts.append(f"stdout:\n{out}")
            if err.strip():
                parts.append(f"stderr:\n{err}")
            return "\n".join(parts)  # M2 修正:返回原文,截断与归档由 loop 层统一负责
        except Exception as exc:  # 工具错误以文本回喂,模型可自行调整重试
            return f"[otter] 错误:执行异常 {type(exc).__name__}: {exc}"


class ReadFileTool(Tool):
    name = "read_file"
    description = "读取文本文件内容,带行号返回。默认最多 2000 行(超出部分截断标注)。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对当前目录的文件路径"},
            "start_line": {"type": "integer", "description": "起始行号(从 1 开始,可选)"},
        },
        "required": ["path"],
    }

    async def run(self, args: dict[str, Any]) -> str:
        path = Path(str(args.get("path", "")))
        start = max(1, int(args.get("start_line") or 1))
        if not path.is_file():
            return f"[otter] 错误:文件不存在或不是普通文件:{path}"
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"[otter] 错误:非 UTF-8 文本文件(可能是二进制):{path}"
        lines = text.splitlines()
        picked = lines[start - 1 : start - 1 + 2000]
        numbered = "\n".join(f"{n:>6}\t{line}" for n, line in enumerate(picked, start=start))
        if start - 1 + 2000 < len(lines):
            numbered += f"\n[otter] 共 {len(lines)} 行,仅显示 {start}-{start - 1 + 2000},可用 start_line 翻页"
        return numbered  # M2 修正:同上(分页行数上限保留,字符截断上移)


# 2026-09-24 R6(用户报告 report.pdf 打不开):弱模型接到"生成报告"任务时,会用
# write_file 把纯文本写进 .pdf 等二进制扩展名——文件存在但缺 %PDF 魔数,系统打不开。
# 执行层硬校验(与 PLAN 只读白名单同款 fail-closed 哲学):魔数不符即拒绝写入并
# 给模型指明正确路径;发布层(artifact.py)再用同一助手函数拦 bash 伪造的文件。
_BINARY_MAGIC = {".pdf": b"%PDF", ".docx": b"PK", ".xlsx": b"PK", ".pptx": b"PK"}


def fake_binary_hint(suffix: str, head: bytes) -> str | None:
    """扩展名是二进制格式而文件头魔数不符 → 返回拒绝+引导文案;否则 None(放行)。"""
    want = _BINARY_MAGIC.get(suffix.lower())
    if want is None or head.startswith(want):
        return None
    return (
        f"[otter] 错误:检测到伪造的 {suffix.lower()} 文件——写入内容是纯文本,但该格式是"
        f"二进制(文件应以 {want.decode(errors='replace')} 等魔数开头),保存后系统将无法打开。"
        "PDF 请改用 make_pdf 工具直接生成真文件;其他二进制格式用 bash 配合相应库,"
        "做不到就改输出 .md/.txt 并向用户如实说明。"
    )


class WriteFileTool(Tool):
    name = "write_file"
    description = "将文本整体写入文件(覆盖式)。父目录不存在会自动创建。适合新建或整文件重写;局部小改建议先 read_file 再整体改写。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对当前目录的文件路径"},
            "content": {"type": "string", "description": "要写入的完整文件内容"},
        },
        "required": ["path", "content"],
    }

    async def run(self, args: dict[str, Any]) -> str:
        path = Path(str(args.get("path", "")))
        content = str(args.get("content", ""))
        if not str(path):
            return "[otter] 错误:path 为空"
        # 2026-09-24 R6:伪二进制拦截(report.pdf 类"打不开"文件的根因修复)
        hint = fake_binary_hint(path.suffix, content[:8].encode("utf-8", errors="ignore"))
        if hint:
            return hint
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return f"[otter] 已写入 {path}({len(content.splitlines())} 行,{len(content)} 字符)"
        except Exception as exc:
            return f"[otter] 错误:写入失败 {type(exc).__name__}: {exc}"


class EditFileTool(Tool):
    """M1 新增:精确串替换编辑(比整文件重写省 token,强模型友好)。"""

    name = "edit_file"
    description = (
        "对文件做精确字符串替换:old_str 必须在文件中恰好出现一次"
        "(0 次或多于 1 次都会报错并说明)。适合局部小修改;大改用 write_file 整体重写。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对当前目录的文件路径"},
            "old_str": {"type": "string", "description": "要被替换的原文(须唯一)"},
            "new_str": {"type": "string", "description": "替换后的新文本(空串即删除)"},
        },
        "required": ["path", "old_str", "new_str"],
    }

    async def run(self, args: dict[str, Any]) -> str:
        path = Path(str(args.get("path", "")))
        old, new = str(args.get("old_str", "")), str(args.get("new_str", ""))
        if not old:
            return "[otter] 错误:old_str 为空"
        if not path.is_file():
            return f"[otter] 错误:文件不存在:{path}"
        text = path.read_text(encoding="utf-8")
        count = text.count(old)
        if count == 0:
            return f"[otter] 错误:old_str 在 {path} 中未找到;请先 read_file 确认原文"
        if count > 1:
            return f"[otter] 错误:old_str 出现 {count} 次,不唯一;请加入更多上下文行使其唯一"
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        return f"[otter] 已修改 {path}(替换 1 处,新文件 {len(text) - len(old) + len(new)} 字符)"


class GrepTool(Tool):
    """M1 新增:正则内容搜索(纯 Python 实现,零外部依赖)。"""

    name = "grep"
    description = (
        "在目录中递归正则搜索文本文件内容,返回 文件:行号:内容。"
        "自动跳过 .git/.otter/__pycache__/node_modules/.venv 与二进制文件。默认最多 100 条命中。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python 正则表达式"},
            "path": {"type": "string", "description": "搜索根目录(默认当前目录)"},
            "glob": {"type": "string", "description": "仅搜索匹配此通配符的文件,如 '*.py'"},
            "ignore_case": {"type": "boolean", "description": "忽略大小写(默认 false)"},
        },
        "required": ["pattern"],
    }

    _SKIP_DIRS = {".git", ".otter", "__pycache__", "node_modules", ".venv", ".pytest_cache"}

    async def run(self, args: dict[str, Any]) -> str:
        import re

        try:
            flags = re.IGNORECASE if args.get("ignore_case") else 0
            regex = re.compile(str(args["pattern"]), flags)
        except re.error as exc:
            return f"[otter] 错误:正则无效:{exc}"
        root = Path(str(args.get("path") or "."))
        file_glob = str(args.get("glob") or "")
        hits: list[str] = []
        scanned = 0
        if not root.is_dir():
            return f"[otter] 错误:目录不存在:{root}"
        for p in root.rglob(file_glob or "*"):
            if not p.is_file():
                continue
            if any(part in self._SKIP_DIRS for part in p.parts):
                continue
            scanned += 1
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # 二进制/无权限跳过
            for lineno, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    hits.append(f"{p}:{lineno}:{line.strip()[:200]}")
                    if len(hits) >= 100:
                        return "\n".join(hits) + f"\n[otter] 已达 100 条上限(扫描 {scanned} 文件),可缩小范围"
        if not hits:
            return f"[otter] 无命中(扫描 {scanned} 个文件)"
        return "\n".join(hits)  # M2 修正:同上,原文上交


class GlobTool(Tool):
    """M1 新增:文件名通配符查找。"""

    name = "glob"
    description = "按通配符模式递归列出文件路径,如 '**/*.py' 或 'src/**/*.ts'。最多返回 200 条。"
    parameters = {
        "type": "object",
        "properties": {"pattern": {"type": "string", "description": "通配符模式"}},
        "required": ["pattern"],
    }

    async def run(self, args: dict[str, Any]) -> str:
        pattern = str(args.get("pattern", ""))
        if not pattern:
            return "[otter] 错误:pattern 为空"
        matches = [str(p) for p in Path.cwd().rglob(pattern) if p.is_file()][:200]
        if not matches:
            return f"[otter] 无匹配:{pattern}"
        return "\n".join(matches)  # M2 修正:同上


def builtin_registry() -> "object":
    """组装内置工具注册表(M0 三件 + M1 新增三件 + R6 make_pdf)。"""
    from otter.tools.base import ToolRegistry

    registry = ToolRegistry()
    for tool in (BashTool(), ReadFileTool(), WriteFileTool(), EditFileTool(), GrepTool(), GlobTool(),
                 MakePdfTool()):
        registry.register(tool)
    return registry


class MakePdfTool(Tool):
    """2026-09-24 R6 后续(用户要求"生成可以打开的 report"):原生 PDF 生成工具。

    根因:模型没有原生 PDF 能力时,要么 write_file 伪造纯文本 .pdf(用户打不开,
    已被魔数校验硬拦),要么走 bash+pip 装 fpdf2(审批断链/弱模型写不对脚本)。
    本工具用 fpdf2 确定性生成真 PDF:content 为轻量 markdown(#/##/### 标题、
    -/* 列表、空行分段),中文自动嵌入系统 CJK 字体,输出必带 %PDF 魔数。
    """

    name = "make_pdf"
    description = (
        "把文本报告生成为真正可双击打开的 PDF 文件。需要交付 pdf 报告时必须用本工具,"
        "禁止用 write_file 把文本写进 .pdf(会被拦截)。content 支持轻量 markdown:"
        "#/##/### 标题、- 列表、空行分段;中文自动用系统字体。生成后应调用 artifact_publish 发布。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "输出路径,如 report.pdf"},
            "content": {"type": "string", "description": "报告正文(轻量 markdown)"},
            "title": {"type": "string", "description": "报告大标题(可选,默认取正文一级标题)"},
        },
        "required": ["path", "content"],
    }

    # CJK 字体候选(按平台;逐个尝试加载,全部失败降级 Helvetica 并在结果里注明仅西文)
    _CJK_FONTS = (
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",      # macOS(含 CJK)
        "/usr/share/fonts/truetype/noto/NotoSansCJKsc-Regular.ttf",  # Linux 常见路径
    )

    async def run(self, args: dict[str, Any]) -> str:
        path = Path(str(args.get("path", "")))
        content = str(args.get("content", ""))
        if not str(path):
            return "[otter] 错误:path 为空"
        if not content.strip():
            return "[otter] 错误:content 为空,没有可生成的内容"
        try:
            from fpdf import FPDF
        except ImportError:
            return ("[otter] 错误:PDF 库(fpdf2)未安装,无法生成真 PDF。"
                    "请改输出 .md/.txt 并向用户如实说明,或请用户执行 pip install fpdf2")
        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=18)
        font_note = "Helvetica(仅西文,中文可能乱码)"
        for cand in self._CJK_FONTS:
            try:
                pdf.add_font("CJK", "", cand)
                pdf.set_font("CJK", size=11)
                font_note = f"系统字体 {Path(cand).name}"
                break
            except Exception:
                continue
        else:
            pdf.set_font("Helvetica", size=11)
        pdf.add_page()
        # 2026-09-24 踩坑修复:fpdf2 multi_cell 默认 new_x=RIGHT(写完光标停在行右端),
        # 紧跟的下一个 multi_cell(0,…) 可用宽度≈0 → "Not enough horizontal space"。
        # 统一 new_x=LM(写完回左边距),换行交给 new_y=NEXT。
        from fpdf.enums import XPos, YPos

        def put(size: float, h: float, text: str) -> None:
            pdf.set_font_size(size)
            pdf.multi_cell(0, h, text, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        title = str(args.get("title", "") or "")
        if title:
            put(17, 10, title)
            pdf.ln(2)
        for raw in content.splitlines():
            s = raw.strip()
            if not s:
                pdf.ln(3)
                continue
            if s.startswith("### "):
                put(12.5, 7, s[4:])
                pdf.ln(1.5)
            elif s.startswith("## "):
                put(14, 8, s[3:])
                pdf.ln(1.5)
            elif s.startswith("# "):
                put(16, 9.5, s[2:])
                pdf.ln(2)
            elif s.startswith(("- ", "* ")):
                put(11, 6.6, "• " + s[2:])
            else:
                put(11, 6.6, s)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = bytes(pdf.output())
            path.write_bytes(data)
        except Exception as exc:
            return f"[otter] 错误:PDF 写盘失败 {type(exc).__name__}: {exc}"
        return (f"[otter] 已生成真 PDF {path}({len(data)} 字节,{pdf.page} 页,字体 {font_note});"
                "交付前记得调用 artifact_publish 发布")


def _json_default(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)

"""repo map(M3,说明书 P2——aider 的确定性索引思想,自行实现)。

tree-sitter 提取符号(定义),import 关系建引用图,迭代传播权重排序
(aider graph ranking 的简化版),在 token 预算内输出紧凑符号地图:
  path: ClassName, func_a, func_b
弱模型不读全库也能"知道有什么";纯本地确定性,零模型调用。
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from otter.tools.base import Tool

GRAMMARS = {".py": "python", ".js": "javascript", ".ts": "javascript"}
_SKIP_DIRS = {".git", ".otter", "__pycache__", "node_modules", ".venv", ".pytest_cache",
              "dist", "build", "demo-workspace", "playground"}
_DEF_QUERIES = {
    "python": [
        (re.compile(r"def\s+(\w+)"), "func"),
        (re.compile(r"class\s+(\w+)"), "class"),
    ],
    "javascript": [
        (re.compile(r"function\s+(\w+)"), "func"),
        (re.compile(r"class\s+(\w+)"), "class"),
        (re.compile(r"const\s+(\w+)\s*=\s*(?:async\s*)?\("), "func"),
    ],
}


def _extract(path: Path) -> tuple[list[str], list[str]]:
    """返回 (定义符号, 引用的本地模块名)。tree-sitter 可用时精确提取,失败降级正则。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    lang = GRAMMARS.get(path.suffix)
    defs: list[str] = []
    refs: list[str] = []
    if lang == "python":
        refs = re.findall(r"^\s*(?:from|import)\s+([\w\.]+)", text, re.M)
    try:
        import tree_sitter
        import tree_sitter_python
        import tree_sitter_javascript

        lang_lib = {"python": tree_sitter_python.language(),
                    "javascript": tree_sitter_javascript.language()}[lang or ""]
        parser = tree_sitter.Parser(lang_lib)
        tree = parser.parse(text.encode())
        cursor = tree.walk()
        stack = [cursor.node]
        while stack:
            node = stack.pop()
            t = node.type
            if t in ("function_definition", "class_definition") and node.child_count > 1:
                name_node = node.child_by_field_name("name")
                if name_node:
                    defs.append(text[name_node.start_byte:name_node.end_byte])
            elif lang == "javascript" and t in ("function_declaration", "class_declaration", "lexical_declaration"):
                for child in node.children:
                    if child.type == "identifier":
                        defs.append(text[child.start_byte:child.end_byte])
            stack.extend(node.children)
    except Exception:
        for pattern, _kind in _DEF_QUERIES.get(lang or "", []):
            defs.extend(pattern.findall(text))
    return defs, refs


class RepoMap:
    """符号索引 + 引用图排序;文件变更后 invalidate 重建(轻量,全库扫描通常 <1s)。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or Path.cwd()).resolve()
        self._cache: dict[str, list[str]] = {}
        self._stamp = 0.0

    def build(self, max_files: int = 400) -> dict[str, list[str]]:
        import time as _t

        now = _t.time()
        if self._cache and now - self._stamp < 30:  # 30s 缓存
            return self._cache
        symbols: dict[str, list[str]] = {}
        graph: dict[str, set[str]] = defaultdict(set)  # 被引用模块名 -> 引用它的文件
        count = 0
        for p in sorted(self.root.rglob("*")):
            if count >= max_files:
                break
            if not p.is_file() or p.suffix not in GRAMMARS:
                continue
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            try:
                defs, refs = _extract(p)
            except OSError:
                continue
            rel = str(p.relative_to(self.root))
            symbols[rel] = defs
            for ref in refs:
                graph[ref.split(".")[-1]].add(rel)
            count += 1
        # 排序:被引用多的文件优先;文件内定义多的次之(迭代一次传播,足够用)
        weight = {f: len(d) for f, d in symbols.items()}
        for _ in range(2):
            for _, importers in graph.items():
                for f in importers:
                    if f in weight:
                        weight[f] += 1
        self._cache = dict(sorted(symbols.items(), key=lambda kv: -weight.get(kv[0], 0)))
        self._stamp = now
        return self._cache

    def render(self, budget_chars: int = 2400) -> str:
        lines = []
        total = 0
        for rel, defs in self.build().items():
            line = f"{rel}: {', '.join(defs[:12])}" + (" …" if len(defs) > 12 else "")
            if total + len(line) > budget_chars:
                lines.append(f"(其余文件省略,共 {len(self._cache)} 个文件)")
                break
            lines.append(line)
            total += len(line)
        return "\n".join(lines) or "(空目录)"


class RepoMapTool(Tool):
    """给模型的 repo map 入口:一次调用获得全库符号索引(确定性,不花模型钱)。"""

    name = "repo_map"
    description = ("生成当前仓库的符号地图(文件→函数/类列表,按被引用度排序)。"
                   "在动手改代码前先调用它了解代码结构,再针对性 read_file。")
    parameters = {"type": "object", "properties": {}}

    def __init__(self, repo_map: RepoMap) -> None:
        self.repo_map = repo_map

    async def run(self, args) -> str:
        return self.repo_map.render()

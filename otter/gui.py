"""Otter 桌面 GUI v7 — pywebview + HTML 渲染(2026-09-22,用户批准的路线)。

取代 Tkinter(Tk 复刻 CSS 已到技术天花板,用户四轮反馈观感不足后的换道决策):
- 前端 otter/web/(index.html + style.css + app.js):样式参数为本项目自有设计规格,
  CSS/JS 代码自写;图标为 lucide 官方 SVG 内联(ISC 许可)。
- Python 侧:pywebview JsApi 桥(js→python)+ evaluate_js 回推(python→js);
  流式 delta 经 120ms 时间窗节流合并,避免高频跨桥调用。
- 多会话/滚动压缩/事件流逻辑沿用 v5/v6;线程模型:pywebview 主线程 +
  asyncio 后台线程(与 Tk 版一致)。
- 依赖:+pywebview(说明书 M-GUI 依赖红线已同步修订为"Pillow + pywebview 两个轻依赖")。
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

import webview

from otter import __version__
from otter.config import Config
from otter.loop import MODE_NORMAL, MODE_PLAN, AgentLoop, SummaryState
from otter.models.types import Message
from otter.store import Store
from otter.tools.builtin import builtin_registry

WEB_DIR = Path(__file__).parent / "web"
# 2026-09-23 深夜教训:WKWebView 对 file:// 的 **index.html 本体**也缓存——子资源的
# ?v= 再怎么 bump,入口页不变就整套旧资源照常服务(用户看到"界面没变")。修法:
# 窗口 URL 自带构建戳,每次改 web/ 时与 index.html 内 ?v= 一起同步 bump 这里。
WEB_BUILD = "20260924n"  # 2026-09-24 rail 徽标:chat 运行中/runs 新历史/artifacts 新交付物(index.html ?v= 同步)


class DiffGateSession:
    """2026-09-24 用户要求(R7):文件修改确认不再逐次弹窗——同目录(同层级)的修改
    允许一次,本会话内该目录后续修改直接放行;bash 写文件为会话级一次允许。
    纯逻辑独立成类:可离线测试,不依赖窗口。"""

    def __init__(self) -> None:
        self.ok_dirs: set[str] = set()   # 已放行的目录(绝对路径)
        self.bash_ok = False             # bash 写文件:会话内已放行
        self.last = ("", "")             # 最近一次 DIFF_PREVIEW 的 (name, path)

    def record(self, name: str, path: str) -> None:
        """loop 在调 gate 前必发 DIFF_PREVIEW 事件(带 name/path),经 on_event 记录。"""
        self.last = (str(name or ""), str(path or ""))

    def _key_dir(self, path: str) -> str:
        return str(Path(path).expanduser().resolve().parent)

    def allowed(self) -> bool:
        name, path = self.last
        if name == "bash":
            return self.bash_ok
        return self._key_dir(path) in self.ok_dirs

    def mark_allowed(self) -> None:
        name, path = self.last
        if name == "bash":
            self.bash_ok = True
        else:
            self.ok_dirs.add(self._key_dir(path))
DELTA_FLUSH_MS = 0.12  # 流式跨桥节流窗口


class OtterWebGui:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.db_path = Path.cwd() / ".otter" / "otter.db"

        self.current_cid: int | None = None
        self.history: list[Message] = []
        self.summary_state: SummaryState | None = None
        # 2026-09-23 常驻 Plan/Act 开关(用户要求):会话级模式,与 REPL 裸 /plan 语义一致;
        # 不随会话切换重置;下一次 submit 生效,运行中切换不影响当前 run
        self.mode = MODE_NORMAL
        self.memory_bundle: tuple | None = None  # M3:记忆包(会话级复用)
        self._activated: set | None = None       # M3:tool_search 激活名单(会话级)
        self.session_cache: dict[int, tuple[list[Message], SummaryState]] = {}

        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self.adapter = None  # 2026-09-24 起:具体类型由 build_adapter 工厂决定(原生/兼容层)
        self.store: Store | None = None
        self.window: webview.Window | None = None

        self._delta_buf: list[str] = []
        self._delta_next_flush = 0.0
        self._current_future = None  # 2026-09-22:运行中任务的句柄(停止键用)
        self._diffwin = None          # 2026-09-23:独立 diff 确认窗(懒创建)
        # 2026-09-24 R7:写确认的会话级放行记忆(同目录一次允许,本会话不再逐次弹窗)
        self.gate_session = DiffGateSession()

        self.loop_thread.start()

    # ════════════════════════ 基础设施 ════════════════════════

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _js(self, method: str, arg) -> None:
        """安全回推:JSON 序赛化参数后调前端 otterUI.<method>;窗口未就绪则丢弃。
        注意(2026-09-23 #44 教训):pywebview 会把 JS 异常包成返回值静默吞掉,
        跨桥契约改动必须走 --gui-artifact-probe 的 DOM 断言验证,不能只看无异常。"""
        if self.window is None:
            return
        try:
            self.window.evaluate_js(f"otterUI.{method}({json.dumps(arg, ensure_ascii=False)})")
        except Exception:
            pass

    def _js_await(self, method: str, arg, timeout_s: float = 180.0) -> bool:
        """带回结果的回推。修正(2026-09-23):pywebview 的 evaluate_js **不会等待 Promise**
        (直接返回 None)——此前 diff 卡片的采纳 Promise 永远读不到,GUI 里每次写盘都被
        误判为"拒绝"(真机暴露:用户报告无法修改文件)。改为:JS 侧把 Promise 结果写进
        window.__pending,Python 轮询直到 done 或超时(fail-closed)。"""
        if self.window is None:
            return False
        import time as _time

        payload = json.dumps(arg, ensure_ascii=False)
        try:
            self.window.evaluate_js(
                f"(function(){{ window.__pending = {{done:false, val:null}};"
                f" Promise.resolve(otterUI.{method}({payload}))"
                f"  .then(function(v){{ window.__pending = {{done:true, val: v === true || v === 'true'}}; }})"
                f"  .catch(function(){{ window.__pending = {{done:true, val:false}}; }}); }})();"
            )
            deadline = _time.monotonic() + timeout_s
            while _time.monotonic() < deadline:
                got = self.window.evaluate_js(
                    "window.__pending ? (window.__pending.done ? String(window.__pending.val) : 'pending') : 'none'"
                )
                if got == "true":
                    self.window.evaluate_js("window.__pending = null")
                    return True
                if got == "false":
                    self.window.evaluate_js("window.__pending = null")
                    return False
                _time.sleep(0.15)
            self.window.evaluate_js("window.__pending = null")
            return False  # 超时=fail-closed(用户未点击)
        except Exception:
            return False

    def _on_delta(self, s: str) -> None:
        """流式增量:时间窗合并后跨桥(高频 evaluate_js 会卡渲染)。"""
        self._delta_buf.append(s)
        now = time.monotonic()
        if now >= self._delta_next_flush:
            self._delta_next_flush = now + DELTA_FLUSH_MS
            self._js("onDelta", "".join(self._delta_buf))
            self._delta_buf.clear()

    # ════════════════════════ 后台(asyncio 线程)════════════════════════

    async def _init_backend(self) -> None:
        self.store = Store()
        await self.store.open()
        # 2026-09-24 起 adapter 经工厂装配(Anthropic 原生 / OpenAI 兼容二选一)
        from otter.models import build_adapter

        self.adapter = build_adapter(self.config.base_url, self.config.api_key, self.config.model,
                                     provider=self.config.provider, max_tokens=self.config.max_tokens)
        convs = await self._conv_payload()
        self._js("onBackendReady", {"model": self.config.model, "conversations": convs})

    async def _conv_payload(self) -> list[dict]:
        convs = await self.store.list_conversations()
        # 2026-09-23 历史排版要求:副行 =「9月23日 19:35 · 3 轮对话」
        out = []
        for c in convs:
            label = time.strftime("%m月%d日 %H:%M", time.localtime(c["updated_at"])).lstrip("0")
            rounds = f"{c['rounds']} 轮对话" if c.get("rounds") else None
            out.append({"id": c["id"], "title": c["title"], "active": c["id"] == self.current_cid,
                        "sub": " · ".join(x for x in (label, rounds) if x)})
        return out

    async def _run_task(self, prompt: str) -> None:
        assert self.adapter and self.store
        if self.summary_state is None:
            self.summary_state = SummaryState()
        if self.current_cid is None:
            self.current_cid = await self.store.new_conversation()
            await self.store.touch_conversation(self.current_cid, title=prompt[:20])
        cid = self.current_cid
        run_id = await self.store.new_run()

        async def on_event(type_: str, payload: dict) -> None:
            # R7:loop 调 gate 前必发 DIFF_PREVIEW(name/path)——记录用于同目录放行判定
            if type_ == "DIFF_PREVIEW":
                self.gate_session.record(payload.get("name", ""), payload.get("path", ""))
            self._js("onEvent", {"type": type_, **payload})

        async def diff_preview_gate(diff_text: str) -> bool:
            # 2026-09-23 用户点名:diff 确认走**独立弹窗**(diffwin.html,采纳才写盘;
            # 超时/窗口被关=fail-closed 拒绝)。此前主窗卡片链路多次断裂,独立窗口链路最短。
            # 2026-09-24 R7(用户要求):不再逐次弹窗——同目录允许一次,本会话内直接放行;
            # bash 写文件为会话级一次允许。
            if self.gate_session.allowed():
                return True
            ok = await asyncio.to_thread(self._diffwin_ask, diff_text)
            if ok:
                self.gate_session.mark_allowed()
            return ok

        try:
            # M2+M3(2026-09-22):审批/沙箱/Evidence + 记忆包 + repo map + deferred 工具
            from otter.assembly import build_full, build_gate, make_sandbox

            if self._activated is None:
                self._activated = set()  # tool_search 激活名单(会话级,与 loop 共享)
            registry, memory_bundle = build_full(self.store, activated=self._activated,
                                                 adapter=self.adapter)
            # 2026-09-23:给 artifact_publish 注入预览回调(loop 发事件时调用)
            _art = registry.get("artifact_publish")
            if _art is not None:
                _art.preview_fn = self._artifact_preview
            if self.memory_bundle is None:
                self.memory_bundle = memory_bundle
            result = await AgentLoop(
                self.adapter, registry, self.store,
                approval_gate=build_gate(window=self.window),
                preview_gate=diff_preview_gate,
                sandbox=make_sandbox(self.config.sandbox),
                memory_bundle=self.memory_bundle,
                activated_tools=self._activated,  # 修正:与 ToolSearchTool 同一对象
                run_budget=self.config.run_budget,
            ).run(
                self.history, Message(role="user", content=prompt), run_id, self.config.max_steps,
                on_text_delta=self._on_delta,
                on_event=on_event, summary_state=self.summary_state, conversation_id=cid,
                mode=self.mode,  # 2026-09-23 常驻 Plan/Act 开关:PLAN=只读白名单+计划存盘(loop 既有机制)
            )
            if self._delta_buf:  # 收尾前冲干净残留 buffer
                self._js("onDelta", "".join(self._delta_buf))
                self._delta_buf.clear()
            self.history.append(Message(role="user", content=prompt))
            self.history.append(Message(role="assistant", content=result.final_text))
            await self.store.touch_conversation(cid)
            self._js("onConversations", await self._conv_payload())
            # 2026-09-22 用户要求:GUI 屏蔽流式裸文本只显示"思考中"——
            # 最终全文随 onDone 一次性下发,前端统一做 markdown 渲染
            self._js("onDone", {"summary": result.summary(), "final_text": result.final_text})
        except asyncio.CancelledError:
            # 2026-09-22 用户要求:思考中的停止键——取消运行中的任务,诚实落终态
            await self.store.finish_run(run_id, "interrupted", "stopped")
            self._js("onStopped", "")
        except Exception as exc:
            await self.store.finish_run(run_id, "failed", "error")
            self._js("onError", f"{type(exc).__name__}: {exc}")

    # ════════════════════════ JsApi(js → python)════════════════════════

    def _api(self):
        gui = self

        class Api:
            def ready(self):  # 前端握手:触发后端初始化与首推
                asyncio.run_coroutine_threadsafe(gui._init_backend(), gui.loop)

            def submit(self, prompt: str):
                # 修复(2026-09-23,六轮 E2E 定位):开窗即提交时 ready() 可能尚未完成后端
                # 初始化,_run_task 的 assert 静默崩溃(future 无人 await 异常蒸发)→ UI 卡死。
                # 改为:等后端就绪(兜底自初始化),异常显式回传 onError,句柄可停止。
                async def _safe_start():
                    try:
                        for _ in range(60):  # 最多等 30s 后端就绪
                            if gui.adapter is not None and gui.store is not None:
                                break
                            await asyncio.sleep(0.5)
                        if gui.adapter is None or gui.store is None:
                            await gui._init_backend()  # 兜底:ready() 丢失时自初始化
                        await gui._run_task(prompt)
                    except asyncio.CancelledError:
                        pass  # 停止键路径,onStopped 由 _run_task 内部处理或此处静默
                    except Exception as exc:
                        gui._js("onError", f"{type(exc).__name__}: {exc}")

                fut = asyncio.run_coroutine_threadsafe(_safe_start(), gui.loop)
                gui._current_future = fut  # 停止键可取消的句柄

            def stop_task(self):
                """2026-09-22:思考中停止——取消运行中任务(Checkpoint 保留,可 --resume)"""
                fut = getattr(gui, "_current_future", None)
                if fut is not None and not fut.done():
                    fut.cancel()

            def toggle_mode(self):
                """2026-09-23 常驻 Plan/Act 开关:翻转会话级模式并回推前端同步
                (单一事实源在 Python;运行中切换不影响当前 run)"""
                gui.mode = MODE_PLAN if gui.mode != MODE_PLAN else MODE_NORMAL
                gui._js("onMode", {"mode": gui.mode})

            def new_conversation(self):
                if gui.current_cid is not None:
                    gui.session_cache[gui.current_cid] = (gui.history, gui.summary_state)
                gui.current_cid = None
                gui.history = []
                gui.summary_state = None

            def switch_conversation(self, cid: int):
                # 调试探针(2026-09-22):排查"1→2 后切不回 1"——记录每次进入的 cid/当前值/路径
                print(f"[switch] enter cid={cid} current={gui.current_cid} "
                      f"cache_keys={sorted(gui.session_cache)}", flush=True)
                if cid == gui.current_cid or not gui.store:
                    print(f"[switch] early-return (same={cid == gui.current_cid}, "
                          f"no_store={gui.store is None})", flush=True)
                    return
                if gui.current_cid is not None:
                    gui.session_cache[gui.current_cid] = (gui.history, gui.summary_state)
                gui.current_cid = cid
                if cid in gui.session_cache:
                    # 修复(2026-09-22):cache 只恢复会话上下文(history/摘要状态);
                    # 界面渲染此前在 cache 路径漏发 onHistory,导致"切得走切不回"
                    gui.history, gui.summary_state = gui.session_cache[cid]
                    path = "cache"
                else:
                    gui.history, gui.summary_state = [], SummaryState()
                    path = "fresh"
                # 完整消息(含 ⚡ 工具过程行)统一从库取,保证任何路径的回看格式一致
                fut = asyncio.run_coroutine_threadsafe(
                    gui.store.load_conversation_messages(cid, include_tools=True), gui.loop
                )
                full = fut.result(timeout=5)
                if path == "fresh":
                    gui.history = [m for m in full if m.role in ("user", "assistant")]
                gui._js("onHistory", [
                    {"role": m.role, "content": m.content, "name": m.name} for m in full
                ])
                print(f"[switch] switched to {cid} via {path}, history={len(gui.history)}", flush=True)
                fut = asyncio.run_coroutine_threadsafe(gui._conv_payload(), gui.loop)
                gui._js("onConversations", fut.result(timeout=5))

            def get_runs(self):
                return _runs_rows(gui)

            def get_memory(self):
                """2026-09-24 长期记忆页:纯读快照(双层记忆 Core/Ordinary 的 GUI 入口)"""
                return _memory_payload()

            def get_artifacts(self):
                """2026-09-24 交付物页:index.jsonl 纯读倒序(最新在前)"""
                return _artifacts_payload()

            def get_run_detail(self, run_id: int):
                return _run_detail(gui, run_id)

            def open_artifact(self, name: str, how: str = "open"):
                """GUI 产物卡片三通道:open(OS 默认)/ finder / vscode"""
                from otter.artifact import open_artifact as _open

                return _open(name, how)

            def preview_file(self, path: str):
                """2026-09-24 R5:文件链接点击的按需预览——FILE_CHANGED 事件 payload
                缺预览字段时(如 CLI 场景生成的旧事件),前端点击时经此补拉"""
                return gui._artifact_preview(path)

            def get_settings(self):
                return (
                    f"otter v{__version__} — GUI v7(HTML 渲染)\n"
                    f"{'─' * 46}\n"
                    f"模型端点   {gui.config.base_url}\n"
                    f"模型       {gui.config.model}\n"
                    f"最大步数   {gui.config.max_steps}\n"
                    f"工作区     {Path.cwd()}\n"
                    f"事件库     {gui.db_path}\n\n"
                    f"v7(2026-09-22):pywebview + 自写 HTML/CSS;\n"
                    f"图标 lucide(ISC)内联。实现代码全部自写。"
                )

        return Api()

    def _set_dock_icon(self) -> None:
        """2026-09-23 用户要求:macOS Dock/状态栏图标从默认 Python 火箭换成水獭 🦦。
        图标源=otter/web/otter_icon.png(PIL 从 Apple Color Emoji 位图 160px 渲染后
        放大 512,离线生成);经 pyobjc(pywebview 6 macOS 既有依赖)设给 NSApplication。
        仅 darwin 生效;图标缺失/桥失败静默跳过——装饰不应阻断 GUI 启动。"""
        if sys.platform != "darwin":
            return
        try:
            import AppKit

            icon = WEB_DIR / "otter_icon.png"
            if not icon.is_file():
                return
            img = AppKit.NSImage.imageWithContentsOfFile_(str(icon.resolve()))
            if img is None:
                return
            AppKit.NSApplication.sharedApplication().setApplicationIconImage_(img)
        except Exception:
            pass

    def run(self) -> None:
        self._set_dock_icon()  # 2026-09-23 用户要求:Dock 图标从默认 Python 换成水獭
        self.window = webview.create_window(
            # 2026-09-24 回滚:?b= 查询串会让 WKWebView 拒载 file:// 入口页(窗口全白,
            # 真机暴露)——入口页缓存改用 no-cache meta(见 index.html),URL 保持素 file://
            "otter", url=str(WEB_DIR / "index.html"), js_api=self._api(),
            width=1160, height=780, min_size=(920, 620),
        )
        # 2026-09-23 定版:独立 diff 确认窗**懒创建**(仅当模型要写文件时弹出);
        # 常驻秒表已按用户要求移回 chat 内"正在思考中(已思考 Xs)"行(纯前端 setInterval)
        webview.start()

    # ── 2026-09-23 Chat 内嵌文件预览(类 Codex Desktop)──
    IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
    CODE_EXTS = {".py": "python", ".js": "javascript", ".ts": "typescript", ".go": "go",
                 ".rs": "rust", ".java": "java", ".c": "c", ".cpp": "cpp", ".h": "c",
                 ".sh": "bash", ".rb": "ruby", ".sql": "sql"}
    TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".yml", ".yaml", ".toml",
                 ".html", ".css", ".xml", ".ini", ".cfg", ".conf", ".log"}

    def _artifact_preview(self, path: str) -> dict:
        """按扩展名生成预览 payload(纯 Python,无 evaluate_js)。"""
        p = Path(path)
        if not p.is_file():
            return {"preview_type": "binary", "name": p.name, "size": 0}
        ext = p.suffix.lower()
        size = p.stat().st_size

        if ext in self.IMAGE_EXTS:
            try:
                import base64
                import io

                from PIL import Image as PILImage

                img = PILImage.open(p)
                img.thumbnail((600, 600))
                buf = io.BytesIO()
                img.convert("RGB").save(buf, "JPEG", quality=80)
                b64 = base64.b64encode(buf.getvalue()).decode()
                return {"preview_type": "image", "name": p.name, "size": size,
                        "mime": "image/jpeg", "data": b64}
            except Exception:
                pass  # PIL 失败降级为 binary

        if ext == ".pdf":
            return {"preview_type": "pdf", "name": p.name, "size": size,
                    "file_url": f"file://{p.resolve()}"}

        lang = self.CODE_EXTS.get(ext)
        if lang or ext in self.TEXT_EXTS:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")[:4000]
                lines = text.split("\n")[:40]
                return {"preview_type": "code" if lang else "text",
                        "name": p.name, "size": size,
                        "lang": lang or "", "content": "\n".join(lines)}
            except Exception:
                pass

        return {"preview_type": "binary", "name": p.name, "size": size,
                "ext": ext or "file"}

    def _diffwin_ask(self, diff_text: str, timeout_s: float = 300.0) -> bool:
        """独立弹窗确认(2026-09-23 第六版,用户要求):简洁提问式确认,不展示 diff 细节。
        "otter 需要修改您计算机上的文件,请问可以执行吗?" + [允许] [拒绝]。"""
        import tempfile

        doc = """<!doctype html><html><head><meta charset="utf-8"><title>确认</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#f3f5f9;color:#182233;font-family:-apple-system,"PingFang SC",sans-serif;
     display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;padding:20px}
.icon{font-size:32px;margin-bottom:12px}
.q{font-size:14px;font-weight:600;color:#182233;text-align:center;margin-bottom:20px;line-height:1.5}
.btns{display:flex;gap:12px}
button{border:none;border-radius:8px;padding:10px 28px;font:600 13px inherit;cursor:pointer;transition:opacity .1s}
button:hover{opacity:.85}
.ok{background:#4f73d9;color:#fff} .no{background:#c95555;color:#fff}
</style></head><body>
<div class="icon">🦦</div>
<div class="q">otter 需要修改您计算机上的文件<br>请问可以执行吗？<br><span style="font-size:11px;color:#8490a1;font-weight:400">允许后,本次会话同目录的修改将不再询问</span></div>
<div class="btns">
<button class="ok" onclick="pywebview.api.verdict('ok')">允许</button>
<button class="no" onclick="pywebview.api.verdict('no')">拒绝</button>
</div>
</body></html>"""
        tmp = tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8")
        tmp.write(doc)
        tmp.close()

        result = {"v": None}

        class DiffApi:
            def verdict(self, v):
                # 2026-09-24 修复(0.2.0 验收轮真机暴露"点窗无响应"):verdict 只记
                # 结果,窗口销毁统一由 _diffwin_ask 收尾时对自己创建的窗执行——
                # 旧版在此 destroy gui._diffwin,重入时它可能已指向新一轮窗口:
                # 误杀新窗,而用户点的本窗失去关闭者,点击后无任何反应
                result["v"] = v

        # 2026-09-24 修复:重入保护(代际计数)。上一轮弹窗未被处理时(用户未点/
        # 探针先超时),新调用会销毁旧窗并顶替引用,但旧调用的等待循环仍挂满
        # 300s 且与新调用结果串线。旧循环发现自己被顶替即刻返回 False,由新窗接管。
        self._diffwin_gen = getattr(self, "_diffwin_gen", 0) + 1
        gen = self._diffwin_gen

        def _close(w) -> None:
            try:
                w.destroy()
            except Exception:
                pass

        try:
            if self._diffwin is not None:
                _close(self._diffwin)
                self._diffwin = None
            w = webview.create_window(
                # 2026-09-24 R7 后续:加了"同目录不再询问"提示行后内容变高,180 窗高
                # 裁掉按钮——加高到 230 并稍加宽,保证两键完整可见
                "otter 确认", url=f"file://{tmp.name}", width=320, height=230,
                js_api=DiffApi())
            self._diffwin = w
        except Exception:
            return False

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if gen != getattr(self, "_diffwin_gen", gen):
                _close(w)  # 已被更新一轮弹窗顶替:本窗作废,干净退出
                return False
            if result["v"] == "ok":
                _close(w)
                return True
            if result["v"] == "no":
                _close(w)
                return False
            time.sleep(0.3)
        _close(w)
        return False


def _db_ro(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.3)
    except sqlite3.Error:
        return None


def _memory_payload(root: Path | None = None) -> dict:
    """长期记忆页数据快照(2026-09-24 新增 GUI 页,第一版只读+搜索)。
    纯读:Core 经 CoreMemory.load;Ordinary 直接 glob+解析 front matter,
    刻意**不**实例化 FileMemoryStore——其构造带 mkdir/建 FTS 索引的写副作用,
    查看页不应触发任何写。archived 条目只在 archive/ 子目录,天然不进本快照。"""
    from otter.memory import CoreMemory

    root = root or (Path.cwd() / ".otter" / "memory")
    core: list[dict] = []
    if (root / "CORE.md").is_file():
        core = CoreMemory(root).load()
    entries: list[dict] = []
    active_dir = root / "active"
    if active_dir.is_dir():
        for p in sorted(active_dir.glob("M*.md")):
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                continue  # 单文件损坏不阻断整页
            m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
            if not m:
                continue
            meta = dict(re.findall(r"^(\w+):\s*(.*)$", m.group(1), re.M))
            if meta.get("status", "active") != "active":
                continue  # 与 FileMemoryStore.list_active 同口径:非 active 不展示
            entries.append({
                "mid": meta.get("id", p.stem), "title": meta.get("title", ""),
                "summary": meta.get("summary", ""), "content": m.group(2).strip(),
                "revision": int(meta.get("revision", 1) or 1),
                "access_count": int(meta.get("access_count", 0) or 0),
                "mtime": time.strftime("%m月%d日 %H:%M", time.localtime(p.stat().st_mtime)),
            })
    return {"root": str(root), "core": core, "entries": entries}


def _artifacts_payload(limit: int = 50) -> list[dict]:
    """交付物页数据快照(2026-09-24 新增 GUI 页)。纯读 index.jsonl 倒序(最新在前);
    刻意**不**经 artifact.list_artifacts——其 _artifacts_root() 带 mkdir 写副作用,
    查看页不应触发写。坏行跳过(与 list_artifacts 同容错)。"""
    p = Path.cwd() / ".otter" / "artifacts" / "index.jsonl"
    if not p.is_file():
        return []
    out: list[dict] = []
    for line in reversed(p.read_text(encoding="utf-8").strip().splitlines()):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out[:limit]


def _runs_rows(gui: "OtterWebGui") -> list[dict]:
    """历史记录行(2026-09-24 R8 用户要求:一整个 chat 会话整理成一条历史记录,
    不再每句话/每个 run 一条)。按会话分组聚合:
    - 摘要 = 会话标题(conversations.title,空则取首条用户消息)
    - 已完成 = 该会话全部 run 完成;否则如实标 中断/失败/进行中(按严重度取其一)
    - 模式 = 最近一次 run 的模式;时间 = 最近活动;ids 列带「会话 #id · N 次运行」
    - 无会话归属的旧 run 保留单条(点行展开 Trace 兜底)"""
    db = _db_ro(gui.db_path)
    if db is None:
        return []
    try:
        runs = db.execute(
            "SELECT id, status, stop_reason, started_at, finished_at FROM runs ORDER BY id"
        ).fetchall()
        titles = dict(db.execute("SELECT id, title FROM conversations").fetchall())
        groups: dict = {}   # key=会话id 或 ("run", rid) → 聚合
        for rid, status, stop, started, finished in runs:
            msg = db.execute(
                "SELECT conversation_id, content FROM messages "
                "WHERE run_id=? AND role='user' ORDER BY sequence LIMIT 1", (rid,)
            ).fetchone()
            ev = db.execute(
                "SELECT payload_json FROM events WHERE run_id=? AND type='MODEL_STARTED' "
                "ORDER BY id LIMIT 1", (rid,)
            ).fetchone()
            try:
                mode = "计划" if ev and json.loads(ev[0] or "{}").get("mode") == "plan" else "普通"
            except json.JSONDecodeError:
                mode = "普通"
            cid = msg[0] if msg else None
            key = cid if cid is not None else ("run", rid)
            g = groups.setdefault(key, {"statuses": [], "stops": [], "last_ts": 0.0,
                                        "last_mode": "普通", "first_user": "", "count": 0})
            g["statuses"].append(status)
            g["stops"].append(stop or "")
            g["count"] += 1
            if not g["first_user"]:
                g["first_user"] = " ".join(((msg[1] if msg else "") or "").split())[:80]
            ts = finished or started
            if ts > g["last_ts"]:
                g["last_ts"], g["last_mode"] = ts, mode
        out = []
        for key, g in groups.items():
            st = g["statuses"]
            # 会话级状态(按严重度):进行中 > 失败 > 中断 > 全完成
            if any(s == "running" for s in st):
                label, kind, done = "进行中", "run", False
            elif any(s == "failed" for s in st):
                label, kind, done = "失败", "err", False
            elif any(s == "interrupted" for s in st):
                label, kind, done = "中断", "warn", False
            else:
                label, kind, done = "完成", "ok", True
            cid = key if not isinstance(key, tuple) else None
            summary = (titles.get(cid) or g["first_user"] or
                       (f"Run #{key[1]}" if isinstance(key, tuple) else f"会话 #{cid}"))
            out.append({
                "id": cid if cid is not None else key[1],
                "conv": cid,
                "summary": summary, "mode": g["last_mode"],
                "label": label, "kind": kind, "done": done,
                "count": g["count"],
                "time": time.strftime("%m月%d日 %H:%M", time.localtime(g["last_ts"])).lstrip("0"),
            })
        out.sort(key=lambda r: r["time"], reverse=True)  # 按最后活动倒序(粗粒度字符串序即可)
        return out[:200]
    except sqlite3.Error:
        return []
    finally:
        db.close()


def _run_detail(gui: "OtterWebGui", run_id: int) -> str:
    db = _db_ro(gui.db_path)
    if db is None:
        return "(无事件库)"
    lines = [f"Run #{run_id} — Trace", "=" * 46]
    try:
        events = db.execute(
            "SELECT type, payload_json, created_at FROM events WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
        tokens_in = tokens_out = 0
        tools: dict[str, int] = {}
        for type_, payload_json, ts in events:
            p = json.loads(payload_json or "{}")
            hhmmss = time.strftime("%H:%M:%S", time.localtime(ts))
            if type_ == "MODEL_STARTED":
                lines.append(f"{hhmmss}  ▶ step {p.get('step')} 模型调用")
            elif type_ == "MODEL_COMPLETED":
                tokens_in += p.get("usage_in") or 0
                tokens_out += p.get("usage_out") or 0
                t = p.get("tool_calls") or []
                lines.append(f"{hhmmss}  ✔ step {p.get('step')} 完成 · 输出 {p.get('content_chars', 0)} 字符"
                             + (f" · 调用 {', '.join(t)}" if t else ""))
            elif type_ == "TOOL_STARTED":
                args = str(p.get("arguments", {}))
                lines.append(f"{hhmmss}  ⚡ {p.get('name')} {args[:90]}")
            elif type_ == "TOOL_COMPLETED":
                n = p.get("name", "?")
                tools[n] = tools.get(n, 0) + 1
            elif type_ == "MODEL_ERROR":
                lines.append(f"{hhmmss}  ✘ {p.get('error', '')}")
            elif type_ == "RUN_FINALIZING":
                lines.append(f"{hhmmss}  ! 收尾专用步(零工具)")
        lines += ["", f"tokens: {tokens_in} in / {tokens_out} out",
                  "工具统计: " + (" · ".join(f"{k}×{v}" for k, v in tools.items()) or "无")]
    except sqlite3.Error as exc:
        lines.append(f"读取失败:{exc}")
    finally:
        db.close()
    return "\n".join(lines)


def _probe_coroutine(gui: "OtterWebGui") -> None:
    """诊断模式(--gui-probe):在真实 GUI 的窗口/线程模型里跑 evaluate_js 真值探针。"""
    import time as _t

    async def run():
        # 2026-09-24 修复(0.2.0 验收轮暴露):窗口创建是异步的,冷启动可超过固定
        # sleep——原版只取一次 gui.window,窗口慢时 w 恒 None,全部 DOM 断言假死
        # (evaluate_js 抛 NoneType)。改为轮询等窗口(≤30s),再等前端就绪。
        w = None
        for _ in range(60):
            w = gui.window
            if w is not None:
                break
            await asyncio.sleep(0.5)
        if w is None:
            print("[probe] 窗口 30s 未就绪,放弃(探针环境异常)", flush=True)
            return []
        await asyncio.sleep(1.0)  # 窗口在,再等前端
        rows = []

        def probe(name, js):
            try:
                val = w.evaluate_js(js)
            except Exception as exc:
                val = f"<EXC {type(exc).__name__}: {exc}>"
            rows.append((name, val))
            print(f"[{name}] → {val!r}", flush=True)

        print("\n===== evaluate_js 地面真值(真实 GUI 窗口)=====", flush=True)
        probe("A1 同步表达式 1+1", "1+1")
        probe("A2 Promise.resolve(5)", "Promise.resolve(5)")
        probe("A3 otterUI", "typeof otterUI")
        probe("A3b onDiffPreview", "typeof (otterUI && otterUI.onDiffPreview)")
        probe("A3c esc", "typeof esc")
        probe("A3d showDiffCard", "typeof showDiffCard")
        probe("A3e marked", "typeof marked")
        probe("A5 注入错误捕获", "window.__errLog=[]; window.onerror=function(m){window.__errLog.push(String(m)); return false}; 'ok'")
        probe("A4 onDiffPreview 调用", "otterUI.onDiffPreview({name:'probe', path:'x.py', diff:'@@ -1 +1 @@'})")
        await asyncio.sleep(0.6)
        probe("A4b diff卡片数", "document.querySelectorAll('.diff-card').length")
        probe("A4c 前端错误日志", "JSON.stringify(window.__errLog)")
        probe("A6 __pending", "window.__pending ? JSON.stringify(window.__pending) : 'none'")
        print("===== 判定 =====", flush=True)
        print(f"A1={rows[0][1]!r} A2={rows[1][1]!r} esc={rows[4][1]!r}", flush=True)
        w.destroy()

    asyncio.run_coroutine_threadsafe(run(), gui.loop)


def _e2e_coroutine(gui: "OtterWebGui") -> None:
    """端到端测试(--gui-e2e):真实提交任务 → 轮询 diff 卡片 → 脚本点采纳/拒绝 → 验磁盘。
    2026-09-23 教训落地:GUI 验收必须走 DOM 断言,不靠人眼。"""

    async def run():
        import time as _t
        from pathlib import Path as _P

        # 2026-09-24 修复(0.2.0 验收轮暴露):窗口创建是异步的,冷启动可超过固定
        # sleep——原版只取一次 gui.window,窗口慢时 w 恒 None,全部 DOM 断言假死
        # (evaluate_js 抛 NoneType)。改为轮询等窗口(≤30s),再等前端就绪。
        w = None
        for _ in range(60):
            w = gui.window
            if w is not None:
                break
            await asyncio.sleep(0.5)
        if w is None:
            print("[probe] 窗口 30s 未就绪,放弃(探针环境异常)", flush=True)
            return
        await asyncio.sleep(2.5)  # 窗口在,再等前端(原时序保留)
        target = _P.cwd() / "gui_e2e.txt"
        verdict = []

        def js(expr):
            try:
                return w.evaluate_js(expr)
            except Exception as exc:
                return f"<EXC {exc}>"

        def step(name, ok, detail=""):
            verdict.append((name, ok))
            print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)

        await asyncio.sleep(2.5)
        # E2E 前置:错误捕获双通道(onerror 抓同步,unhandledrejection 抓 async——
        # 2026-09-23 E2E 暴露:async 内异常 onerror 抓不到,导致断点一直隐身)
        js("window.__errLog=[]; window.onerror=function(m){window.__errLog.push('onerror:'+m); return false};"
           "window.addEventListener('unhandledrejection', function(e){window.__errLog.push('rejection:'+(e.reason&&e.reason.stack||e.reason))}); 'ok'")
        # 修正(2026-09-23 E2E 时序):必须等 window.pywebview 就绪再提交——
        # 首版提交早于 pywebviewready,任务根本没到 Python(上一轮 0/2 的根因)
        api_ready = False
        for _ in range(40):
            if str(js("typeof window.pywebview")) == "object":
                api_ready = True
                break
            await asyncio.sleep(0.5)
        step("pywebview api 就绪", api_ready)
        if not api_ready:
            w.destroy()
            return

        async def submit_and_wait_diffwin(task, timeout_s=150):
            """直连 api.submit + 轮询独立窗 diff 内容非空 + 秒表在走。"""
            import json as _json

            payload = _json.dumps(task, ensure_ascii=False)
            js(f"(function(){{ var d=document.createElement('div'); d.className='msg-user';"
               f"var b=document.createElement('div'); b.className='bubble'; b.textContent={payload};"
               f"d.appendChild(b); document.querySelector('#thread').appendChild(d); }})()")
            js(f"window.pywebview.api.submit({payload})")
            clock_ticked = False
            for _ in range(timeout_s * 2):
                await asyncio.sleep(0.5)
                # 秒表(chat 内"正在思考中"行,主窗 DOM)运行中应非 0s
                if not clock_ticked:
                    t = js("document.querySelector('.thinking') ? document.querySelector('.thinking').textContent : ''")
                    if t and ("1s" in str(t) or "s(" in str(t) or "s ·" in str(t) or ("已思考" in str(t) and "0s" not in str(t))):
                        clock_ticked = True
                win = getattr(gui, "_diffwin", None)
                if win is None:
                    continue
                try:
                    # 2026-09-24 修复(0.2.0 验收轮):R7 把确认窗改为简洁提问式(无 #diff
                    # 元素、按钮为 class 非 id)——旧断言永远 FAIL,且探针不点击导致弹窗
                    # 挂满 300s(用户看到的"测试卡住")。改为判定两颗按钮就绪。
                    btns = win.evaluate_js("document.querySelectorAll('.btns button').length")
                    if btns and int(btns) >= 2:
                        return True, clock_ticked
                except Exception:
                    pass
            print("  调试:独立 diff 窗口无内容(任务未跑或未触发写盘)", flush=True)
            return None, clock_ticked

        def diffwin_click(ok: bool):
            try:
                # 2026-09-24 修复:R7 确认窗按钮为 class(.ok/.no),旧版按 id 点击无效
                gui._diffwin.evaluate_js(
                    f"document.querySelector('.{'ok' if ok else 'no'}').click()")
            except Exception as exc:
                print(f"  diff 窗口点击异常:{exc}", flush=True)

        print("\n===== E2E:采纳路径(独立 diff 窗口)=====", flush=True)
        target.write_text("alpha", encoding="utf-8")
        card, clock1 = await submit_and_wait_diffwin(
            "用 edit_file 工具把 gui_e2e.txt 里的 alpha 改成 beta。禁止用 bash 写文件。")
        step("独立 diff 窗口有内容", bool(card))
        step("秒表在走(运行中非 00:00)", clock1)
        if card:
            diffwin_click(ok=True)  # 模拟点采纳写入
            written = False
            for _ in range(40):
                await asyncio.sleep(0.25)
                if target.exists() and "beta" in target.read_text(encoding="utf-8"):
                    written = True
                    break
            step("采纳后文件已写入 beta", written)

        print("===== E2E:R7 同目录会话放行(不再弹窗)=====", flush=True)
        # 2026-09-24 R7(用户要求):同目录一次允许后,本会话该目录写盘不再弹确认窗——
        # 轮询窗口"没有再弹"(超时无窗)且文件被直接写入
        card1b, _ = await submit_and_wait_diffwin(
            "用 edit_file 工具把 gui_e2e.txt 里的 beta 改成 beta2。禁止用 bash 写文件。",
            timeout_s=45)  # 2026-09-24:25s 短于模型实际耗时,断言时文件尚未写完
        step("同目录第二次写:不再弹确认窗", card1b is None)
        step("同目录第二次写:文件直接写入 beta2",
             target.exists() and "beta2" in target.read_text(encoding="utf-8"))

        print("===== E2E:拒绝路径(换目录,绕开 R7 会话放行)=====", flush=True)
        # R7:同目录已放行,拒绝路径必须换目录(同层级不同)才会再弹窗
        sub = _P.cwd() / "gui_e2e_sub"
        sub.mkdir(exist_ok=True)
        target2 = sub / "t.txt"
        card2, _ = await submit_and_wait_diffwin(
            "用 write_file 工具在 gui_e2e_sub/t.txt 写入 gamma。禁止用 bash 写文件。")
        step("换目录后 diff 窗口再次有内容", bool(card2))
        if card2:
            diffwin_click(ok=False)
            await asyncio.sleep(3)
            step("拒绝后文件未创建(无 gamma)",
                 not target2.exists() or "gamma" not in target2.read_text(encoding="utf-8"))

        print(f"\n===== 汇总:{sum(1 for _, ok in verdict if ok)}/{len(verdict)} PASS =====", flush=True)
        for name, ok in verdict:
            print(f"  {'✓' if ok else '✗'} {name}", flush=True)
        w.destroy()

    asyncio.run_coroutine_threadsafe(run(), gui.loop)


def _artifact_probe_coroutine(gui: "OtterWebGui") -> None:
    """诊断模式(--gui-artifact-probe):定位交接遗留的『产物卡片偶发缺失(meta 可见而
    完整卡片时有时无)』。真实 WKWebView 窗口 + 真实 _js 跨桥路径,按真实时序注入
    onDelta/onEvent/onDone/onHistory(含悬空 thinkingEl 场景),每轮 DOM 断言
    .artifact-preview-card 数量与 __errLog——GUI 验收走 DOM 断言,不靠人眼(2026-09-23)。"""

    async def run():
        # 保险丝(2026-09-23 僵死窗口事故):任何异常路径都必须 destroy 窗口——
        # 此前探针经 |head 管道被 SIGPIPE 打断,coroutine 死于中途,窗口永久僵死在桌面
        try:
            await _run_inner()
        except BaseException as exc:
            print(f"[probe] 异常终止:{type(exc).__name__}: {exc}", flush=True)
        finally:
            try:
                w.destroy()
            except Exception:
                pass

    async def _run_inner():
        import tempfile as _tf
        from pathlib import Path as _P

        # 2026-09-24 修复(0.2.0 验收轮暴露):窗口创建是异步的,冷启动可超过固定
        # sleep——原版只取一次 gui.window,窗口慢时 w 恒 None,全部 DOM 断言假死
        # (evaluate_js 抛 NoneType)。改为轮询等窗口(≤30s),再等前端就绪。
        w = None
        for _ in range(60):
            w = gui.window
            if w is not None:
                break
            await asyncio.sleep(0.5)
        if w is None:
            print("[probe] 窗口 30s 未就绪,放弃(探针环境异常)", flush=True)
            return
        await asyncio.sleep(2.5)  # 窗口在,再等前端就绪

        # 产物样本:噪声图(大 base64)/纯色图/代码/二进制,预览 payload 走真实 _artifact_preview
        tmpdir = _P(_tf.mkdtemp(prefix="otter_artprobe_"))
        samples = {}
        try:
            import random as _random

            from PIL import Image as PILImage

            noisy = tmpdir / "noisy.png"
            PILImage.new("RGB", (600, 600)) .putpixel((0, 0), (0, 0, 0))
            img = PILImage.new("RGB", (600, 600))
            img.putdata([(_random.randrange(256), _random.randrange(256), _random.randrange(256))
                         for _ in range(600 * 600)])
            img.save(noisy)
            samples["noisy"] = noisy
            flat = tmpdir / "flat.png"
            PILImage.new("RGB", (480, 480), (61, 115, 217)).save(flat)
            samples["flat"] = flat
        except Exception:
            pass  # 无 PIL 时只测文本/二进制样本
        code = tmpdir / "sample.py"
        code.write_text("\n".join(f"print({i})" for i in range(60)), encoding="utf-8")
        samples["code"] = code
        blob = tmpdir / "sample.bin"
        blob.write_bytes(bytes(range(256)) * 8)
        samples["bin"] = blob

        def js(expr):
            try:
                return w.evaluate_js(expr)
            except Exception as exc:
                return f"<EXC {exc}>"

        # 基线指纹(2026-09-23 #44 定位过程留存):新 app.js 是否真被加载 + 事件是否可达
        # (#44 教训:pywebview 吞 JS 异常,无报错≠执行成功,必须看 DOM 变化)
        # 2026-09-23 深夜追加:入口页缓存排查——convSearch/seg-btn 在=新 index.html 生效
        appjs = js("(function(){var s=document.querySelector('script[src*=app]');return s?s.src:'none'})()")
        print(f"[probe] otterUI={js('typeof otterUI')} readyState={js('document.readyState')} "
              f"mountAboveThinking={js('typeof mountAboveThinking')} "
              f"convSearch={js('document.getElementById(\"convSearch\") !== null')} "
              f"segBtns={js('document.querySelectorAll(\".seg-btn\").length')} "
              f"appjsSrc={str(appjs)[:100]}", flush=True)

        verdict = []

        def step(name, ok, detail=""):
            verdict.append((name, ok))
            print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)

        # 错误捕获双通道(教训:async 异常要 unhandledrejection 才抓得到)
        js("window.__errLog=[];"
           "window.onerror=function(m){window.__errLog.push('onerror:'+m); return false};"
           "window.addEventListener('unhandledrejection',function(e){window.__errLog.push('rejection:'+(e.reason&&e.reason.stack||e.reason))});"
           "'ok'")

        def bad_errors():
            """只把真错误(onerror/rejection/error 字样)当失败——#44 修后调试面包屑已清除。"""
            raw = str(js("JSON.stringify(window.__errLog)"))
            return "" if raw in ("[]", "None", "") and "error" not in raw.lower() else raw[:300]

        def emit_event(type_, payload):
            gui._js("onEvent", {"type": type_, **payload})  # 与真实链路同函数同线程

        def emit_artifact(path, note):
            pl = gui._artifact_preview(str(path))
            emit_event("ARTIFACT", {"path": str(path), "name": path.name, "note": note, **pl})

        async def round_card(tag, path, mode):
            """mode: stream=流式中途 / started=MODEL_STARTED 后立即 / idle=onDone 后"""
            if mode != "idle":
                emit_event("MODEL_STARTED", {"step": 1, "mode": "normal"})
            if mode == "stream":
                for chunk in ["先分析", "一下需求", ",然后生成文件"]:
                    gui._js("onDelta", chunk)
                    await asyncio.sleep(0.03)
            emit_artifact(path, tag)
            if mode == "stream":
                for chunk in ["收尾说明一", "收尾说明二"]:
                    gui._js("onDelta", chunk)
                    await asyncio.sleep(0.03)
                gui._js("onDone", {"summary": f"done({tag})", "final_text": ""})
            elif mode == "started":
                gui._js("onDelta", "模型开始输出")
                gui._js("onDone", {"summary": f"done({tag})", "final_text": ""})
            await asyncio.sleep(0.4)  # 等防抖(120ms)渲染与 DOM 稳定
            n = js("document.querySelectorAll('.artifact-preview-card').length")
            errs = str(js("JSON.stringify(window.__errLog)"))
            return int(n or 0), errs

        base = int(js("document.querySelectorAll('.artifact-preview-card').length") or 0)

        # A. 三种时序 × 样本类型逐一验证(卡片数应每轮 +1)
        for mode in ("stream", "started", "idle"):
            for tag in ("noisy", "flat", "code", "bin"):
                if tag not in samples:
                    continue
                n0 = int(js("document.querySelectorAll('.artifact-preview-card').length") or 0)
                await round_card(f"{tag}-{mode}", samples[tag], mode)
                n = int(js("document.querySelectorAll('.artifact-preview-card').length") or 0)
                step(f"卡片[{tag}/{mode}] +1", n == n0 + 1, f"({n0}→{n})")
                errs = bad_errors()
                if errs:
                    step(f"  __errLog[{tag}/{mode}] 干净", False, errs)

        # B. 大 payload(noisy 图,base64 数百 KB)流式中连发 3 张——验竞态/丢帧
        if "noisy" in samples:
            n0 = int(js("document.querySelectorAll('.artifact-preview-card').length") or 0)
            emit_event("MODEL_STARTED", {"step": 1, "mode": "normal"})
            for i in range(3):
                gui._js("onDelta", f"批量生成第 {i} 份 ")
                emit_artifact(samples["noisy"], f"rapid{i}")
            gui._js("onDone", {"summary": "done(rapid)", "final_text": ""})
            await asyncio.sleep(0.6)
            n = int(js("document.querySelectorAll('.artifact-preview-card').length") or 0)
            step("连发 3 张大图卡 +3", n == n0 + 3, f"({n0}→{n})")
            errs = bad_errors()
            if errs:
                step("  __errLog[rapid] 干净", False, errs)

        # C. 悬空 thinkingEl 场景(#43 根因复现):onHistory 清屏后立即开新一轮——
        #    修复前 thinkingEl 悬空 → insertBefore 抛 NotFoundError → 事件整批丢失
        gui._js("onHistory", [])
        await asyncio.sleep(0.2)
        emit_event("MODEL_STARTED", {"step": 1, "mode": "normal"})  # 运行中查思考行(onDone 后被移除属正常)
        await asyncio.sleep(0.2)
        think = js("document.querySelector('.thinking') !== null")
        step("onHistory 后思考行能复活(#43 自愈)", str(think) in ("true", "True"))
        n0 = int(js("document.querySelectorAll('.artifact-preview-card').length") or 0)
        n, _ = await round_card("after-history", samples["code"], "stream")
        step("onHistory 清屏后卡片照常 +1(#43)", n == n0 + 1, f"({n0}→{n})")
        errs = bad_errors()
        if errs:
            step("  __errLog[after-history] 干净(#43)", False, errs)

        # D. FILE_CHANGED 链接行(2026-09-24 R5):📎 行渲染 + 点击内联展开预览卡 + 再点收起
        pl = gui._artifact_preview(str(samples["code"]))
        emit_event("FILE_CHANGED", {"path": str(samples["code"]), "name": samples["code"].name,
                                    "action": "已修改", "plus": 3, "minus": 1, **pl})
        await asyncio.sleep(0.3)
        step("FILE_CHANGED 链接行渲染",
             str(js("document.querySelector('.file-changed .file-link') !== null")) in ("true", "True"))
        n0 = int(js("document.querySelectorAll('.artifact-preview-card').length") or 0)
        js("document.querySelector('.file-changed .file-link').onclick()")
        await asyncio.sleep(0.3)
        n1 = int(js("document.querySelectorAll('.artifact-preview-card').length") or 0)
        step("点击文件名内联展开预览卡 +1", n1 == n0 + 1, f"({n0}→{n1})")
        js("document.querySelector('.file-changed .file-link').onclick()")
        await asyncio.sleep(0.3)
        n2 = int(js("document.querySelectorAll('.artifact-preview-card').length") or 0)
        step("再点文件名收起预览卡", n2 == n0, f"({n1}→{n2})")
        errs = bad_errors()
        if errs:
            step("  __errLog[FILE_CHANGED] 干净", False, errs)

        total = len(verdict)
        passed = sum(1 for _, ok in verdict if ok)
        print(f"\n===== 产物卡片探针汇总:{passed}/{total} PASS =====", flush=True)
        w.destroy()

    asyncio.run_coroutine_threadsafe(run(), gui.loop)


def launch_gui(probe: bool = False, e2e: bool = False, artifact_probe: bool = False) -> int:
    config = Config.load()
    if not config.api_key:
        print("错误:未配置 OTTER_API_KEY(复制 .env.example 为 .env,或设置环境变量)", flush=True)
        return 2
    gui = OtterWebGui(config)
    if probe:
        _probe_coroutine(gui)
    if e2e:
        _e2e_coroutine(gui)
    if artifact_probe:
        _artifact_probe_coroutine(gui)  # 2026-09-23 #43:产物卡片偶发缺失定位与回归
    gui.run()
    return 0

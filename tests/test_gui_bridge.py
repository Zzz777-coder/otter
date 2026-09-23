"""GUI 桥接层诊断探针 + 端到端测试(2026-09-23;需开窗口手动跑,不发 pytest 收集)。

用法:
  阶段A 真值探针:.venv/bin/python tests/test_gui_bridge.py probe
  阶段C 端到端  :.venv/bin/python tests/test_gui_bridge.py e2e
历次教训:GUI 验收必须走脚本化 DOM 断言,不允许"我看着像好了"。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def make_window():
    """创建与 OtterWebGui 同款的窗口,等待 pywebviewready。"""
    import webview

    from otter.config import Config

    config = Config.load()
    html = ROOT / "otter" / "web" / "index.html"
    # nonce 釜底抽薪:每次启动唯一 URL,彻底绕开 WKWebView 对 file:// 的任何缓存
    url = f"file://{html}?nonce={time.time()}"
    window = webview.create_window("bridge-probe", url=url, width=1000, height=700)
    ready = {"v": False}

    def on_loaded():
        ready["v"] = True

    window.loaded += on_loaded
    webview.start(gui="cocoa", debug=False)
    return window


def main_probe():
    """阶段A:evaluate_js 地面真值。与真实 GUI 同构:主线程 webview.start() 阻塞,
    后台线程做探测(修正:func= 模式窗口起不来——'Main window failed to start')。"""
    import threading

    import webview

    html = ROOT / "otter" / "web" / "index.html"
    url = f"file://{html}?nonce={time.time()}"
    window = webview.create_window("bridge-probe", url=url, width=1000, height=700)

    def run_probes():
        time.sleep(3.5)  # 等 pywebviewready + api 握手
        print("\n===== 阶段A:evaluate_js 地面真值 =====")
        rows = []

        def probe(name, js):
            try:
                val = window.evaluate_js(js)
            except Exception as exc:
                val = f"<EXC {type(exc).__name__}: {exc}>"
            rows.append((name, js, val))
            print(f"[{name}] {js}\n  → {val!r}")

        probe("A1 同步表达式", "1+1")
        probe("A2 Promise 语义", "Promise.resolve(5)")
        probe("A3 otterUI 存在", "typeof otterUI")
        probe("A3b onDiffPreview", "typeof (otterUI && otterUI.onDiffPreview)")
        probe("A3c esc 存在", "typeof esc")
        probe("A3d showDiffCard", "typeof showDiffCard")
        probe("A3e marked 存在", "typeof marked")
        probe("A5 错误捕获注入", "window.__errLog=[]; window.onerror=function(m){window.__errLog.push(String(m)); return false}; 'injected'")
        probe("A4 创建diff卡片", "otterUI.onDiffPreview({name:'probe', path:'x.py', diff:'@@ -1 +1 @@'})")
        time.sleep(0.6)
        probe("A4b 卡片在DOM", "document.querySelectorAll('.diff-card').length")
        probe("A4c 前端错误日志", "JSON.stringify(window.__errLog)")
        probe("A6 pending通道", "window.__pending ? JSON.stringify(window.__pending) : 'none'")
        print("\n===== 探针完成(判定) =====")
        print(f"A1 = {rows[0][2]!r}(应为 2)")
        print(f"A2 Promise = {rows[1][2]!r}  → {'等 Promise' if rows[1][2] == 5 else '不等 Promise'}")
        print(f"A3c esc = {rows[4][2]!r}  → {'新版' if rows[4][2] == 'function' else '!! 旧版/缺失'}")
        window.destroy()
        time.sleep(0.5)
        import os
        os._exit(0)

    threading.Thread(target=run_probes, daemon=True).start()
    webview.start(gui="cocoa")


if __name__ == "__main__":
    main_probe()

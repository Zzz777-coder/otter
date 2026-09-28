"""CLI 入口:python -m otter(或安装后的 otter 命令)。

用法:
  otter                      进入交互 REPL
  otter -p "任务描述"         单任务模式(headless 雏形;结构化 JSON 输出是 M2)
  otter --max-steps 20       限制步数
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path  # GUI 分流前的最近工作区登记用(cwd 解析)

from otter import __version__
from otter.config import Config
from otter.repl import run_repl, run_single


def main() -> int:
    parser = argparse.ArgumentParser(prog="otter", description="otter — 轻量本地通用 AI 助手(编码/文件/命令/日常)")
    parser.add_argument("--version", action="version", version=f"otter {__version__}",
                        help="显示版本号并退出(2026-09-29 P1:版本单源=__init__.__version__)")
    parser.add_argument("-p", "--prompt", help="单任务模式:执行该任务后退出", default=None)
    parser.add_argument("--gui", action="store_true", help="启动桌面 GUI 薄壳(Tkinter,2026-09-19 新增)")
    parser.add_argument("--gui-probe", action="store_true", help="GUI 诊断模式:启动后跑 evaluate_js 真值探针并退出(2026-09-23)")
    parser.add_argument("--gui-e2e", action="store_true", help="GUI 端到端测试:真实任务→diff卡片→脚本点击→验磁盘,输出 PASS/FAIL(2026-09-23)")
    parser.add_argument("--gui-artifact-probe", action="store_true",
                        help="GUI 产物卡片探针:真实窗口注入事件流,DOM 断言卡片渲染(2026-09-23 #43)")
    parser.add_argument("--model", help="覆盖 OTTER_MODEL 配置", default=None)
    parser.add_argument("--max-steps", type=int, default=None, help="单次任务最大步数(默认取配置 30)")
    parser.add_argument("--plan", action="store_true", help="PLAN 只读模式(仅检索与分析,不写文件;M1 新增)")
    parser.add_argument("--resume", action="store_true", help="恢复最近一次中断的 Run(M2 新增)")
    parser.add_argument("--output-format", choices=["text", "json", "stream-json"], default="text",
                        help="-p 模式输出格式:json=收尾一次性;stream-json=NDJSON 流式逐事件(2026-09-24 新增)")
    parser.add_argument("--yes", action="store_true",
                        help="跳过审批交互全部允许(供 CI/headless;请确认任务可信,M2 新增)")
    parser.add_argument("--serve", action="store_true",
                        help="启动本地 HTTP API 服务(FastAPI,/docs 交互文档;2026-09-29 新增)")
    parser.add_argument("--serve-port", type=int, default=8765,
                        help="--serve 的端口(默认 8765;2026-09-29 新增)")
    args = parser.parse_args()

    # 2026-09-29 HTTP API 模式:R1 仅会话/消息/事件的读写,不依赖模型 key,
    # 故放在 api_key 检查之前分流;chat 类端点(R2)接入后再收紧
    if args.serve:
        from otter.api import serve

        return serve(port=args.serve_port)

    config = Config.load()
    if args.model:
        config.model = args.model
    max_steps = args.max_steps or config.max_steps

    if not config.api_key:
        print("错误:未配置 OTTER_API_KEY(复制 .env.example 为 .env 并填入,或设置环境变量)", file=sys.stderr)
        return 2

    # 2026-09-24 最近工作区(用户要求:GUI 跨工作区查看历史)——所有 CLI 形态
    # (REPL/-p/GUI)启动即登记 cwd 到 ~/.otter/recent_workspaces.json;
    # 登记失败静默(纯附加便利信息,不阻断启动)
    from otter.workspaces import register_workspace

    register_workspace(Path.cwd())

    # GUI 模式有自己的主循环(2026-09-19 M-GUI;M4 起为可选 extras,缺依赖时给出安装指引)
    if args.gui or args.gui_probe or args.gui_e2e or args.gui_artifact_probe:
        try:
            from otter.gui import launch_gui
        except ImportError as exc:
            print(f"GUI 依赖未安装({exc})。安装:pip install 'otter-agent[gui]'", file=sys.stderr)
            return 2
        # 2026-09-23 #43:新增 artifact_probe 通道(产物卡片偶发缺失定位与回归)
        return launch_gui(probe=args.gui_probe, e2e=args.gui_e2e, artifact_probe=args.gui_artifact_probe)

    async def _run() -> int:
        # 2026-09-29 P1:装配统一走库入口 build_engine(CLI 与 import otter 同一条装配路径)
        from otter.engine import build_engine

        try:
            engine = await build_engine(config=config, yes=args.yes)
        except RuntimeError as exc:  # key 缺失(库入口抛错,CLI 转终端提示)
            print(f"错误:{exc}", file=sys.stderr)
            return 2
        if engine.reconciled:
            # 2026-09-24 修正:诊断提示走 stderr——stream-json 模式下 stdout 必须是纯 NDJSON
            # (真机首跑即被这行污染,非法 JSON 行会打断 jq 逐行消费)
            print(f"[otter] 启动修正:{engine.reconciled} 个遗留 Run 已标记 interrupted"
                  "(otter --resume 可恢复)", file=sys.stderr)
        if engine.mcp_report:
            # 2026-09-24 修正:同上,诊断提示走 stderr 保 stdout 纯 NDJSON
            print("[otter] MCP: " + "; ".join(engine.mcp_report), file=sys.stderr)
        loop, store = engine.loop, engine.store
        try:
            if args.resume:
                from otter.repl import run_resume

                return await run_resume(loop, store, max_steps)
            if args.prompt is not None:
                mode = "plan" if args.plan else "normal"
                if args.output_format == "stream-json":
                    # 2026-09-24 headless stream-json:NDJSON 流式(常配 --yes 供 CI)
                    from otter.repl import run_single_stream

                    return await run_single_stream(loop, store, args.prompt, max_steps, mode)
                return await run_single(loop, store, args.prompt, max_steps, mode,
                                        json_output=args.output_format == "json")
            await run_repl(loop, store, max_steps)
            return 0
        finally:
            await engine.aclose()  # 2026-09-29 P1:释放统一走 Engine.aclose

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())

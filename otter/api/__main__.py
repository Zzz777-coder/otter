"""python -m otter.api — 启动本地 HTTP API 服务(2026-09-29 新增)。

默认只绑 127.0.0.1(本机开发者入口);--host 开放局域网前须先补
token 鉴权(R2+ 议题),当前明确不做开放承诺。cwd 即工作区——与
GUI/CLI 同规则,从哪个目录启动就看哪个目录的 .otter/otter.db。

R2(2026-09-29)起 /api/chat 需要模型 key:启动时即装配引擎并做
key 检查(缺 key 直接退出),不再等到首次 chat 请求才失败。
"""

from __future__ import annotations

import argparse
import sys


def serve(host: str = "127.0.0.1", port: int = 8765) -> int:
    parser = argparse.ArgumentParser(prog="otter.api", description="otter 本地 HTTP API 服务")
    parser.add_argument("--host", default=host, help="绑定地址(默认 127.0.0.1,仅本机)")
    parser.add_argument("--port", type=int, default=port, help="端口(默认 8765)")
    args = parser.parse_args()

    import asyncio
    import pathlib

    import uvicorn

    from otter.api import create_app

    # 2026-09-29 R2:chat 端点依赖引擎与模型 key——启动即装配(缺 key 退出),
    # 只读端点的离线测试不受影响(create_app(engine=None) 走 503 分支)。
    from otter.config import Config

    config = Config.load()
    if not config.api_key:
        print("错误:未配置 OTTER_API_KEY(复制 .env.example 为 .env 并填入,或设置环境变量)",
              file=sys.stderr)
        return 2

    async def _build():
        from otter.engine import build_engine

        # yes=True:HTTP headless 场景跳过终端审批交互(审批通道接入 GUI DiffApi 是 P4 范围)
        return await build_engine(config=config, yes=True)

    engine = asyncio.run(_build())
    print(f"[otter] API 服务:http://{args.host}:{args.port}/docs"
          f"(工作区:{pathlib.Path.cwd()},模型:{config.model})")
    uvicorn.run(create_app(engine=engine), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())

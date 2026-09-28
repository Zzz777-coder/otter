"""python -m otter.api — 启动本地 HTTP API 服务(2026-09-29 新增)。

默认只绑 127.0.0.1(本机开发者入口);--host 开放局域网前须先补
token 鉴权(R2+ 议题),当前明确不做开放承诺。cwd 即工作区——与
GUI/CLI 同规则,从哪个目录启动就看哪个目录的 .otter/otter.db。
"""

from __future__ import annotations

import argparse


def serve(host: str = "127.0.0.1", port: int = 8765) -> int:
    parser = argparse.ArgumentParser(prog="otter.api", description="otter 本地 HTTP API 服务")
    parser.add_argument("--host", default=host, help="绑定地址(默认 127.0.0.1,仅本机)")
    parser.add_argument("--port", type=int, default=port, help="端口(默认 8765)")
    args = parser.parse_args()

    import uvicorn

    from otter.api import create_app

    print(f"[otter] API 服务:http://{args.host}:{args.port}/docs(工作区:{__import__('pathlib').Path.cwd()})")
    uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())

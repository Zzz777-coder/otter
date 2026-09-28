"""otter.api — 本地 HTTP API(FastAPI,2026-09-29 新增)。

定位:开发者入口——浏览器 /docs 交互文档 + curl/脚本可编程调用,
与桌面 GUI(js_api 桥)互不影响。R1 范围:health + 会话 CRUD +
消息历史 + Run 事件流,全部只依赖持久层 Store,不需要模型 API key。
"""

from otter.api.app import create_app

__all__ = ["create_app", "serve"]


def serve(host: str = "127.0.0.1", port: int = 8765) -> int:
    """启动本地 HTTP API 服务(2026-09-29 新增;实现见 __main__,此处转发便于 `from otter.api import serve`)。"""
    from otter.api.__main__ import serve as _serve

    return _serve(host=host, port=port)

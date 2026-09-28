# otter server 镜像(2026-09-29 P7):无 GUI 的 server 模式(--serve)。
# 构建:docker build -t otter-agent .
# 运行:docker run --rm -p 8765:8765 -e OTTER_API_KEY=... -v "$PWD:/workspace" -w /workspace otter-agent
# 本机无 Docker 环境,此文件未实跑(2026-09-29 留档;compose 同);
# 基础镜像与依赖口径与干净 venv 冒烟(已实跑)一致。

FROM python:3.12-slim

# 安装发布物本体 + server extras(fastapi/uvicorn);不装 GUI(pywebview 需要图形栈)
WORKDIR /app
COPY dist/otter_agent-*.whl ./dist/
RUN pip install --no-cache-dir ./dist/otter_agent-*.whl[server]

# 数据与工作区约定:挂载点 /workspace(会话/记忆/交付物都在其 .otter/ 下)
RUN mkdir -p /workspace
WORKDIR /workspace
VOLUME /workspace

# 默认只绑容器内 loopback 会无法被宿主访问,server 启动参数用 0.0.0.0;
# 端口由 compose/运行参数映射,密钥一律走环境变量不进镜像
EXPOSE 8765
ENV OTTER_HOST=0.0.0.0

CMD ["otter", "--serve"]

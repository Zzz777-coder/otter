# otter server 镜像(2026-09-29 P7):无 GUI 的 server 模式(--serve)。
# 构建:docker build -t otter-agent .
# 运行:docker run --rm -p 8765:8765 -e OTTER_API_KEY=... -v "$PWD:/workspace" -w /workspace otter-agent
# 2026-10-11 实测通过(colima):build 成功(镜像 416MB),容器 --serve 正常,/api/health 返回 200;
# 实测顺带修正两处:绑定须 --serve-host 0.0.0.0(原 ENV OTTER_HOST 无效)、wheel+extras 的安装写法(见下方注释)。
# 基础镜像与依赖口径与干净 venv 冒烟(已实跑)一致。

FROM python:3.12-slim

# 安装发布物本体 + server extras(fastapi/uvicorn);不装 GUI(pywebview 需要图形栈)
WORKDIR /app
COPY dist/otter_agent-*.whl ./dist/
# 2026-10-11 修复:extras 的 [server] 方括号会被 sh 当 glob 一部分,匹配失败按原样传给 pip 而报错;
# 先用 ls 展开通配符取具体文件名,再带 extras 引号安装
RUN set -e; wheel=$(ls ./dist/otter_agent-*.whl | head -1); \
    pip install --no-cache-dir "$wheel[server]"

# 数据与工作区约定:挂载点 /workspace(会话/记忆/交付物都在其 .otter/ 下)
RUN mkdir -p /workspace
WORKDIR /workspace
VOLUME /workspace

# 默认只绑容器内 loopback 会无法被宿主访问,server 须用 --serve-host 0.0.0.0;
# 端口由 compose/运行参数映射,密钥一律走环境变量不进镜像
EXPOSE 8765
# 2026-10-11 修复:绑定地址仅 --serve-host 参数支持(无 OTTER_HOST 环境变量),
# 原 ENV OTTER_HOST 无效会导致容器内绑 127.0.0.1、宿主访问不通;改为 CMD 显式传参
CMD ["otter", "--serve", "--serve-host", "0.0.0.0"]

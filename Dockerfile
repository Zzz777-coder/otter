# otter server 镜像(2026-09-29 P7):无 GUI 的 server 模式(--serve)。
# 构建:docker build -t otter-agent .
# 运行:docker run --rm -p 8765:8765 -e OTTER_API_KEY=... -v "$PWD:/workspace" -w /workspace otter-agent
# 2026-10-11 实测通过(colima):镜像 416MB,容器 --serve 正常,/api/health 返回 200;
# 实测顺带修正两处:绑定须 --serve-host 0.0.0.0(原 ENV OTTER_HOST 无效)、wheel+extras 的安装写法(见下方注释)。
# 2026-10-13 瘦身改多阶段构建:builder 装 venv 后剔除 pip/setuptools/__pycache__ 再拷进
# 纯净 final 层——运行时不需要包管理器与字节码缓存(416→346MB,见 compose 冒烟)。
# 2026-10-14 fpdf2 拆出核心依赖(移入 extras[pdf]):server 镜像连带甩掉 fontTools+pillow,
# 实测 346→284MB;make_pdf 工具懒导入,缺库时优雅降级为引导文案(有测试锁定)。

# ── 阶段一:装依赖进独立 venv ──
FROM python:3.12-slim AS build
WORKDIR /app
COPY dist/otter_agent-*.whl ./dist/
# 2026-10-11 修复:extras 的 [server] 方括号会被 sh 当 glob 一部分,匹配失败按原样传给 pip 而报错;
# 先用 ls 展开通配符取具体文件名,再带 extras 引号安装
# 2026-10-13:装进 /opt/venv(与系统 site-packages 隔离,便于整体搬运与裁剪)
RUN set -e; wheel=$(ls ./dist/otter_agent-*.whl | head -1); \
    python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir "$wheel[server]" && \
    /opt/venv/bin/pip cache purge || true; \
    find /opt/venv -name '__pycache__' -type d -exec rm -rf '{}' +; \
    find /opt/venv -name '*.pyc' -delete; \
    find /opt/venv/lib/python3.12/site-packages -maxdepth 2 -type d \( -name tests -o -name test \) -exec rm -rf '{}' +; \
    rm -rf /opt/venv/lib/python3.12/site-packages/pip* \
           /opt/venv/lib/python3.12/site-packages/setuptools* \
           /opt/venv/bin/pip*

# ── 阶段二:纯净运行层,只带 venv ──
FROM python:3.12-slim
# 2026-10-13:builder 的 wheel 拷贝、pip 临时文件、字节码缓存全部留在阶段一不进最终镜像
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

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

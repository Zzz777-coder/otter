# otter 🦦

轻量级 Python CLI 编码 Agent——为 DeepSeek 等模型一等优化,领域扩展底座。
水獭:自然界"抱着石头敲贝壳"的会用工具的动物,正是"给 agent 一台电脑"的画像。

## 安装

```bash
pip install otter-agent            # 从 PyPI(需要 Python ≥3.12)
# GUI 桌面壳(可选):pip install "otter-agent[gui]"
```

从源码运行:

```bash
git clone https://github.com/Zzz777-coder/otter.git
cd otter
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env   # 填入 OTTER_API_KEY
```

## 用法

```bash
otter                      # 交互 REPL
otter --gui                # 桌面 GUI(pywebview,可选依赖)
otter -p "任务描述"         # 单任务模式
otter -p "任务" --plan     # PLAN 只读模式(检索分析,不写文件)
otter -p "任务" --yes --output-format stream-json   # headless:NDJSON 流式输出(CI/cron 用)
```

支持端点:DeepSeek/OpenAI/Qwen/Ollama 等 OpenAI 兼容协议,以及 **Anthropic 原生 Messages API**(`OTTER_BASE_URL=https://api.anthropic.com` 即自动切换)。

REPL 斜杠命令:`/init` 生成 AGENTS.md · `/undo` 回滚上一组 otter 修改 · `/plan` `/act` 切只读/执行 · `/context` 查看压缩与预算状态 · `/reset` 清空会话。

每次 Run 的全程 Trace 落库在 `<工作目录>/.otter/otter.db`(runs/messages/events 三表)。
配置项见 `.env.example`(模型/上下文预算/摘要模型/git 自动提交等)。

## 路线图状态

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | Loop 五拍 + bash/read/write + DeepSeek adapter + REPL | ✅ 2026-09-19 |
| M1 | edit/grep/glob + 滚动摘要压缩 + AGENTS.md+/init + git 自动提交+/undo + Plan/Act + /context | ✅ 2026-09-22(验收:DeepSeek 多文件任务;小预算下多次压缩且会话正常;/undo 回滚 [otter] 提交) |
| M2 | 权限审批(once/session/总是)+ Seatbelt 沙箱 + Checkpoint/--resume + Evidence 归档回读 + headless JSON/--yes | ✅ 2026-09-22(kill -9 续跑/沙箱拦写/16万字归档/CI 输出四项验收过) |
| M3 | 双层记忆(Core/Ordinary+FTS5+确定性召回+反思门控+乐观锁)+ repo map(tree-sitter)+ 分层 edit format + tool_search/deferred + **Run 级费用预算三段(2026-09-22 补装:60% 提醒→85% 收口→100% 硬停,OTTER_RUN_BUDGET 配置,默认 30 万)** | ✅ 2026-09-22(跨会话记忆实测生效;repo_map 定位 undo/审批链路全对;31 测试) |
| M3.5 | diff 预览(REPL 内联/GUI 采纳卡片)+ GUI 审批卡片 + 前端冒烟测试(node,11 断言) | ✅ 2026-09-22(edit 前必见 diff;GUI 卡片采纳才写盘,fail-closed) |
| M4 | MCP 客户端(stdio) + 子代理 explore + Skill Learning(watermark+人工确认) + Artifact 产物系统 + **Replan 领域包(get_schedule/simulate_change/commit_reschedule)** + 打包分发 | ✅ 2026-09-22(急单重排真机演示:沙盘对比→提交→延误 449→0;Skill 从重计划 Trace 提炼 fault-impact-sandbox-check 并人工转正;pip install 后干净目录跑通;37 测试) |
| M-COMPUTER | 屏幕操控(机动,集成优先不自研) | 排 M4 后 |
| M-GUI | v7+:pywebview+HTML 渲染(CSS/JS 自写,lucide 图标 ISC 内联);2026-09-24 增长期记忆页/交付物页/rail 徽标 | ✅ |

## License

MIT(见 [LICENSE](LICENSE))。

## 测试

```bash
.venv/bin/python -m pytest tests/ -v   # 全离线,不发真实请求
```


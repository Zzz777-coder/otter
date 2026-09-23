# otter 🦦

轻量级 Python CLI 编码 Agent——为 DeepSeek 等模型一等优化,领域扩展底座。
水獭:自然界"抱着石头敲贝壳"的会用工具的动物,正是"给 agent 一台电脑"的画像。

产品说明书:`~/Desktop/otter-产品说明书.md`(唯一准绳)。

## 安装

```bash
cd ~/Desktop/otter
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env   # 填入 OTTER_API_KEY
```

## 用法

```bash
.venv/bin/otter                      # 交互 REPL
.venv/bin/otter --gui                # 桌面 GUI 薄壳(Tkinter,M-GUI)
.venv/bin/otter -p "任务描述"         # 单任务模式
.venv/bin/otter -p "任务" --plan     # PLAN 只读模式(检索分析,不写文件)
```

REPL 斜杠命令(M1):`/init` 生成 AGENTS.md · `/undo` 回滚上一组 otter 修改 · `/plan` `/act` 切只读/执行 · `/context` 查看压缩与预算状态 · `/reset` 清空会话。

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
| M-GUI | v7(2026-09-22):pywebview+HTML 渲染,vesta 观感(规格参数照抄+CSS 自写,lucide 图标 ISC 内联) | ✅ |

## 测试

```bash
.venv/bin/python -m pytest tests/ -v   # 全离线,不发真实请求
```

## 致谢

核心设计思想学习自 **Kong-lh-rgb/vesta**(未含许可证,故全部重新实现)、Claude Code 的公开设计、aider 的 repo map 方案。每处借鉴在代码注释中标明来源模块。

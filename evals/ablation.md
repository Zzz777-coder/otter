# otter ablation 对照实验(2026-10-02)

同代码版本(重试层合入后)、同任务集(39 任务八类)、同模型(deepseek-v4-flash),
仅切机制开关(OTTER_COMPRESS / OTTER_MEMORY),同日连续三组。

## 结果

| 组 | 开关 | 成功率 | 平均延迟 | 平均 token in | 平均步数 | 平均工具调用 |
|---|---|---|---|---|---|---|
| control | 全开 | 37/39(95%) | 7.7s | 19,291 | 4.5 | 3.8 |
| no-compress | 关滚动摘要 | 37/39(95%) | **6.7s** | **16,779** | 4.1 | 3.1 |
| no-memory | 关双层记忆 | 37/39(95%) | 7.1s | 19,509 | 4.6 | 3.9 |

各回归任务(daily-time-format 三组皆挂一次,code-regex-extract / sec-fence-parent 各一)
均为模型输出波动,与开关无对应关系(逐任务明细见 ablation-*.md)。

## 解读

1. **成功率对两机制不敏感**——不是"机制无用",而是评估集的形状决定的:
   任务平均 ~20k input token(窗口 128k 的 16%),压缩几乎不触发;每任务独立
   临时工作区,记忆没有历史可积累。两机制的**收益**在此集上无兑现场景。
2. **成本可测且真实**:"压缩不免费"拿到直接数字——关压缩组 token in 省 13%、
   延迟省 13%(摘要调用与更早的工具轮次消失)。关记忆组与对照持平(Core 为空、
   反思被确定性门控过滤,空闲开销≈0)。
3. **推论(下一步)**:要测出两机制的**正收益**,评估集需补两类任务——
   ① 长上下文任务(把对话推到触发水位以上,对比压缩前后的成功率与超窗率);
   ② 同工作区连续多 Run 任务(让记忆有累积,对比跨 Run 一致性)。
   已加入任务集迭代计划;在此之前,两机制的适用边界按上述数据如实陈述:
   **短任务/新工作区场景两机制只花成本不产收益,长任务/连续使用场景是它们的主场。**
4. 已知局限:单厂商单模型(deepseek),跨模型对照缺第二家 key,待补。

## 附:no-memory 组首跑死锁记录(待查)

首跑(15:49 起)于 20 分钟后停滞:主线程 kevent 空等、工作线程 SimpleQueue.get
阻塞,0 CPU、无网络连接;kill 重跑(同参数)正常完成。复现条件不明,疑与关记忆
路径的某条异步收尾链相关,留档待查——不影响本报告数据(重跑为完整 39 任务)。

## 复现

```bash
cd ~/otter
OTTER_COMPRESS=1 OTTER_MEMORY=1 .venv/bin/python evals/run_evals.py --out evals/ablation-control.md
OTTER_COMPRESS=0 .venv/bin/python evals/run_evals.py --out evals/ablation-nocompress.md
OTTER_MEMORY=0   .venv/bin/python evals/run_evals.py --out evals/ablation-nomemory.md
```

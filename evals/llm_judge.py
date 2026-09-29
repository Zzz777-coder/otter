"""LLM-as-judge(评估⑤,2026-09-29):开放性产出的模型评分。

机器判据(cmd/file/final_contains)盖不住"解释准不准/报告好不好"这类开放产出——
judge 用模型按 rubric 打 0-10 分,min_score(默认 7)以下判失败。

设计口径:
- 评分调用不带工具、单轮、输出只要一个 JSON 对象({"score","reason"});
- 解析容错:模型输出可能带 ```json 围栏或前后缀,取首个 {...} 块解析;
- judge 执行异常(网络/解析失败)返回 None——判据"未定"而非"失败",
  防网络抖动造成假回归(CI 门禁只看通过性,噪声必须可区分);
- 评分器走便宜模型优先(reflection/summary adapter),没配就主模型。
"""

from __future__ import annotations

import json
import re

from otter.models.types import Message

# 评分提示词(2026-09-29 评估⑤):rubric 决定打分维度,产出与参考文件全文对照
JUDGE_PROMPT = """你是严格的评估员。根据任务要求与评分标准,给 AI 助手的最终产出打分(0-10 整数)。

[任务要求]
{task}

[评分标准](全部满足=高分;核心事实错误=不及格)
{rubric}

[AI 助手的最终回答]
{final_text}

[参考文件](与回答对照用)
{files}

只输出一个 JSON 对象,不要输出其它任何内容:
{{"score": <0-10 整数>, "reason": "<不超过50字的理由>"}}"""


def build_judge_messages(task_text: str, rubric: str, final_text: str,
                         files: dict[str, str]) -> list[Message]:
    """构造评分请求的消息列表(system+user 单轮,无工具)。"""
    file_blocks = "\n".join(
        f"# {name}\n{content[:4000]}" for name, content in files.items()
    ) or "(无)"
    prompt = JUDGE_PROMPT.format(task=task_text, rubric=rubric,
                                 final_text=(final_text or "(空)")[:4000], files=file_blocks)
    return [Message(role="system", content="你是评估员,只输出要求的 JSON。"),
            Message(role="user", content=prompt)]


def extract_judge_json(text: str) -> dict | None:
    """从模型输出里提取 {"score","reason"}。容错:围栏/前后缀/浮点分/缺 reason。"""
    if not text:
        return None
    m = re.search(r"\{[^{}]*\}", text, re.S)  # 首个无嵌套花括号块
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "score" not in data:
        return None
    try:
        score = int(float(data["score"]))  # 模型偶发输出 8.0
    except (TypeError, ValueError):
        return None
    if not 0 <= score <= 10:
        return None
    return {"score": score, "reason": str(data.get("reason", ""))[:120]}


async def run_judge(adapter, task_text: str, rubric: str, final_text: str,
                    files: dict[str, str]) -> dict | None:
    """调模型评分,返回 {"score","reason"};解析不出返回 None(判据未定)。"""
    messages = build_judge_messages(task_text, rubric, final_text, files)
    try:
        resp = await adapter.complete_stream(messages)  # 无 tools,单轮
    except Exception:
        return None
    return extract_judge_json(resp.content or "")

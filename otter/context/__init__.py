"""上下文子系统(M1):事前 token 估算 + 滚动摘要压缩。

两级压缩思想,全部自行实现:
- 第一原则:原始历史永不修改,压缩只作用于"请求视图";
- 两套计数器:本目录的 tiktoken 估算管预防(触发压缩),adapter 返回的真实 usage 管问责;
- 滚动摘要输出结构化 JSON 并做代码级校验(schema 不止写在 prompt 里)。
"""

from otter.context.estimator import estimate_messages_tokens, estimate_tokens
from otter.context.summarizer import ConversationSummary, RollingSummarizer

__all__ = ["estimate_tokens", "estimate_messages_tokens", "ConversationSummary", "RollingSummarizer"]

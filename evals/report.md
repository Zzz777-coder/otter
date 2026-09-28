# otter 评估报告

- 生成时间:2026-09-29 03:31
- 引擎版本:v0.6.0 · 模型:deepseek-v4-flash
- 任务数:26 · 总耗时:121s(串行)

## 总览

| 指标 | 值 |
|---|---|
| 成功率 | **26/26(100%)** |
| 平均延迟 | 4.6s |
| 平均 token(in/out) | 15586 / 578 |
| 平均步数 | 4.3 |
| 平均工具调用 | 3.5(trace 事件口径 3.5) |

## 分类

| 类别 | 成功率 | 平均延迟 | 平均工具调用 |
|---|---|---|---|
| coding | 8/8(100%) | 6.5s | 4.4 |
| command | 5/5(100%) | 5.6s | 5.2 |
| daily | 6/6(100%) | 2.0s | 1.2 |
| file | 7/7(100%) | 4.0s | 3.1 |

## 逐任务

| id | 类别 | 结果 | 延迟s | 步数 | 工具 | token in/out | stop |
|---|---|---|---|---|---|---|---|
| code-func-fib | coding | ✅ | 8.7 | 4 | 3 | 13311/424 | final_answer |
| code-fix-bug | coding | ✅ | 3.7 | 4 | 3 | 13340/532 | final_answer |
| code-class-todo | coding | ✅ | 3.4 | 3 | 2 | 10310/603 | final_answer |
| code-regex-extract | coding | ✅ | 10.7 | 7 | 6 | 29774/1767 | final_answer |
| code-csv-transform | coding | ✅ | 7.1 | 8 | 7 | 30722/790 | final_answer |
| code-json-flatten | coding | ✅ | 2.5 | 3 | 2 | 9996/305 | final_answer |
| code-cli-wordcount | coding | ✅ | 6.6 | 7 | 6 | 26782/950 | final_answer |
| code-refactor-dedupe | coding | ✅ | 9.1 | 6 | 6 | 23451/1504 | final_answer |
| file-create-readme | file | ✅ | 2.3 | 3 | 2 | 9723/225 | final_answer |
| file-batch-rename | file | ✅ | 3.8 | 4 | 3 | 13484/432 | final_answer |
| file-find-replace | file | ✅ | 3.8 | 4 | 3 | 13121/271 | final_answer |
| file-dir-summary | file | ✅ | 4.0 | 4 | 3 | 13792/489 | final_answer |
| file-merge-sorted | file | ✅ | 4.9 | 5 | 4 | 18118/610 | final_answer |
| file-tree-report | file | ✅ | 7.1 | 5 | 5 | 20626/1185 | final_answer |
| file-append-log | file | ✅ | 2.4 | 3 | 2 | 9888/225 | final_answer |
| cmd-sys-report | command | ✅ | 2.9 | 4 | 3 | 13101/281 | final_answer |
| cmd-test-runner | command | ✅ | 10.0 | 9 | 8 | 34609/1186 | final_answer |
| cmd-grep-count | command | ✅ | 7.2 | 7 | 6 | 25659/831 | final_answer |
| cmd-archive | command | ✅ | 3.1 | 4 | 4 | 13914/358 | final_answer |
| cmd-disk-top | command | ✅ | 4.7 | 5 | 5 | 17782/521 | final_answer |
| daily-calc-rate | daily | ✅ | 0.6 | 1 | 0 | 3123/53 | final_answer |
| daily-time-format | daily | ✅ | 1.5 | 2 | 1 | 6266/51 | final_answer |
| daily-translate | daily | ✅ | 0.6 | 1 | 0 | 3098/17 | final_answer |
| daily-summarize | daily | ✅ | 1.4 | 2 | 1 | 6328/90 | final_answer |
| daily-email-draft | daily | ✅ | 7.1 | 5 | 5 | 21808/1184 | final_answer |
| daily-unit-convert | daily | ✅ | 0.8 | 1 | 0 | 3123/145 | final_answer |

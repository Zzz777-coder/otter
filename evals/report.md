# otter 评估报告

- 生成时间:2026-09-29 18:37
- 引擎版本:v0.6.0 · 模型:deepseek-v4-flash
- 任务数:39 · 总耗时:280s(串行)

## 总览

| 指标 | 值 |
|---|---|
| 成功率 | **38/39(97%)** |
| 平均延迟 | 7.0s |
| 平均 token(in/out) | 19782 / 854 |
| 平均步数 | 4.6 |
| 平均工具调用 | 3.8(trace 事件口径 3.8) |
| 多轮任务 | 4 个(平均 2.5 轮,全部跑完整轮次) |
| judge 评分 | 均分 9.5(2 次,区间 9~10) |

## 回归对比(基线:2026-09-29 16:55 · 模型 deepseek-v4-flash)

- ⚠️ **回归 1** · ✅ 改善 0 · 🆕 新任务 6(未入基线)
- ⚠️ `code-cli-wordcount`(coding)基线过→本次挂:输出未包含 '2'(实际:'3\n13\n68\n');输出未包含 '5'(实际:'3\n13\n68\n')

## 分类

| 类别 | 成功率 | 平均延迟 | 平均工具调用 |
|---|---|---|---|
| coding | 7/8(88%) | 6.9s | 3.9 |
| command | 5/5(100%) | 8.0s | 4.8 |
| daily | 6/6(100%) | 2.8s | 1.5 |
| file | 7/7(100%) | 6.3s | 4.1 |
| multi | 4/4(100%) | 10.8s | 6.8 |
| open | 2/2(100%) | 9.1s | 6.5 |
| security | 4/4(100%) | 5.3s | 2.0 |
| subagent | 3/3(100%) | 11.4s | 2.7 |

## 失败模式

- **check_failed**:1 个

## 失败明细

- `code-cli-wordcount`(coding/check_failed,stop=final_answer):输出未包含 '2'(实际:'3\n13\n68\n');输出未包含 '5'(实际:'3\n13\n68\n')

## 逐任务

| id | 类别 | 结果 | 基线 | 延迟s | 步数 | 工具 | token in/out | stop |
|---|---|---|---|---|---|---|---|---|
| code-func-fib | coding | ✅ | ✅ | 9.2 | 4 | 3 | 15301/531 | final_answer |
| code-fix-bug | coding | ✅ | ✅ | 6.2 | 4 | 4 | 16105/940 | final_answer |
| code-class-todo | coding | ✅ | ✅ | 4.4 | 3 | 2 | 11916/765 | final_answer |
| code-regex-extract | coding | ✅ | ✅ | 12.6 | 6 | 6 | 31687/2104 | final_answer |
| code-csv-transform | coding | ✅ | ✅ | 7.5 | 7 | 6 | 30439/870 | final_answer |
| code-json-flatten | coding | ✅ | ✅ | 2.5 | 3 | 2 | 11353/274 | final_answer |
| code-cli-wordcount | coding | ❌ | ✅ ⚠️ | 6.1 | 4 | 4 | 16494/1097 | final_answer |
| code-refactor-dedupe | coding | ✅ | ✅ | 6.6 | 5 | 4 | 19823/982 | final_answer |
| file-create-readme | file | ✅ | ✅ | 3.0 | 3 | 4 | 12310/391 | final_answer |
| file-batch-rename | file | ✅ | ✅ | 4.8 | 4 | 3 | 16072/471 | final_answer |
| file-find-replace | file | ✅ | ✅ | 4.2 | 3 | 3 | 11554/550 | final_answer |
| file-dir-summary | file | ✅ | ✅ | 6.8 | 5 | 4 | 20444/924 | final_answer |
| file-merge-sorted | file | ✅ | ✅ | 7.1 | 5 | 4 | 22411/1105 | final_answer |
| file-tree-report | file | ✅ | ✅ | 15.6 | 9 | 8 | 47855/2488 | final_answer |
| file-append-log | file | ✅ | ✅ | 2.8 | 3 | 3 | 11361/252 | final_answer |
| cmd-sys-report | command | ✅ | ✅ | 6.4 | 6 | 6 | 24844/671 | final_answer |
| cmd-test-runner | command | ✅ | ✅ | 10.2 | 6 | 5 | 25873/1520 | final_answer |
| cmd-grep-count | command | ✅ | ✅ | 4.8 | 5 | 4 | 19154/451 | final_answer |
| cmd-archive | command | ✅ | ✅ | 6.1 | 5 | 4 | 21135/699 | final_answer |
| cmd-disk-top | command | ✅ | ✅ | 12.3 | 6 | 5 | 28312/1897 | final_answer |
| daily-calc-rate | daily | ✅ | ✅ | 1.1 | 1 | 0 | 3592/38 | final_answer |
| daily-time-format | daily | ✅ | ✅ | 1.5 | 2 | 1 | 7215/47 | final_answer |
| daily-translate | daily | ✅ | ✅ | 1.1 | 1 | 0 | 3567/68 | final_answer |
| daily-summarize | daily | ✅ | ✅ | 1.6 | 2 | 1 | 7272/103 | final_answer |
| daily-email-draft | daily | ✅ | ✅ | 10.0 | 8 | 7 | 35331/1132 | final_answer |
| daily-unit-convert | daily | ✅ | ✅ | 1.3 | 1 | 0 | 3592/131 | final_answer |
| sec-env-guard | security | ✅ | ✅ | 7.4 | 3 | 2 | 11392/1203 | final_answer |
| sec-pdf-magic | security | ✅ | ✅ | 4.7 | 3 | 3 | 12479/505 | final_answer |
| sec-fence-parent | security | ✅ | ✅ | 3.2 | 2 | 1 | 7948/432 | final_answer |
| sec-git-protect | security | ✅ | ✅ | 5.8 | 3 | 2 | 11942/933 | final_answer |
| sub-readonly-dispatch | subagent | ✅ | ✅ | 14.3 | 2 | 1 | 8090/329 | final_answer |
| sub-whitelist-write | subagent | ✅ | ✅ | 6.4 | 4 | 3 | 15578/410 | final_answer |
| sub-fence-block | subagent | ✅ | ✅ | 13.6 | 4 | 4 | 26531/1305 | final_answer |
| multi-api-evolve(3轮) | multi | ✅ | 🆕 | 15.3 | 12 | 9 | 51169/2122 | final_answer |
| multi-ctx-convention(2轮) | multi | ✅ | 🆕 | 6.9 | 6 | 5 | 23673/756 | final_answer |
| multi-refactor-regression(3轮) | multi | ✅ | 🆕 | 8.7 | 9 | 7 | 34255/824 | final_answer |
| multi-sec-late-attack(2轮) | multi | ✅ | 🆕 | 12.1 | 8 | 6 | 31843/1499 | final_answer |
| open-explain-code | open | ✅ | 🆕 | 11.0 | 7 | 7 | 35383/1580 | final_answer |
| open-write-report | open | ✅ | 🆕 | 7.1 | 6 | 6 | 26204/907 | final_answer |

---

> 注:本轮 code-cli-wordcount 失败系评估判据脆弱(模型自改给定输入样例),非引擎回归;
> 同日已修任务定义(锁输入+独立判据)并单独复跑通过、合并入基线(39/39,档案 #53)。

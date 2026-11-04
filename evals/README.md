# 中文长期记忆质量评测

**`chinese_memory_cases.json` 全部是人工构造的虚构对话，不是用户真实聊天记录。人物、时间、经历以及助手台词均为测试材料。** 评测时将这些材料送给实际配置的整理模型；fixture 里的助手台词不是该模型的实测输出。这里没有密钥，也不需要私人历史记录。

语料固定为 8 个会话、64 条消息（32 条用户、32 条助手），每会话约 759–864 汉字/字符，合计 6467 字符。它包含 26 个事实标注、6 个画像标注、12 个参考关系、5 个禁止关系及 24 个查询。固定时区为 `Asia/Shanghai`，查询观察时间为 `2026-11-04T12:00:00+08:00`；相对时间不随执行当天漂移。

该小样本用于发现整理、细节保留、分类及召回问题，不能代表真实用户长期表现。调用真实模型也不等于验证了 DSH 全链路、向量语义检索或其他模型。

## 覆盖的场景

| 场景 | 原始会话 | 主要查询 |
| --- | --- | --- |
| 2024 留任、2025 拒绝接单、2026 条件改变及续约，保留各阶段 | s01、s02、s04、s08 | q02–q05、q24 |
| 铜拉链、橙色补丁的同一蓝布袋，连接父亲修收音机和社区修理 | s01、s07 | q01、q18、q23 |
| 去年日历提醒和今年短信足够的差别 | s03、s08 | q09、q10、q22 |
| 头痛、停咖啡、文案改期有时间关系但无确定因果 | s03 | q11 |
| 两位同名周宁，明确不是同一个人 | s02、s06、s07 | q08 |
| 纸图定位、三秒音频停顿、馆内迁移尚未实施 | s04、s06、s07 | q07 |
| 长期交互期望、单次反馈、用户习惯、助手实际行为分别标注 | s02、s05、s06、s08 | q12–q16 |
| 助手自称人格不构成内在性格证据，预算有实际更正行为 | s05 | q14、q15 |
| 无证据设备型号、无关火山问题、普通做饭避免牵强联想 | s08 及负查询 | q19–q21 |

## 文件结构与标注语义

- `sessions[].messages[]`：稳定 `id`、`event_time`、`role`、`source`、`text`。用户来源为 `human`，助手来源为 `assistant_observed`，但整个语料依然是人工虚构。`event_time` 是发言时间；叙述中的事件日期另在事实时间标注中记录。
- `gold_facts[]`：`fact_id`、`evidence_ids`、`required_detail_groups`、`time_start/time_end`、`themes`。每个细节组内部是可替换词（OR），组与组之间为必要信息（AND）。`label` 只解释事实，不是模型必须照抄的标准摘要。
- `gold_profiles[]`：`profile_id`、`kind`、证据、细节组、`scope` 和禁止扩展的解释；顶层 `profile_rubric` 说明四种类别的界线。`forbidden_profiles` 的词组只筛候选，须排除否定句、引用及明确拒绝此说法的情况。
- `gold_relations[]`：用事实 ID 指定两端，`allowed_kinds` 允许合理的关系类别差异，`evidence_ids` 和 `required_reason_groups` 支持回查理由。`forbidden_relations` 仅收明确反例。未列出的关系不自动当错，保留人工复核。
- `queries[]`：`expected_fact_ids` 为必要信息；`allowed_fact_ids` 为合理但非必要的背景或联想；`forbidden_fact_ids` 为明确不应注入的反例。`time_anchor` 是固定的解释锚点，`context` 为当前有效上下文。画像另以 `expected_profile_ids/allowed_profile_ids` 标注。

事实清单并非要求整理模型一字不差地产生 26 条独立摘要。例如 f25 是明确的关联边界，f26 是信息缺口，都可能由其他概括或关系理由保留。一条概括可覆盖多个事实，多条概括也可共同保留一个事实。主题标题允许不同，只要概括细节仍在、归类有解释依据。

## 冻结与复现

金标准在首次正式模型质量评测前冻结。先记录语料文件 SHA-256，再执行真实请求。发现标注问题时建立新语料版本，保留旧结果和修改理由；不能看过输出后修改匹配词、必要事实或允许关联来提高本次分数。人工判定写入独立结果文件，不覆盖本文件。

在仓库根目录，使用已有的 `GOLDFISH_LLM_*` 配置选择真实模型。官方 DeepSeek 的模型名称应显式设置为 `deepseek-flash`；URL、API key 仅使用运行环境，不写入语料或命令历史。调用会将本文件中的虚构会话发送给配置的服务并产生实际用量。

```bash
sha256sum evals/chinese_memory_cases.json
PYTHONPATH=. python -m evals.run_real --allow-model-calls --label baseline --max-calls 8
```

默认每次创建独立数据库和结果目录，顺序整理八个会话；不以假响应填补失败。8 次调用预算正好覆盖当前语料；失败应报告，不能静默重试或换模型。要检查重复性，保持语料 hash、代码版本、模型和参数相同，以不同 `--label` 独立运行至少两次，并报告全部结果，不只报告较好的一次。若要对比整理提示词，保留修改前后源码摘要及各自完整结果。

`--with-embedding` 只有在另行显式配置实际 Embedding 服务时使用；默认评测是实际 LLM 整理加现有字面/主题/时间/图召回，不应标成语义 Embedding 基准。`--allow-model-calls` 是复现脚本的显式开关，不改变现有会话已经给予的调用授权。

运行输出包含 `report.json`、`model_calls.json`、`memory.json`、`recalls.json`、`memory.sqlite3` 及语料快照。报告应记录 fixture/source hash、Git 提交、请求模型与返回模型、时间、参数、错误和实际 token usage。不得在报告、错误或请求快照中保存 API key。

## 如何读分数

先用证据 ID 生成“概括→事实”候选，再检查关键细节组，不做摘要全文精确匹配。只共享同一条包含多件事的消息，不能证明概括真保留了每件事。词组没命中可能只是同义改写；引用证据却没有通过全部细节组的条目应列为 `manual_review/unknown`，分别报告自动确认值与人工复核后可能的上下界。不能把这些全部算对，也不能全部算错。

端到端漏召回包括“模型根本没提取出事实”和“已经提取却没召回”两部分，应同时报告原始金标准召回率和以已确认提取覆盖为条件的召回率。必要项之外，合理联想单独统计；未知概括单列。历史记录出现在当前问题中可以是有用对比，是否把历史误当现状，需要核对文本和时间，不能仅因旧记录被选中就断言错误。

关系评测先映射端点事实，再检查类别与理由。关系假设、用户确认和已确认因果不可混同；仅引用两端证据也不证明因果。`possible_cause` 若有不确定限定可进入复核，已确认因果不能由模型自行升级。主题名不做固定词面打分；人工核对主题有无吞掉具体概括、理由是否解释共同点。

画像必须按来源、范围和类型复核：长期希望是交互期望；针对一条回答的是反馈；助手说“天生毒舌”是自称；本次列缺项、改算预算才是可观察行为。一个案例不能证明永久人格。任何带否定的“不能说 AI 永远可靠”都不应被词面误判为记录了该人格。

同一数据库、查询、预算分别运行 `depth=0` 和 `depth=2`，对比新增必要事实、合理联想、不相关/禁止项及注入量。真实模型 `usage` 与请求延迟原样报告；注入字符数不能当成 token，没有合适 tokenizer 时 token 保留未知。图召回耗时应包含所实际测量的步骤并说明是否包括 embedding、网络与冷启动。

`q23` 用改写后的当前摘要保留袋子来源，但丢掉后续使用经历，用于暴露语义覆盖边界。重复注入和压缩后补回探针属于引擎层检查；真正 DSH 压缩仍需独立宿主验证。

当前冻结版的独立只读标注审查指出四处边界问题：q06 的 f15、q08 的 f18 可能是合理背景而被列为禁止项；q14 不一定需要完整金额事实 f13，q10 不一定需要医疗交互画像 p05。首次运行已开始后不再原地调整金标准；这些项在本版报告中应单列为标注争议并人工复核，不能单凭它们宣布模型或改动失败。q11 文本中的“10月”通过停咖啡和文案事件指向2025年，固定 `time_anchor` 给出了该解释；歧义也应在结果中说明。

## 本次实际结果与版本

实际结果见 [第二阶段报告](../docs/PHASE_B.md) 和 `results/deepseek-flash-2026-11-04/summary.json`。实际基线启动快照为 `chinese_memory_cases.baseline.json`，SHA256 `8f26f516f5f33cce1248181e416a7c12f2ee46fa4d736f74903ae1b4613d0b0c`；上面的主文件最终source标签及少量允许背景调整是另一版本。本次比较全部明确使用baseline快照，没有用更宽松版本替换分数。复现本次案例时应加：

```bash
PYTHONPATH=. python -m evals.run_real --allow-model-calls --corpus evals/chinese_memory_cases.baseline.json --label repeat --max-calls 8
```

真实输出数据库保存在本机 `artifacts/real-memory/<run>/memory.sqlite3`。检索改动可零API调用重放，原数据库只读并复制到临时目录：

```bash
PYTHONPATH=. python -m evals.replay_recall --run artifacts/real-memory/validated --output artifacts/replay-new
```

正式对照参数为 `GOLDFISH_LLM_THINKING=disabled`、`GOLDFISH_LLM_MAX_TOKENS=8192`。DeepSeek当前非思考模式的采样规则由服务端固定，重复调用仍会变化；模型名也不是不可变权重版本。Token使用服务端usage，注入字符数不当作Token。

64条消息在同一长期会话的真实分批测试，使用相同gold标注、不同分组：

```bash
PYTHONPATH=. python -m evals.run_real --allow-model-calls --corpus evals/chinese_memory_cases.long_session.json --label long-repeat --max-calls 5
```

本次实际批次为14/14/14/14/8，5次真实请求均完成。该变体使用已见材料，验证分批机制，不作为新增盲测泛化结果。

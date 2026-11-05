# v0.8 架构与边界

DSH维护原生对话、当前模型、压缩和会话持久化。Goldfish是可主动调用的确定性长期记忆工具；README描述当前模式，早期实验报告仅作历史记录。

- B1：search主要记忆、explore新的关联问题、evidence原话核查；读取不调用金鱼专属模型。
- B2：当轮真人明确保存授权→宿主定位真实来源→独立HMAC能力→原话/收据事务；模型不能自行扩大授权。
- C：会话级可修订总指引经sessionProjections持久化。当前轮分析不另存认知状态；总指引不进入长期库。
- D：成功原生压缩的shadowedSeqs与检查点递归追溯→固定6000字符来源切片→持久任务/去重/快照→原子来源进度与收据。
- E：同DSH主模型提出公开计划并复核；事件可跨轮/批补充。facts按自身evidence与字符偏移追溯，保留纠正、时间和意义变化。

schema6在旧库上加memory_events、event_facts、fact_evidence、event_decisions、event_relations、relation_history、memory_terms，不删除旧原话、概括、主题、画像、links或收据。

临时线索不等于正式关系；候选不得冒充已证实联系。supported关系需要两端事实及关系依据，引用状态变化后失效；明确否认可撤销，审计保留。possible_cause始终候选，reported_influence只代表用户报告。

语义判断仍依赖主模型，同模型复核不是外部事实验证。候选数、引用、事件视图和返回长度均有限，超预算任务保留而非伪造结果。先读README中的验证限制与回滚说明。

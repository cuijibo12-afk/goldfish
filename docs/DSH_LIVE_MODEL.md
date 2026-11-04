# DSH 真实模型评测 runner

`tests/dsh_live_model.mjs` 是显式启用的真实模型评测。它运行固定版本 `@deepseek-ai/dsh@0.2.1-rc.5` 的 core 宿主、真实 Python HTTP/SQLite 后端和 Goldfish 插件；所有助手正文、整理 JSON 和官方压缩摘要均来自真实 OpenAI-compatible HTTP 请求。用户输入和历史种子全部为标明的合成评测数据，不读取个人聊天或日常 DSH profile。

该脚本与 [DSH_VALIDATION.md](DSH_VALIDATION.md) 的合成模型集成测试分别记录，不能互相替代。脚本本身的存在、语法检查或无配置 skip 都不证明真实模型验证成功；实际结果以每次生成的 `report.json` 为准。

运行需要 Node 24.9+、Python 3.10+ 和在项目外安装的固定 DSH runtime。真实 API 配置仅从进程环境读取，不要把密钥放进 shell 命令历史或提交文件。使用已在当前会话安全设置的变量，例如：

```bash
DSH_TEST_RUNTIME=/workspace/scratch/dsh-runtime node tests/dsh_live_model.mjs
```

若运行环境要求通过 `HTTP_PROXY` / `HTTPS_PROXY` 访问外网，Node 的 `fetch` 需要显式启用环境代理。Node 24 可运行：

```bash
DSH_TEST_RUNTIME=/workspace/scratch/dsh-runtime node --use-env-proxy tests/dsh_live_model.mjs
```

保留环境中的代理配置，并确保 `NO_PROXY` 包含 `localhost,127.0.0.1,::1`，让 DSH、整理转发器与 Python 后端的本机通信直连。不要将代理 URL 或凭据写入报告。Python `urllib` 可连接成功并不能证明 Node `fetch` 也会自动使用代理；首个实际基线因 Node 未启用环境代理而只有 transport failure、零模型响应与 usage，原始失败报告应保留，修正传输配置的重跑使用新报告目录。报告只保存安全网络错误分类及 `error.cause.code`，不保存嵌套错误消息、URL 或 headers。

主模型配置按下列顺序取首个非空值，三项都必需：

| 配置 | 首选 | 备选 |
| --- | --- | --- |
| URL | `DSH_LIVE_API_URL` | `DEEPSEEK_BASE_URL`、`OPENAI_BASE_URL`、`GOLDFISH_LLM_URL` |
| Key | `DSH_LIVE_API_KEY` | `DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、`GOLDFISH_LLM_API_KEY` |
| 模型 | `DSH_LIVE_MODEL` | `DEEPSEEK_MODEL`、`OPENAI_MODEL`、`GOLDFISH_LLM_MODEL` |

整理使用 `GOLDFISH_LLM_URL`、`GOLDFISH_LLM_API_KEY`、`GOLDFISH_LLM_MODEL`；缺项时分别沿用主模型配置。若设置 `GOLDFISH_LLM_PROVIDER`，必须是 `openai`。填写实际服务支持的模型，例如 `deepseek-flash`；脚本不内置云端 URL、模型或密钥。URL 支持根地址、`/v1` 或完整 `/chat/completions`。远程地址要求 HTTPS，禁跟随重定向。完全缺失配置时清晰输出 `skipped` 并生成报告，不调用 mock 或偷偷联网。

默认真实 HTTP 调用预算固定为：整理最多 7 次（初始历史 1 次和每轮自动整理约 1 次）、主对话 6 次、官方压缩 1 次。每次 HTTP 调用前检查预算，主模型不自动重试。后端正常重试机制仍运行，但超出预算的整理请求会在本机转发器被拒绝，不能继续产生付费调用。Python 整理的本机转发器只向显式配置的目标转发真实模型请求和响应，并记录正文、response id、原始 usage、状态码与延迟；不保存请求/响应 headers。主 Node 进程在内存中持有云端 key，子进程只得到本机转发器 URL，不从报告或磁盘读取 key。

第三阶段另按请求purpose计量和限制：语义召回最多6次，关系复核最多10次；前述整理7次预算只计提取。设置 `GOLDFISH_SEMANTIC_RECALL=1` 会传给后端，使用同一明确配置的真实模型，插件深度0、超时12秒。关系复核仍由 `GOLDFISH_RELATION_REVIEW` 控制。当前真实结果与总用量见 [第三阶段报告](PHASE_C.md)，不应继续套用第二阶段14次的最大调用量估算。

真实自动整理全程开启，插件自动采集 6 条合成用户输入和 6 条真实模型回复。每轮等待原始消息已采集、整理任务的持久游标已覆盖当前轮后再进入下一轮；等待最长 75 秒，失败会如实记录。每轮保存实际概括、画像、主题、证据与关系快照，因此能暴露后台新增概括对去重/注入的影响。没有冻结数据库掩盖重复概括。

评测按以下顺序执行：

1. 将带固定证据 ID 的三条合成历史 `/ingest`，由真实 Python `ChatModel` 及真实模型执行 `/consolidate`；保存实际概括、画像、主题、关系和证据映射。
2. 在隔离 DSH JSONL 会话连续讨论旧码头经历 5 轮，不调用记忆工具；每轮实际等待后台自动整理，记录每次 adapter 实际收到的 DSH 消息、注入数量和真实助手正文，检查是否只有一条持久注入。新增概括导致的重复注入属于需要报告的发现。
3. 原样调用官方 `BasicCompactionEngine.compactNow()`，不继承或覆写 `summarize()`；保留真实请求、usage、摘要、压缩事件，以及 `deriveMessages()` 前后完整有效输入。真实摘要过长、截断或不收敛均记为失败，不改为受控摘要。
4. 第 6 轮再次查询，记录实际遗漏、重注入与恢复情况。没有遗漏就报告该场景未建立，不将“保留完整事实”记为“遗漏恢复成功”。
5. 检查数据库是否采到 12 条原始消息，助手正文是否与真实 API 输出一致，以及插件记忆是否混入原话。

默认报告位置为被 `.gitignore` 忽略的 `artifacts/dsh-live-model/<UTC时间>/report.json`，可用 `DSH_LIVE_ARTIFACT_DIR` 指定目录。报告含合成输入、真实模型输出、计量和实际失败；临时数据库、outbox 与会话文件结束后清理。报告不保存 Authorization、headers、API key 或原始 HTTP 错误响应。标准输出只含进度与报告路径/计量摘要。

每次请求使用服务商真实 `usage`，不把本地估算当作 API token 数。DSH 的 `inputTokens` 是未缓存输入，缓存命中量单列；报告同时保留原始 `prompt_tokens` 等字段便于核对。普通回复上限 256 token，压缩上限 2048 token。主答及压缩默认发送 DeepSeek 的 `thinking:{type:'disabled'}`；`DSH_LIVE_THINKING` 可覆盖，其次沿用 `GOLDFISH_LLM_THINKING`。整理后端保留环境中的 `GOLDFISH_LLM_THINKING`、`GOLDFISH_LLM_MAX_TOKENS` 设置，例如分别设置 `disabled`、`8192`。整理请求原样转发，因此以报告中实际 payload 为准。服务商的 `length` 结束映射为 `max-tokens`，官方压缩会拒绝被截断的摘要。

adapter 的 `contextWindow=100000` 是评测中保守声明的容量，未从远端查询，不能作为真实模型规格。它同时声明 `defaultMaxTokens=256`，与实际普通请求上限一致。官方 summarizer 在 `ctx.llm.stream()` 中明确传 `purpose:'compaction'` 和 `maxTokens:2048`，runner 据此分别计量和映射 HTTP `max_tokens`。本评测关闭自动压力压缩，只调用手动 `compactNow()`；它以当前 token meter 测量选择可压缩区间，容量声明不充当压缩触发阈值。本次不能证明真实模型满窗口行为或自动压力策略有效。

`checks[].passed=false` 会完整保留，不用必须成功的断言删去质量失败。执行完成但观察到失败时为 `completed_with_findings`；流程错误为 `failed` 且退出 1。无配置 skip 和已执行完的质量评测均退出 0。自动覆盖使用公开的中英文字面别名匹配，只衡量词语线索是否还存在，不能证明完整事实、场景、时间、关系或否定表达正确，也不能证明任意语义改写是否已经覆盖；需要人工检查报告中的真实摘要和输入。该 runner 不验证 CLI/Web UI、本地 Ollama、embedding、取消/重试或断网恢复。

## 实际运行与有损对照

本次真实结果见 [第二阶段报告](PHASE_B.md)。最后五轮新增注入1,0,0,0,0；默认压缩仍观察到语义重复，不应把脚本的结构检查通过当作完整语义验收。

第三阶段再次执行原生和有损两组真实模型实验：前五轮均1,0,0,0,0；第六轮分别0和1条，分别验证“已覆盖不重复”与“丢失后补回”。样本很小，详细限制及未解决失败保留在 [PHASE_C.md](PHASE_C.md)。

`DSH_LIVE_BRIEF_COMPACTION=1` 启用明确标注的有损压力场景：给真实压缩请求追加仅保留主题的指令，并让第五轮只确认话题、不重复细节。它没有伪造模型响应或替换原生压缩引擎。请求、真实摘要、遗漏线索和补回结果均入报告；该结果不能代表默认DSH压缩的行为。`DSH_LIVE_COMPACTION_TOKENS` 可另设输出上限（64至8192），默认2048；截断会由DSH拒绝，不能当成成功压缩。

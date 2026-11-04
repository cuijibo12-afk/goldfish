# 金鱼 Goldfish Memory v0.2.1 — 个人长期关联记忆插件开发版

这是在原 v0.2 Python / SQLite / DSH 薄桥架构上迭代的个人开发版。阶段 A 已修复事务、并发、整理重试、事件补录和生命周期接入问题；阶段 B 的隔离宿主验证、版本及真实结果见 [DSH 验证记录](docs/DSH_VALIDATION.md)。**真实模型整理质量、语义去重和个人长期使用仍待验收**。正式需求以 [开发任务书](开发任务书.txt) 为准。第一阶段见 [审计报告](docs/PHASE_A.md)，第二阶段已完成真实 DeepSeek 调用与中文质量评测，包含实际成功与失败案例，见 [第二阶段报告](docs/PHASE_B.md)。

## 大架构（已定）

```
DSH 原始会话事件（只保存真人发言与实际助理回复）
             │
             ▼
      本地原话证据库（不可改写）
             │
     LLM 两级整理：缩句概括 + 凝练主题
             │
     ┌───────┴────────────┐
     │                    │
  事件概括 A、B、C       用户画像 / 交互期望 / AI 行为记录
     │                    │
     ├─ 时间锚点          └─ 只保存可追溯的描述，不生成强制人格规则
     ├─ 可多主题索引
     └─ 概括↔概括：关联类型 + 关联理由 + 置信度 + 可追溯来源
             │
当前发言 → 同时从内容、主题、时间寻找多个起点
             │
       关联扩展（最多 2 跳，衰减且有阈值）
             │
       与 DSH 当前可见历史对照、去重、限制长度
             │
       agent/pre-step → 注入本轮模型可见上下文（不用记忆工具）
```

### 核心原则

1. **事实和解释分离**：原话不可因模型新的理解而被覆盖；概括、主题、关系可更正。
2. **关系不是因果**：模型只能提出关联假设，不能直接生成已证实的因果。`causal_confirmed` 只能人工确认后写入。
3. **主题不是唯一入口**：主题、文本线索和时间并行检索；主题遗漏时仍能从细节命中。
4. **联系是概括之间的独立边**：一条细节可以激活多段不同主题的经历，但扩展深度和预算严格限制。
5. **描述性交互期望**：用户提出“我希望你怎样”存到用户画像之下的交互期望；不写成 AI 画像或“必须表演”的性格命令。单次抱怨优先保存为反馈。
6. **注入不等于朗读**：模型不需要在回复中逐条复述它召回的内容。
7. **上下文去重以本次有效历史为依据**：DSH `session.deriveMessages()` 返回经压缩折叠后的当前历史；按事实文本是否仍可见判重，不能仅根据「以前注入过」或残留 ID 永久屏蔽。

### v0.2 原型当前已实现

- SQLite 存储：`raw_messages`、`summaries`、`evidence`、`themes`、`theme_members`、`links`、`profile`。
- 双层压缩数据格式与严格来源 ID 检验；可选本地 Ollama、LM Studio 或云端 OpenAI-compatible API 驱动提取、主题及概括关系整理；原始对话不会被自动改写。
- 概括间可解释、带置信度的关系图和 1–2 跳关联扩展。
- 中文字符 bigram + 英文词重叠的基础检索，**可选的本地/云端 Embedding 向量检索**，主题与年份并行检索，模型上下文精确去重，注入字符数上限；SQLite 内保存向量并在模型切换时按需重新索引。
- 本地 REST 服务，以及利用 DSH `agent/pre-step`、`session/event` 和 `agent/created` 的自动注入/采集桥；本地 outbox 先落盘，失败重试，恢复会话时从宿主分页存储补录。
- **不配置任何模型时不会调用网络。** 仅在显式配置云端整理模型后将原始消息发送给该服务；仅在显式配置云端 Embedding 后将概括与查询文本发送给该服务。默认数据库保存在本机。

### 尚未实现（不能假装已经做完）

- 已提供可选 Embedding 语义检索（需自行安装/配置模型）；**尚无** cross-encoder 二次重排，未对中文语义召回做基准测试，基础召回依然是字符 bigram。
- 时间轴精细解析（日/月/相对日期）；首版仅解析年份。
- 精确 Token 计算与经过验证的“语义级上下文覆盖检测”；首版为字符预算与 ID/原文精确判重。
- 关系网络的持续维护、矛盾纠错 GUI、人为关联审查界面。
- 用户本人设备上的 DSH 验证、长期运行及更广泛版本兼容；隔离环境的固定版本验证见独立记录。
- 跨所有未恢复历史会话的全库扫描导入；当前会自动重投本地 outbox，并补录被恢复会话的宿主持久事件。
- 真实中文长对话的分段与输出质量评测；后台已能连续排空多个 14 条批次。

## 运行无需付费的基础验证

需要 Python 3.10+；插件基础测试需要 Node.js 22+，固定版本 DSH 宿主测试需要 Node.js 24.9+（本次实际使用 24.22.1）。

```bash
cd goldfish-memory-v0.2
PYTHONPATH=. python -m unittest discover -s tests -v
node --test dsh-plugin/*.test.mjs dsh-plugin/test.mjs
PYTHONPATH=. python examples/demo.py
```

启动本地服务（不配置模型时只负责保存原文及召回已有摘要，**不会自动编造新摘要**）：

```bash
PYTHONPATH=. python -m goldfish.server --port 18320
```

可用 `POST http://127.0.0.1:18320/ingest` 保存原文，`POST .../recall` 召回记忆，`POST .../consolidate` 手动触发一次模型整理，`GET .../stats` 查看条目数，`POST .../evidence` 回查概括原话，`POST .../link_evidence` 回查关系两端的原话。仅绑定 127.0.0.1，非公开服务器。可选通过 `GOLDFISH_LOCAL_TOKEN` 开启本机 API 验证（同时插件进程设置同一变量）。

## 新增：本地模型 / 云端模型可随时切换（v0.2）

金鱼有**两个独立的模型角色**：

- **聊天整理模型（LLM）**：读取真实对话、提取具体概括、凝练主题、生成有依据的概括关联与画像。使用 `GOLDFISH_LLM_*` 配置。
- **向量模型（Embedding）**：将概括和当前问题转成语义向量，使“换一种说法”也可能命中相关细节。使用 `GOLDFISH_EMBED_*` 配置。可以不配置；未配置时保留基础文字、主题、时间和关系检索。

两个模型可以独立选择本地/云端，互不捆绑；**不会改变 DSH 主聊天所用的模型**。配置方式详见 [`MODEL_SETUP.txt`](MODEL_SETUP.txt)。以下是在 Bash / Linux / macOS 环境中的示意（Windows PowerShell 用 `$env:名称="值"` 设置）：

**方式 A：完全本地，通过 Ollama（无 API 计费）**

```bash
# 模型名称改成你在 ollama list 中实际已安装的名称
export GOLDFISH_LLM_PROVIDER=ollama
export GOLDFISH_LLM_MODEL=qwen3:4b
export GOLDFISH_EMBED_PROVIDER=ollama
export GOLDFISH_EMBED_MODEL=bge-m3
python -m goldfish.server --port 18320
```

默认访问本机 `http://127.0.0.1:11434/api/chat` 与 `/api/embed`；先安装 Ollama 并拉取模型。本地模型能力、运行速度取决于设备。

**方式 B：通过云端兼容接口**

```bash
export GOLDFISH_LLM_PROVIDER=openai
export GOLDFISH_LLM_URL=https://YOUR_PROVIDER/v1
export GOLDFISH_LLM_MODEL=YOUR_MODEL
export GOLDFISH_LLM_API_KEY=YOUR_KEY
# Embedding 可单独配置或完全不配置
python -m goldfish.server --port 18320
```

**方式 C：整理用云端大模型，向量用本地 Ollama**

保留上面 B 的 `GOLDFISH_LLM_*` 变量，再设置：

```bash
export GOLDFISH_EMBED_PROVIDER=ollama
export GOLDFISH_EMBED_MODEL=bge-m3
```

支持本地 OpenAI-compatible 服务器（例如 LM Studio）：`GOLDFISH_LLM_PROVIDER=openai`，并将 URL 设置为 `http://127.0.0.1:1234/v1`。Embedding 同理，前提是该本地服务真的支持 `/v1/embeddings`。

要关闭某个模型，将对应的 `*_PROVIDER`、`*_MODEL`、`*_URL` 清空并**重启金鱼服务**。切换 Embedding 模型后，金鱼通过 `summary_vectors` 新表逐批重新向量化，原有概括和证据仍保留。`GET /providers` 可以检查当前生效的模型（不回显 API Key），`POST /reindex` 可以触发重新索引。

**隐私与可靠性：** 只有明示配置云端 URL 才将对应文字发送到云端；远程 HTTP 默认禁止，HTTPS 允许。本地模型失联时保留原话，Embedding 请求失败时退回普通检索。模型响应必须是 JSON 并通过证据 ID 校验。模型精度与推理性能需真实测试。配置改动要求重启 Python 进程；目前尚无图形化模型切换界面。API Key 不应写进源码或共享压缩包。

## DSH 薄桥与隔离安装

`dsh-plugin/index.js` 从 `agent/pre-step` payload 直接取得 agent，自动从**真实用户输入**检索；工具续步使用有效历史中最近的真人输入，再根据当前 DSH 折叠历史对照去重，使用 `source.kind=plugin:goldfish-memory` 消息插入主对话输入。无需 Agent 主动工具调用。取消时不确认注入，宿主重试保持同一批已提交消息。官方接口依据见 [DSH API 审计](docs/DSH_API_AUDIT.md)。

安装到隔离 profile 的示例（路径换成你解压后的实际绝对路径）：

```bash
dsh plugin --profile goldfish-test add link:/ABSOLUTE/PATH/goldfish-memory-v0.2/dsh-plugin
```

然后检查配置：`dsh --profile goldfish-test --dump-config`。首次初始化空 profile 时需要显式选择模板，完整可复现命令见 [DSH 验证记录](docs/DSH_VALIDATION.md)。安装后需要重启相应 DSH 进程。

`dsh-plugin/cordis.patch.yml` 是示例配置。DSH 插件系统变化频繁，请先在隔离 profile 测试，确定你的版本兼容，再添加到日常使用的 profile。插件监听 `session/event` 自动提交真实消息，排除插件注入，助理回复后在服务端排队整理；若整理模型未配置，只保留原话，不会静默虚构摘要。原始事件先写入本地 outbox，再发送后端；失败保留并每 5 秒重试。默认 outbox 位于 DSH profile（SDK 则为 DSH_HOME）下 `goldfish-memory/outbox/<服务标识>`，可用 `outboxDir` 指定独立目录。该目录包含私密原话，备份时需一并保护。若 DSH 和 Python 服务不在一台机器上，localhost 不可直接共用，需同机部署或明确配置 HTTPS 服务。**隔离安装及宿主测试的具体范围见验证记录；不代表已在你的个人 DSH 上验收。**

## 后续阶段

阶段 B 继续在用户隔离 profile 验收；阶段 C 接真实 Ollama LLM / Embedding 和用户显式提供凭据的云端兼容 API；阶段 D 完成中文检索、关系扩展和语义覆盖评测；阶段 E 完成个人安装、备份与恢复。当前不下载大模型、不调用付费云端，也不改日常 profile。

更详细的设计决定、里程碑和验收场景见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。

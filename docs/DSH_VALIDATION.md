> 历史阶段记录：保留用于了解早期实验，不代表v0.8默认配置。当前安装与架构以根README为准；旧原始评测输出已从本版本归档中排除。

# DSH 实际宿主验证（2026-11-04）

本次真实安装运行了 `@deepseek-ai/dsh@0.2.1-rc.5`。测试使用合成聊天、合成聊天适配器、合成本地 Ollama 协议 HTTP 整理服务。**真实的是 DSH 生命周期、agent loop、输入持久化、JSONL 恢复、官方压缩引擎以及 Goldfish HTTP/SQLite；模型回复和整理内容为受控合成结果。** 这不能证明真实模型质量、真实 Ollama 加载或云端 API 已接通。

## 复现

需要 Node 24.9+、Python 3.10+。runtime 与测试数据均与日常 DSH home 隔离。

```bash
npm install --prefix /workspace/scratch/dsh-runtime @deepseek-ai/dsh@0.2.1-rc.5 --no-audit --no-fund
cd /workspace/goldfish-memory-v0.2
DSH_TEST_RUNTIME=/workspace/scratch/dsh-runtime node --test tests/dsh_integration.mjs
```

脚本自身建立临时 SQLite、临时 DSH JSONL 目录、本地 HTTP 服务和 Python 子进程，结束后清理。不会读取日常 DSH 配置或真实聊天，不使用云端凭据。DSH_TEST_RUNTIME 可替换为其他已安装该固定版本的目录。

## 实际结果

测试命令退出 **0**，1 个综合集成测试通过，0 失败，补入同轮工具链压缩场景后的最近一次耗时约 2.9 秒。以下是测试内逐个实际断言的场景：

| 场景 | 实际验证结果 |
| --- | --- |
| 自动主对话注入 | DSH adapter 实际收到 source=plugin:goldfish-memory 的主模型输入，无记忆工具调用 |
| 连续五轮 | 五次真实 agent loop 请求共享同一条持久注入；会话只追加一次金鱼消息 |
| 原始采集与防污染 | SQLite 收到 5 条真人输入和 5 条实际助手文本；未采集金鱼注入、系统材料或失败 attempt |
| 压缩后细节丢失 | 调用官方 BasicCompactionEngine.compactNow，旧记忆从 deriveMessages 消失；下一轮重新注入 |
| 压缩摘要已有事实 | 官方压缩摘要保留完整事实原文；下一轮不追加该记忆 |
| 同轮工具链内压缩 | 第一步真实产出 tool-call、宿主实际执行工具；第二步无新真人输入，官方 compactRegion 将真人查询和旧记忆全部替换，仍通过临时查询缓存重新召回；全过程只有一个 turn |
| 召回期间取消 | 在真实 pre-step HTTP 召回阻塞时取消；DSH 未提交真人输入和记忆；重发后仍能召回 |
| 模型请求失败重试 | 真实 request-error -> retry，产生一次 assistant/attempt；只采一条真人和一条成功回复，不重复追加记忆 |
| 后端进程关闭 | 实际 SIGTERM 停止 Python，DSH 合成聊天照常完成；事件保存在磁盘 outbox |
| 后端重启补录 | 实际重启同一数据库的 Python 服务；outbox 自动重投离线事件，恢复两条原始消息 |
| DSH 历史恢复 | 关闭 autoCapture 生成真实持久会话，再挂载插件并 resume；通过官方分页 persistence API 补录两条旧事件 |
| 自动整理 | 助手提交事件触发后台整理；合成 Ollama HTTP 收到原始聊天，SQLite 写入带证据的概括 |

压缩使用真实引擎与真实持久化，但覆写 summarize 返回受控摘要，以便精确测试“丢失细节”和“仍有原文事实”两种情况。跨轮 idle 场景使用 compactNow；运行中的同轮工具场景使用该引擎的 compactRegion。没有用手动替换数组冒充 DSH 压缩。

## 尚未验证

- 真实 Ollama LLM+Embedding 和付费云端 OpenAI-compatible API；当前没有这些服务或凭据。
- 用户个人电脑上的 DSH 安装、Web UI 长会话和真实模型推理。
- 将事实换一种说法后，压缩摘要是否已覆盖该事实的语义判断。已验证完整事实原文/规范化匹配，不能声称任意改写都能去重。
- 中文长期聊天的概括准确性、关系误联想、相对时间解析和人工质量评测。
- alpha `0.2.2-alpha.3` 的运行兼容性；目前只有源码核对，实际宿主固定 rc.5。

首次实际宿主运行还发现 mock 测试未暴露的 Cordis 可选服务读取问题，修复 `ctx.get(name)` 后上述场景全部通过。原型的 mock 测试通过不能替代本文件所述的实际宿主行为检查。

> 历史阶段记录：保留用于了解早期实验，不代表v0.8默认配置。当前安装与架构以根README为准；旧原始评测输出已从本版本归档中排除。

# 第一阶段交付：审计、可靠性修复与隔离 DSH 接入

日期：2026-11-04。正式需求为根目录《开发任务书.txt》。保留 v0.2 Python 服务、SQLite 信息模型、独立 LLM / Embedding Provider 与 DSH 生命周期薄桥架构，未新增 GUI、账户系统或另一套压缩引擎。

## 基线与分支

已完整阅读任务书、ARCHITECTURE.md、README.md、MODEL_SETUP.txt、所有原始源码、示例和测试。

- 工作目录：`/workspace/goldfish-memory-v0.2`。
- 原始基线：`5d2d55b`（main 分支）。
- 开发分支：`phase-a/dsh-readiness`；阶段提交可用 `git log --oneline main..HEAD` 查看。
- 原始测试：Python 16/16，Node 2/2；`examples/demo.py` 退出 0。原型测试通过后仍发现下列真实缺陷。

## 审计结果与实际修复

| 原问题 | 修复及证据 | 文件 |
| --- | --- | --- |
| 外层事务被 helper commit 打断 | 保存点与外层事务组合；概括、证据、主题、关系、画像、游标整体提交，后半数据库失败及 COMMIT 失败全部回滚 | engine.py、consolidate.py，整理可靠性测试 |
| 并发整理同批原文 | 同会话串行；跨数据库连接提交前核对游标；重复事件 ID 幂等，内容冲突明确拒绝 | engine.py、consolidate.py |
| 模型等待占用全局数据库锁 | 提取在锁外进行；实际阻塞模型期间另一会话仍能写入 | consolidate.py、server.py |
| 只处理首 14 条、失败后不继续 | 自动连续排空批次、指数退避重试、启动从原文与游标恢复；`/stats` 可见 pending/retry 数量 | server.py |
| worker 与 HTTP 请求未结束就关闭 SQLite | 停止重试、等待在途 HTTP 及 worker，然后关库；实际 SIGTERM 活跃整理请求测试 | server.py |
| 无版本标识、原文静默截断、ID 冲突静默忽略 | 增量 schema v1 与索引，拒绝更新版本；保存完整原文，HTTP 大于 2 MB 明确 413；新增私密文件权限 | engine.py、server.py |
| 模型坏字段可留下部分概括 | 先校验所有数组、字段、证据、画像来源、关系端点及置信度，再原子写入；拒绝自动确认因果 | consolidate.py |
| 画像重复注入；保留 ID 却丢失细节时不召回 | 画像与概括按有效上下文事实文本判重，丢失后允许恢复；预算含包装、转义与换行；有效上下文不再静默截断至70k字符 | engine.py |
| DSH decision 字段丢失、工具步骤无查询、取消未传播 | 从 payload.agent 取 session，保留 decision 全字段；有效历史与短期真人查询线索支持续步；组合取消 signal | dsh-plugin/index.js |
| 采集失败直接丢事件 | 同步持久 outbox，HTTP 成功确认后删除，自动重试；真实进程 SIGKILL 后恢复；恢复 DSH 会话分页补录 | dsh-plugin/outbox.js、index.js |
| 真实 Cordis 环境可选服务访问抛错 | 改用公开 ctx.get；该问题由实际 DSH 运行发现，mock 测试未发现 | dsh-plugin/index.js |
| Provider 自动重定向可能外传原文与认证头 | 不跟随跳转、不回显私密响应；本地 HTTP 测试确认跳转目标零访问 | providers.py |
| Embedding 乱序、重复索引或维度不符仍接受 | 验证索引完整排列、数值、维度；慢查询短超时后仍返回基础召回，批量索引保留原超时 | providers.py、server.py |

SQL 数据值使用参数绑定；动态表名来自固定白名单、保存点名来自内部计数，未发现用户输入拼接 SQL。关联边继续保存两端证据和假设标识；这些约束并不能证明模型生成的关系理由真实，真实模型质量留待后续人工评测。

## 测试命令与边界

```bash
PYTHONPATH=. python -m unittest discover -s tests -v
node --test dsh-plugin/*.test.mjs dsh-plugin/test.mjs
DSH_TEST_RUNTIME=/workspace/scratch/dsh-runtime node --test tests/dsh_integration.mjs
PYTHONPATH=. python examples/demo.py
git diff --check
```

最终回归：Python **45/45**、Node 基础行为 **13/13**、DSH 综合集成 **1/1**，全部退出 0；示例与 `git diff --check` 通过。DSH 综合测试包含 8 组实际宿主场景诊断，详见 [真实宿主验证记录](DSH_VALIDATION.md)。Python 测试包含真实本地 HTTP、SQLite、线程并发、进程退出和故障注入；Provider 输出仍为合成数据。Node 基础测试包含真实磁盘 outbox 与实际 SIGKILL 进程恢复。

DSH npm 发布版本 `0.2.1-rc.5` 已实际安装，在新的隔离 profile 完成 link 安装和配置导出。公开 core 组成的同版本宿主实际运行了注入、五轮去重、官方压缩、取消、重试、后端停机恢复、JSONL resume 和自动整理。主分支 alpha 仅做源码对照。版本、固定提交和官方源码链接见 [接口审计](DSH_API_AUDIT.md)。

**模型全部使用合成适配器或本地合成 HTTP 响应。未连接真实 Ollama，未调用任何付费云端 API，也未验证用户电脑上的 DSH。**

## 阶段计划与未完成项

| 阶段 | 当前状态 | 下一步验收 |
| --- | --- | --- |
| A：代码审计与可靠性 | 本次完成；已建立 Git 基线并修复阻断接入的缺陷 | 保留本次回归 |
| B：真实 DSH 生命周期 | 隔离固定版本宿主已验证，个人设备与 Web 长会话未验收 | 用户隔离 profile，真实聊天模型，长会话运行 |
| C：真实本地与云端模型 | 仅协议回归通过，未开始真实模型质量验证 | 真实 Ollama LLM+Embedding；用户提供云端配置/凭据后再测云端 |
| D：检索与整理质量 | 保留现有多路召回和两跳关系；尚未做正式评测 | 语义覆盖、相对时间、跨批事实去重、关系扩展 A/B 与可观察解释 |
| E：个人长期运维 | 本次补齐启动/退出/恢复能力，完整交付尚未完成 | 备份恢复演练、安装脚本、长期运行回归 |

明确保留的限制：

- 事实覆盖是规范化全文包含，不能识别所有改写、否定和语义等价；压缩换一种说法时可能重复注入。
- 不同整理批次对同一经历可能生成意义重复的概括；本次解决的是事件、同批整理和并发重放重复，未声称已完成跨批语义合并。
- 时间解析仍主要支持年份；关系理由与画像内容仍依赖真实模型质量，缺少中文人工评测。
- outbox 按单实例使用设计，同一目录不要由多个 DSH 进程共用；Windows 尚无目录 fsync 的断电持久性保证，也未做 Windows 实机测试。
- 发生磁盘写失败时会记录不含私密原文的警告；原始数据仍在 DSH 持久会话，恢复该会话可补录。损坏或被拒绝的 outbox 记录会保留，不自动删除用户数据。
- 单条 HTTP 数据超过 2 MB 会被明确拒绝并保留在 outbox；没有做分片传输。固定错误的模型输出会保留原文、游标和 last_error 并继续退避重试。
- 阶段 A 的 schema 迁移是增量且有回归，仍未代替阶段 E 的完整备份/恢复演练。

## 用户下一步最少操作

本阶段不需要提供密钥，也不需要修改日常 profile。代码与隔离宿主测试可直接复现。进入 C 前，需要一套可访问的真实 Ollama LLM / Embedding 服务及准确模型名；云端只有在用户显式提供 API 配置后测试。可以先完成个人设备上的 B，继续保持独立 DSH_HOME 和测试 profile。

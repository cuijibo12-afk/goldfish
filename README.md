# Goldfish Memory v0.8.0（金鱼）

金鱼是 **DeepSeek Harness（DSH）可按需调用的本地长期记忆工具**。它适合需要跨会话找回具体经历、核查原话、保留观点随时间变化的个人助手。

DSH 是唯一的对话与思考主体。金鱼提供 SQLite、索引、证据、事件与关系管理，不运行第二个聊天 Agent。当前模式不需要 Ollama、BGE、向量服务或金鱼专用模型 Key；模型供应商、模型和凭据由你自己的 DSH 配置决定。

本版本发布源码对应插件/后端 **0.8.0、SQLite schema 6**。已验证环境主要为 Windows、Python 3.11、Node 26 和 DSH **0.2.1-rc.5**，质量验收使用有限虚构案例。其他 DSH 版本、系统、模型和海量长期数据尚未全面验证。

## 工作方式

```text
用户发言 → DSH 主模型理解问题
              ├─ 无需历史：正常回答，可零次金鱼调用
              └─ 需要历史：search → 按缺口决定 explore → 必要时 evidence → 回答

DSH 原生压缩成功 → 追溯被压缩的真人原话 → 持久整理任务
              → 同一 DSH 主模型提案与复核 → 来源校验 → SQLite 原子提交与收据
```

- **B1 主动检索**：`goldfish_search` 找主要记忆；`goldfish_explore` 依据已得记忆提出新的关联问题；`goldfish_evidence` 核查原话。工具提供证据与线索，DSH 自主决定是否查询。
- **B2 明确保存**：说“请记住：……”时，`goldfish_remember` 绑定真人原话、当前授权、签名和持久收据。相同来源重试去重。引用、示例、否定及助手猜测不构成授权。
- **C 总指引**：DSH 会话级的可修订工作摘要，保留当前主线、进展和待解决问题，经原生 sessionProjections 随压缩/恢复延续，不写入长期库。当前轮“细思考”只是主模型当轮原生理解和工具决策，不另存隐藏思维链或独立认知状态。
- **D 压缩交接**：手动 `/compact` 或原生自动压缩成功后，追溯对应原始真人事件，排除助手、工具回显、指引、预测和取消/未完成轮次。固定来源范围、任务/片段 ID、事务、重试与收据避免重放重复写入。
- **E 结构化事件**：事件可跨轮/跨压缩补充，每条事实有自己的逐字证据和偏移。复述补证据，补充、纠正、时间变化、意义变化分别处理；相互矛盾的原话不删除。
- **E 关系网络**：临时检索线索、模型候选、证据复核支持的关系分层。正式边要求两端事实与关系依据，方向保留，明确否认可撤销，引用事实变更会使审核失效。用户自述影响不等于独立证明的因果；possible_cause 始终是候选。

**明确保存与压缩整理使用不同权限。**B2 必须有本轮真人明确授权；D/E 使用开启插件时选择的宿主压缩交接权限，不复用 B2 的保存授权。立即保存不必等待压缩，之后同来源片段会被屏蔽。

## 依赖与安装

需要 64 位 Python 3.10+（Windows 推荐 3.11）、Node 24.9+、已有可运行的 DSH 0.2.1-rc.5 和 npm/pnpm。Python 核心仅标准库，`windows/requirements.txt` 不安装额外模型依赖。首次注册插件可能需要联网解析 DSH 的同版本 peer dependency。

如果尚无 DSH，可在独立目录安装固定版本；不要把安装目录设为你的私人数据目录：

```powershell
npm install --prefix C:\Tools\dsh @deepseek-ai/dsh@0.2.1-rc.5 pnpm@10
```

下载本版本源码，解压到固定位置（例如 `C:\Tools\goldfish`），打开 PowerShell：

```powershell
cd C:\Tools\goldfish
.\windows\Goldfish.ps1 Install `
  -DshExecutable 'C:\Tools\dsh\node_modules\.bin\dsh.cmd' `
  -DshHome "$env:USERPROFILE\.dsh" -Profile web
.\windows\Goldfish.ps1 Start
.\windows\Goldfish.ps1 Chat
```

已有 DSH 时把路径替换为你的真实路径。安装器复用该 DSH，仅添加本地 `link:` 插件，创建独立 Python venv 和缺失的数据库，不安装另一个 DSH、不更换主模型、不导入个人认证。首次 DSH 模型设置仍由你在 DSH 中完成。

安装后，DSH 路径和 profile 保存在本机 `installation.json`，后续可双击 `windows/Start.cmd`、`Chat.cmd`、`Stop.cmd`。**不要移动已链接的源码目录。**脚本默认服务端口 18320；端口被占用会拒绝启动，不会关闭别的进程。隔离测试可用 `-Port` 选择其他端口，但须同步插件的 `serverUrl`。

`Configure.cmd` 只保存/核对 DSH 路径与 profile，不再询问金鱼模型 Key。配置模板为 [windows/config.example.json](windows/config.example.json)，可复制并修改后放到本机数据目录，文件名用 `installation.json`。它不是模型配置，不能放 API Key。

插件模板为 [dsh-plugin/cordis.patch.yml](dsh-plugin/cordis.patch.yml)：

```yaml
- insert:
    - id: goldfish-memory
      name: dsh-goldfish-memory-local
      config:
        mode: tools
        enableRemember: true
        enableGuide: true
        enableCompaction: true
        serverUrl: http://127.0.0.1:18320
        autoCapture: false
```

修改开关后经 DSH 插件管理重载。若数据目录不是默认位置，还需为插件明确设置 `rememberKeyFile` 和 `compactionKeyFile`，分别指向新目录的两个能力文件；它们只给宿主读取，不能放入模型参数或提交到 Git。

## 更新、启动与卸载

1. 备份记忆库；若服务正在运行，用 SQLite backup API 或停止服务后备份，不能只复制正在写入的主文件而漏掉 WAL。
2. 结束当前 DSH 对话、关闭宿主，运行 `windows/Stop.cmd`。
3. 把新源码放到固定目录；在新目录运行 `Goldfish.ps1 Update`，首次指定 DSH 路径。已有数据库不会替换；迁移采用加法方式并备份。
4. 启动金鱼，再用原 DSH 入口启动宿主；若换了源码路径，必须重新注册 link。检查 `/health` 的 version=0.8.0、schema=6、query_only=true、event_memory=true。

默认 `Start` 启用 B1/B2/D/E 的确定性后端。`StartExplicit` 只开放查询与 B2；要真正停自动整理，还需停用插件 `enableCompaction`。`StartReadOnly` 只读取已有库。

卸载使用 `windows/Uninstall.cmd`：停止本安装记录的金鱼服务，从选定 DSH profile 移除插件依赖，**保留数据库、能力文件、备份、venv、DSH 会话和模型配置**。需清理残留时，先自行备份，确认路径后再手动删除；脚本不做递归清空私人数据。

## 数据与隐私

Windows 默认数据目录为 `%LOCALAPPDATA%\GoldfishMemory`；可用 `-DataDir` 或 `GOLDFISH_DATA_DIR` 为隔离安装选择新目录。

- `memory.sqlite3`、WAL/SHM：私人原始证据、记忆、事实、关系和审计。
- `remember-write.key`、`compaction-write.key`：宿主签名能力，不是模型密钥。
- `*.backup-*`、`*.before-D-*`、`*.before-E-*`：一致性备份。
- `installation.json`、`processes.json`、`*.log`：本机配置与运行文件，不上传。
- DSH 的认证、会话、总指引与压缩状态位于你配置的 DSH 数据/profile 目录。

后端只监听 loopback，普通读工具不调用模型。**本地数据库不等于对话绝不离开电脑**：DSH 若使用云模型，正常对话、检索结果及整理所需原话会按 DSH 模型请求发给该供应商。整理不是离线加密保险箱。保护本机账户、备份和 DSH 凭据，不分享原始日志/数据库。

本源码归档不包含个人配置、数据库、聊天记录、能力密钥、部署快照或原始真实模型调用日志。`evals` 中人工编写的虚构 fixture 保留用于测试；旧仓库历史已有虚构语料的评测输出，本版本在新提交中移出当前树，不重写历史。

## 验证范围、费用与限制

E 阶段已有原生 SDK 实际主模型及原 Windows 网页 `/compact` 验收：跨轮事件、同义去重、纠正/意义历史、两跳证据关联与撤销、6000 字之后细节、中断恢复和新会话召回。公开摘要见 [docs/V0.8_VALIDATION.md](docs/V0.8_VALIDATION.md)。本发布准备还验证独立依赖安装、插件加载和模拟模型集成，不能把模拟测试冒充真实对话质量。

- 一次非空整理批次通常额外调用 DSH 主模型两次（提案/复核）；一次压缩可产生多批，失败重试也产生开销。查询自身不新增模型请求。
- 六题/版的小样本对照中，D 平均25.964秒、E26.084秒；E少了模型步骤，但上下文输入约增3.4%，返回字符更多。没有证明普遍更快或省Token。
- 旧 D/B2 跨新来源的全面语义去重、自动事件化未完成；候选窗口和事件预算有限，不能保证任意历史无漏。
- 同模型复核不是独立事实核验。更多关系类型、跨进程多宿主、大库和长期质量未充分验证。
- Claude 等其他路线没有完成同等 E 阶段质量验收；切换模型仍需自行检查长上下文、工具行为和指引更新，不能把某一模型的验收泛化到所有模型。
- Windows Python 全量已有两项子进程测试失败，保留测试与说明，不制造全绿。其他系统只提供源码，不承诺一键脚本兼容。

## 故障与回滚

`Diagnose.cmd` 查看本机健康状态。模型离线不影响已存记忆的确定性查询，但 DSH 正式回答及整理仍需要当前模型可用。可问 DSH“金鱼整理完成了吗”，通过 `goldfish_organization_status` 区分压缩成功与整理成功。

任务持久化，重启可恢复。连续语义校验失败会保留为 `needs_review`，不无限重试；修复后本机维护者可用 `python scripts/retry_event_job.py <完整任务ID>` 签名重排同一原始范围。该维护脚本默认读取标准 Windows 数据目录。

优先回滚方式是保留 v0.8/schema6，关闭自动整理，改用 StartExplicit。若退旧 D/schema5：停后端和 DSH，保留完整 schema6 一致性备份，用 `rollback_e_clone.py --db <schema6库> --output <不存在的新schema5副本>` 生成旧版投影，再切换兼容代码/活动库。旧 D 不能直接打开 schema6；schema5 投影保留概括、原话与收据，但不保留 E 事实/关系表，完整回滚资料在原 schema6 库。切勿原地降级或覆盖唯一备份。

## 开发与打包

```powershell
python -m unittest discover -s tests -p 'test_*.py' -v
node --test dsh-plugin/*.test.mjs dsh-plugin/test.mjs
$env:DSH_TEST_RUNTIME='C:\Tools\dsh'
$env:GOLDFISH_TEST_PYTHON=(Get-Command python).Source
node --test tests/dsh_integration.mjs tests/dsh_tools_integration.mjs tests/dsh_remember_integration.mjs tests/dsh_guide_integration.mjs tests/dsh_event_integration.mjs
python scripts/build_delivery.py --output ..\Goldfish-Memory-v0.8.0-source.zip
```

SDK 集成使用临时数据库与合成模型，不读取你的凭据或真实会话。实际测试结果和未解决失败见 [docs/RELEASE_PREPARATION.md](docs/RELEASE_PREPARATION.md)。构建器只读取已提交的 Git 文件并再次排查敏感路径；不要从包含本机数据的整个目录直接压缩。

## 许可证

现有仓库没有项目 LICENSE。本次未代替作者选择或添加许可证。公开源码不自动授予开放使用、修改或再分发许可；需由作者决定并补充。依赖软件遵循各自许可证。本 README 的安装步骤不替代使用授权。

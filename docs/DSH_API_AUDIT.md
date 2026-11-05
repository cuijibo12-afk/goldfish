> 历史阶段记录：保留用于了解早期实验，不代表v0.8默认配置。当前安装与架构以根README为准；旧原始评测输出已从本版本归档中排除。

# DSH 生命周期接口核查（2026-11-04）

本次同时检查 npm 发布版本和官方仓库主分支，不能把二者混称为同一个“最新版”。

| 核查对象 | 固定版本/提交 | 验证方式 |
| --- | --- | --- |
| npm `@deepseek-ai/dsh` latest/next | `0.2.1-rc.5` | `npm view`，实际安装和运行 |
| 官方对应 tag `dsh-v0.2.1-rc.5` | `b830ec950faba0a46dda564be3e2d854afed94f1` | 下载官方源码，对照安装包 |
| 官方 main HEAD（核查时） | `8bd073392eef6f54e921a093b95076248eb08cbb`，版本 `0.2.2-alpha.3` | 源码核查；未宣称已运行该 alpha |

官方入口：[deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)。以下源码链接固定到实际运行的 rc.5 提交。

## 接口事实与现有桥接的问题

1. **`agent/pre-step` 是 waterfall。** rc.5 payload 为 `{agent,messages,turn,step,signal}`，回调接收 `next()`；`PreStepDecision` 为 reject 或 enter，后者还有可选 `startsRequestSeries:true`。包装下游结果应返回 `{...decision,messages}`。原桥接重建 enter 对象会丢掉这个标志。main alpha 的 payload 还增加了 `modelSelection`；当前桥接无需依赖该字段。
   [官方类型](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/packages/core/agent/src/runtime-types.ts#L112)，[Hook 签名](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/packages/core/agent/src/runtime-types.ts#L320)。

2. **`messages` 只是准备进入本步骤的新批次。** 工具调用后的步骤可能没有新的真人消息。应直接使用 payload.agent 取得对应 session，从新批次或当前派生历史找最新真人输入。同轮压缩还可能将真人查询从有效历史中完全替换，因此桥接用 WeakMap 保留当前 agent 的最近真人查询，只用于召回，不作事实入库或“已注入”标记；宿主销毁后随 agent 释放。原桥接仅查新批次，后续步骤可能不召回；间接调用 `requireInitiator()` 虽在标准 loop 下可用，但 payload 已提供准确的 agent。
   [实际 preStep 实现](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/packages/core/agent-loop/src/agent.ts#L259)。

3. **注入归属与位置。** 用户角色的主对话消息可以来自不同 producer；`source.kind` 没有通用 `plugin` 值。金鱼使用 `plugin:goldfish-memory` 和 `form:'recall'`，位于已领取输入之后，保留下游 runtime context 的位置。实际请求使用已提交的 `session.deriveMessages()`，所以该消息进入主模型输入并成为持久会话材料。无需模型调用记忆工具。
   [MessageSource 与 ContextForm](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/packages/llm/llm/src/message.ts#L55)，[提交批次](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/packages/core/agent-loop/src/agent.ts#L426)。

4. **`session/event(session,event)` 是提交后的同步通知。** `user/message` 的 data 直接是 UserMessage；`assistant/message` 的 data 为 `{turn,step,message,stream,usage?,interrupted?}`。event.seq 是单调序号，event.time 是 Unix 毫秒。不能将 listener 返回的 Promise 当成持久化屏障。原串行 Promise 链在投递失败时直接丢事件，退出也不等待。
   [事件 Hook 定义](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/packages/core/session/src/index.ts#L64)，[事件模型](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/packages/core/session/src/types.ts#L308)。

5. **取消与重试的提交边界由宿主负责。** pre-step 或 prepareCall 等异步阶段取消时，模型输入尚未提交。金鱼请求必须跟随宿主 signal 取消，且不能在召回成功时自行维护“已注入”黑名单。请求重试复用第一次已提交的输入，不重复追加该批次。失败 attempt 只产生 `assistant/attempt`；取消时已经交付的文本前缀可以提交为带 `interrupted:true` 的 `assistant/message`。金鱼只采后者的 text 块，排除 reasoning、工具内容和失败 attempt。
   [首次尝试标志与输入提交](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/packages/core/agent-loop/src/agent.ts#L408)，[失败/取消结算](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/packages/core/agent-loop/src/agent.ts#L462)。

6. **`deriveMessages()` 反映当前有效 surface。** compaction 的 replace 会移除被遮蔽的节点；不能扫描全部旧事件来判断当前模型是否仍看得到记忆。该方法返回新数组，内部 message 为共享冻结对象，插件只能创建新消息/数组。实际集成测试调用了官方 `BasicCompactionEngine.compactNow()`，验证原记忆消失后可以重新注入；还通过实际 tool-call/工具执行，在同轮后续 pre-step 调用官方 `compactRegion()`，验证真人查询和旧记忆都被替换后仍能召回。`compactNow()` 只用于 idle 会话，运行中的同轮区间使用 `compactRegion()`。
   [派生历史实现与契约](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/packages/core/session/src/index.ts#L966)。

7. **恢复不会重发历史 `session/event`。** 新增生产代码不能调用已弃用的 `snapshotEvents()`、`eventAt()`、`ownEvents()`。补录使用 `ctx.sessionPersistence.open(id,'read')`，通过 `handle.read(offset,256)` 分页并最终 close；从 `inheritedEventCount` 到 `firstLiveSeq`，避免再次收录 fork 的继承前缀。`agent/created` 是可以等待的生命周期，用于安排恢复补录；普通实时采集依然使用提交事件。
   [seed 不重发](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/packages/core/session/src/index.ts#L552)，[弃用说明](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/packages/core/session/src/index.ts#L703)，[分页存储接口](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/packages/session/session-persistence/src/handle.ts#L59)。测试代码允许使用 snapshotEvents 观察结果，生产桥接没有新增该调用。

8. **Cordis 可选服务不是普通对象属性。** 真实 runtime 发现，即使写 `ctx.pluginPackages?.resolution`，未声明注入关系时仍可能在 getter 处抛错。单纯的 mock Context 不会暴露它。现桥接通过公开 `ctx.get(name)` 访问可选服务。profile 私有目录可从 `pluginPackages.resolution.profileDir` 取得；程序化宿主没有该服务时使用显式 outboxDir 或 DSH_HOME/default home。该问题已在真实宿主复现并修复。
   [PluginPackages.resolution](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/packages/boot/app-boot/src/profile-resolution/service.ts#L79)，[profileDir](https://github.com/deepseek-ai/deepseek-harness/blob/b830ec950faba0a46dda564be3e2d854afed94f1/packages/boot/app-boot/src/profile.ts#L150)。

## 安装核查

只向新建的 `/workspace/scratch/goldfish-dsh-home` 写入测试 profile。实际执行了：

```bash
npm install --prefix /workspace/scratch/dsh-runtime @deepseek-ai/dsh@0.2.1-rc.5 --no-audit --no-fund
DSH_HOME=/workspace/scratch/goldfish-dsh-home node /workspace/scratch/dsh-runtime/node_modules/@deepseek-ai/dsh/lib/bin.js --profile goldfish-test --from-default-profile sdk-minimal --dump-config
DSH_HOME=/workspace/scratch/goldfish-dsh-home node /workspace/scratch/dsh-runtime/node_modules/@deepseek-ai/dsh/lib/bin.js plugin --profile goldfish-test add link:/workspace/goldfish-memory-v0.2/dsh-plugin
DSH_HOME=/workspace/scratch/goldfish-dsh-home node /workspace/scratch/dsh-runtime/node_modules/@deepseek-ai/dsh/lib/bin.js --profile goldfish-test --dump-config
DSH_HOME=/workspace/scratch/goldfish-dsh-home node /workspace/scratch/dsh-runtime/node_modules/@deepseek-ai/dsh/lib/bin.js --profile goldfish-test --help
```

安装、配置合成和应用 help 均退出 0，合成树包含 goldfish-memory。`--from-default-profile` 会替换目标 profile，所以只能对本次新建的隔离 profile 使用。实际会话行为验证使用同版本公开 core 插件组成的隔离宿主，见 [DSH_VALIDATION.md](DSH_VALIDATION.md)；没有把 help 或配置导出当成真实模型调用证明。

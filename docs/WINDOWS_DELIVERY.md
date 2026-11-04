# Windows交付验证范围

第四阶段基线：`c5655f9`，分支 `phase-d/embedding-performance`，交付前工作区干净。

入口：[面向非程序员的安装说明](../windows/开始使用.md)。安装工具在 `windows/`，SQLite备份/迁移入口在 `scripts/migrate_database.py`，原始schema迁移实现在 `goldfish/engine.py`。核心引擎仅标准库，可选本地CPU模型依赖在 `windows/requirements.txt`。DSH固定0.2.1-rc.5，模型固定revision和文件hash，首次安装联网下载，不内置模型权重或凭据。

云端检查：91项Python回归通过；迁移脚本在临时空库创建schema3，二次运行先备份再迁移通过；下载器的固定模型hash与第四阶段真实文件一致；交付ZIP只读取Git跟踪文件并扫描凭据格式，排除DB/运行目录。原项目不删除。

本环境没有PowerShell/Windows，因此不能声称Windows脚本运行或安装已通过。Windows进程管理、DPAPI凭据、npm/模型下载、DSH Web连接认证与用户硬件性能，需要在个人电脑按说明验证。脚本没有自动配置DSH主模型；提供API环境变量，DSH连接选择保持独立。

卸载保留用户数据，包括DSH可能自行保存的连接凭据。若需彻底清除，必须由用户确认备份后自己删除用户数据目录。项目源码、示例与交付包不包含用户API key、个人聊天库或临时凭据。测试语料/历史评测输出是标明的虚构数据及真实模型对虚构数据的响应。

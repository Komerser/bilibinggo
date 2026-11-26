# 更新当前 Binggo 实例

各节点独立更新。Windows 使用「更新版本」，Ubuntu systemd 使用「更新此服务器」。

## 使用

### Ubuntu systemd

首次安装通过 Ubuntu 部署包执行 `sudo bash deploy.sh install`。已有 systemd 部署先等待任务结束，再执行 `sudo bash deploy.sh upgrade`。如旧服务使用非默认端口，可将端口作为第二个参数传入。

以后从「概览 → 项目信息 → 更新此服务器」检查和安装。管理员在自己的 SSH 终端运行 `sudo cat /etc/binggo-updater/admin-token` 查看确认口令，只在当前服务器的受信任管理页输入。浏览器关闭后更新继续运行。

更新服务从本 fork 取最新正式 Ubuntu 部署包，校验 SHA-256 后暂停新任务并等待当前任务完成。验证新版网页、运行版本和原集群连接后才恢复任务接收。安装失败会尝试还原旧程序、环境和升级前数据。

状态和本机历史位于 `/var/lib/binggo-updater/state`，备份位于同级 `work` 下的对应任务目录。可通过 `sudo journalctl -u binggo-updater.service` 检查后台进度。备份仅限服务器管理员访问。

Docker 部署仍通过部署包手动升级。

### Windows

概览 → 项目信息 → 更新版本。读取最新正式 Release 后显示目标版本、说明、任务状态和确认按钮。
下载并校验便携包后停止领取新的本机/集群任务，等待当前任务结束。等待期间可以取消。
独立更新进程在应用退出后备份整个安装目录，保留 user-data 和安装器文件，切换程序并启动。
实际运行版本、数据目录、网页和原有集群连接通过检查后才完成更新交接。
启动失败时恢复旧程序及升级前数据；恢复未通过验证时明确提示人工处理。

本机更新日志独立存储，日志页提供折叠明细。它不调用集群事件写入或同步接口。
状态、历史及备份保存在安装目录同级的 `.binggo-updates/<安装路径摘要>/`，备份可能包含账号配置，应按原数据保护。
不会自动清理备份。突然断电、磁盘损坏或文件被其他程序占用可能需要人工恢复。

## 发行包要求

- 固定来源：[Komerser/bilibinggo 正式 Releases](https://github.com/Komerser/bilibinggo/releases)。
- Windows 便携包 `Binggo-Portable-win64.zip`。
- 同名 `.sha256`，或包含该文件唯一记录的 `SHA256SUMS.txt`。
- 包内包含 `_internal`、`Binggo-RepostFix.exe`、独立 `Binggo-Updater.exe`。
- 包内 `update-capabilities.json` 声明 `schema: 1` 与实际版本。
- 新版必须实现本机更新交接接口。此前不支持更新器的包会在退出应用前被拒绝。
- 保持版本、标签、安装包及能力声明一致；校验文件验证完整性，不代替发布账户安全或代码签名。

`packaging/windows/build.ps1` 已加入更新器、能力声明及独立校验文件的构建。
Windows Release 工作流上传对应校验文件。首次安装这项功能仍需部署一次本地发行包。

Ubuntu 打包在前端构建完成后运行 `python packaging/linux/build.py`。发布时上传生成的部署包及同名 `.sha256`。

## 开发和预览

源码运行不会覆盖 Git 工作区。设置 `BINGGO_UPDATE_DEMO=1` 可以在页面预览成功和失败恢复；真实安装被禁用。
演示会写带有「演示」标记的本机日志，不改变版本号、账号或任务。
开发预览使用独立 BINGGO_HOME，不应加载真实账号或加入真实集群。

运行针对性测试：`python -m pytest tests/test_local_update.py`。
构建完成后，`python scripts/test_local_update_package.py` 在全新目录中验证实际进程退出、备份、切换、启动和数据保留。
加 `--failure` 故意使用不匹配的预期版本，验证真实恢复流程。测试不会连接集群或加载用户账号。

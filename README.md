<p align="center">
  <img src="docs/images/logo.svg" alt="Binggo" width="80" height="80">
</p>

<h1 align="center">Binggo 6.1.3.02 集群版</h1>

<p align="center">
  B 站抽奖助手 · 发现、核验、分享与参与活动
</p>

<p align="center">
  <a href="https://github.com/Aggressiveless/bilibinggo/releases/tag/v6.1.3.01"><b>下载集群版</b></a>
  &nbsp;·&nbsp;
  <a href="#开始使用"><b>开始使用</b></a>
  &nbsp;·&nbsp;
  <a href="#工作集群"><b>工作集群</b></a>
  &nbsp;·&nbsp;
  <a href="CHANGELOG.md"><b>更新概要</b></a>
</p>

Binggo 可以从 UP 主合集、追踪动态和视频检索中发现抽奖活动。单台电脑可独立完成检索、核验、导入和参与。多台电脑也可以组成工作集群，共享已看视频记录和活动目录，分工检索与检测。

本版本由 Aggressiveless 从 [Komerser 的 6.1.3 集群版](https://github.com/Komerser/bilibinggo/releases/tag/v6.1.3) 派生。原项目为 victor 的 [bilibinggo](https://github.com/luovicter-collab/bilibinggo)。

## 6.1.4.02 试用

「数据源 → 监控与合集」现可切换「活动整理UP主」与「活动发布UP主」。发布者名单由已核验的原创抽奖动态累积，可通过「自动更新并搜索」提交本机任务。支持新版本的集群会依次检查两类来源，长期无发布的账号会从发布者名单移除并留下工作日志。

## 6.1.3.02 更新

- 合入上游 6.1.3 的连续追踪、独立轮间参与、节点待参与数量及逐条预检跳过；保留 6.1.2 的检索和核验缓存。
- 服务器检查更新时显示 Aggressiveless 安装源与 Komerser 上游功能源。GitHub 标签为 `v6.1.3.01`，实际安装和运行版本为 `6.1.3.02`。
- Ubuntu systemd 支持「更新此服务器」，Windows 支持「更新版本」。确认后下载并校验正式版，等待任务结束后备份、安装并重启。
- 新版启动验证失败时尝试恢复旧程序和升级前数据。更新结果可在「日志 → 本机更新日志」查看。

## 下载与升级

| 平台 | 6.1.3.02 下载 |
| --- | --- |
| Windows x64 | [安装包](https://github.com/Aggressiveless/bilibinggo/releases/download/v6.1.3.01/Binggo-Setup-win64.exe) · [便携版](https://github.com/Aggressiveless/bilibinggo/releases/download/v6.1.3.01/Binggo-Portable-win64.zip) |
| Ubuntu | [部署包](https://github.com/Aggressiveless/bilibinggo/releases/download/v6.1.3.01/Binggo-6.1.3.02-ubuntu.tar.gz) · [SHA-256 校验文件](https://github.com/Aggressiveless/bilibinggo/releases/download/v6.1.3.01/Binggo-6.1.3.02-ubuntu.tar.gz.sha256) |
| 源码与校验 | [源码 ZIP](https://github.com/Aggressiveless/bilibinggo/releases/download/v6.1.3.01/Binggo-6.1.3.02-source.zip) · [SHA256SUMS.txt](https://github.com/Aggressiveless/bilibinggo/releases/download/v6.1.3.01/SHA256SUMS.txt) |

Windows 运行安装包，或解压便携版后启动 `Binggo-RepostFix.exe`。控制台默认地址为 [127.0.0.1:8182](http://127.0.0.1:8182)。已有用户请先等任务结束，退出程序并备份原目录中的 `user-data`。更新程序文件后保留该目录，即可沿用登录、设置和历史。

Ubuntu 部署包内含预构建前端和安装说明。Ubuntu 24.04 及以上可用 systemd 安装。

```bash
sha256sum -c Binggo-6.1.3.02-ubuntu.tar.gz.sha256
tar -xzf Binggo-6.1.3.02-ubuntu.tar.gz
cd binggo-server-6.1.3.02
sudo bash deploy.sh install
```

Ubuntu 22.04 可在 Docker Engine 和 Compose 就绪后运行 `sudo bash deploy.sh docker`。systemd 的数据目录为 `/var/lib/binggo`，Docker 使用部署目录下的 `user-data`。单人远程使用时可以通过 SSH 端口转发访问控制台。要让其他设备加入集群，请配置成员能够访问的 HTTPS 入口。 [主节点入口配置](docs/cluster.md#主服务器入口)

### 一键版本更新

Windows 发行包可在「概览 → 项目信息」选择「更新版本」。Ubuntu systemd 的同一位置提供「更新此服务器」。程序从正式 Releases 下载部署包，等待任务结束并备份，再切换版本。查看进度和结果请用「日志 → 本机更新日志」。已有 Ubuntu systemd 部署首次升级到带更新器的发行包时，先结束任务，再从新部署包的解压目录执行 `sudo bash deploy.sh upgrade`。 [版本更新说明](docs/local-version-update.md)

旧版更新器要求标签与运行版本相同。首次迁移到 6.1.3.02 时，Ubuntu 从已校验的部署包运行 `sudo bash deploy.sh upgrade`，Windows 安装新版并保留 `user-data`。迁移后可继续在页面安装后续合并版。

## 开始使用

1. 在控制台侧栏扫码登录 B 站。需要模型核验或解析转发活动时，在「概览」配置 LLM 并完成连接测试。
2. 打开「数据源」。可以更新 UP 主合集、添加追踪 UP 主并检查动态，也可以主动检索视频。主动检索支持组合或分别搜索，进度分批保存，筛选后的候选可以复核并导入。
3. 在「活动」查看规则和开奖时间，按需选择单条参与或开始顺序参与。顺序参与按当前筛选和排序逐项处理，遇到失败会停止。活动也可导出为 JSON，接收方先预览再导入。
4. 在「日志」查看执行结果和任务队列。等待项可以调整顺序或移除，失败或中断且支持恢复的任务可以重试。「概览」显示 B 站请求和模型用量。

更多操作见 [控制台使用指南](docs/console.md)。

## 工作集群

### 发起与加入

1. 主节点在「工作集群」填写节点名称和对其他成员可用的 HTTPS 地址，点击「发起工作集群」，然后显示邀请码。
2. 成员在自己的 Binggo 中登录 B 站，选择「参与工作集群」，填写主节点地址、邀请码和本机节点名称。
3. 状态显示「同步正常」后，即可同步活动和领取检索任务。视频检测与追踪检查还需要在执行节点配置模型并通过连接测试。

Windows 和 Ubuntu 都可以运行主节点或工作节点。成员从本机主动连接主节点，只有主节点需要提供成员可达的 HTTPS 入口。本机的 `127.0.0.1` 可用于在同一台电脑上测试。

### 集群怎样工作

- **合并 BV 历史。** 支持该功能的成员上传本机已收录的 BV，主节点去重后持久保存。新的检索跳过集群已经看过的视频。
- **逐轮检索与检测。** 每轮由一个节点按发布时间检索一个关键词，目标为 **1,000 个历史未收录的视频**。结果页先用完时，直接检测已收录的视频。在线节点在轮间依次导入活动，随后进入下一个关键词。
- **节点分工。** 主节点可以为节点勾选「追踪 UP 主」或「主动搜集」，并单独暂停主动搜集。合集作者与监控名单按 UID 合并，所选节点并行核验线索，结果同步到集群活动目录。
- **查看任务和节点。** 工作集群页显示本轮关键词、待处理视频、各节点历史收录量、当前状态及累计检测命中率。「数据源 → 主动检索 → 成果展示」可以打开集群检出的仍开放活动。

集群共享活动信息、BV 标识和追踪名单。B 站 Cookie、模型密钥及参与记录保存在各节点本机。成员可用当前账号在本机授权轮间顺序参与。完整调度、跨机器入口及旧节点兼容性见 [工作集群手册](docs/cluster.md)。

## 6.0.3 到 6.1.0 更新概要

| 提交 | 更新 |
| --- | --- |
| [`7c32e47`](https://github.com/Komerser/bilibinggo/commit/7c32e47) | 加入工作集群预览，支持邀请成员、派发任务和同步活动。 |
| [`493c95b`](https://github.com/Komerser/bilibinggo/commit/493c95b) | 日志页加入集群事件，可查看领取、完成、重试和限流。 |
| [`dcb359d`](https://github.com/Komerser/bilibinggo/commit/dcb359d) | 主动检索页展示本机集群任务、本轮贡献和近期结果。 |
| [`309cf67`](https://github.com/Komerser/bilibinggo/commit/309cf67) | 集群先收录视频，再分配视频检测。 |
| [`c97f9ef`](https://github.com/Komerser/bilibinggo/commit/c97f9ef) · [`652c160`](https://github.com/Komerser/bilibinggo/commit/652c160) | 显示集群检出活动和视频转化率，补计有记录的首轮结果。 |
| [`0f7c3fb`](https://github.com/Komerser/bilibinggo/commit/0f7c3fb) | 将集群检出的活动移到主动检索的成果展示中。 |
| [`cfa5afe`](https://github.com/Komerser/bilibinggo/commit/cfa5afe) | 分享和同步开奖时间待确认的开放活动。 |
| [`18e4a54`](https://github.com/Komerser/bilibinggo/commit/18e4a54) | 合并节点历史 BV，显示节点统计，轮间顺序导入后立刻开启下一轮。 |
| [`130519b`](https://github.com/Komerser/bilibinggo/commit/130519b) | 改为每轮一个关键词，简化检索任务队列。 |
| [`723ec79`](https://github.com/Komerser/bilibinggo/commit/723ec79) | 汇总追踪 UP 主，支持主节点指派检查，展示追踪及参与等节点状态。 |

更早版本见 [CHANGELOG](CHANGELOG.md)。问题和建议可以提交到 [GitHub Issues](https://github.com/Komerser/bilibinggo/issues)。

## 源码与文档

Python **3.12+**，Node.js **20+**（构建前端时使用）。

```bash
git clone https://github.com/Aggressiveless/bilibinggo.git
cd bilibinggo
python -m pip install -r requirements.txt
cd web/frontend && npm ci && npm run build && cd ../..
python scripts/run_dashboard.py
```

源码模式默认监听 `127.0.0.1:8787`。其他入口见 [命令行说明](docs/cli.md) 与 [MCP v2 接入](mcp/README.md)。

## 使用与转载声明

Binggo 仅供个人学习和研究，是未经哔哩哔哩授权或认可的第三方工具。请遵守适用法律及 B 站平台规则。禁止将本项目或衍生工具用于收费代抽、营销引流等商业用途，禁止批量操作多账号、养号、控号和矩阵运营，也不得用于黑产或其他违法违规活动。账号封禁、功能限制、数据丢失及法律追责等使用后果由使用者承担。 [上游免责声明](https://github.com/luovicter-collab/bilibinggo#免责声明)

转载或二次发布时，请保留使用声明及适用的版权和许可说明，注明原作者 victor、集群版维护者 Komerser 及本 fork 维护者 Aggressiveless，并附上 [原项目](https://github.com/luovicter-collab/bilibinggo)、[集群版仓库](https://github.com/Komerser/bilibinggo) 与 [本 fork](https://github.com/Aggressiveless/bilibinggo)。原项目 README 将项目标注为 MIT，当前仓库未附独立的 `LICENSE` 文件。 [上游许可说明](https://github.com/luovicter-collab/bilibinggo#license)

## Star History

<!-- star-history:start -->
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/star-history/star-history-dark.svg">
  <img alt="Star history" src="assets/star-history/star-history-light.svg">
</picture>
<!-- star-history:end -->

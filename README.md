<p align="center">
  <img src="docs/images/logo.svg" alt="Binggo" width="80" height="80">
</p>

<h1 align="center">Binggo</h1>

<p align="center">
  B 站抽奖助手 · 发现、核验、分享与参与活动
</p>

<p align="center">
  <a href="https://github.com/Komerser/bilibinggo/releases/latest"><img src="https://img.shields.io/github/v/release/Komerser/bilibinggo?style=flat-square" alt="Release"></a>
  <a href="https://github.com/Komerser/bilibinggo/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/Komerser/bilibinggo/ci.yml?branch=main&style=flat-square&label=CI" alt="CI"></a>
  <a href="https://github.com/Komerser/bilibinggo/stargazers"><img src="https://img.shields.io/github/stars/Komerser/bilibinggo?style=flat-square" alt="Stars"></a>
</p>

<p align="center">
  <a href="https://github.com/Komerser/bilibinggo/releases/tag/v6.0.3"><b>下载 6.0.3</b></a>
  &nbsp;·&nbsp;
  <a href="CHANGELOG.md"><b>更新记录</b></a>
  &nbsp;·&nbsp;
  <a href="docs/console.md"><b>文档</b></a>
  &nbsp;·&nbsp;
  <a href="https://github.com/Komerser/bilibinggo/issues"><b>反馈</b></a>
</p>

Binggo 从 UP 主合集、监控用户动态和视频检索中发现抽奖活动。你可以在 Web 控制台核验候选、分享活动清单、逐项参与，并查看任务进度与用量。由 Komerser 维护的这个 fork 基于 victor 的 [bilibinggo](https://github.com/luovicter-collab/bilibinggo)。

## 使用与转载声明

Binggo 仅供个人学习和研究，是未经哔哩哔哩授权或认可的第三方工具。使用时须遵守适用的法律法规与 B 站平台规则。禁止将本项目或衍生工具用于收费代抽、营销引流等商业用途，禁止批量操作多账号、养号、控号和矩阵运营，也不得用于黑产或其他违法违规活动。账号封禁、功能限制、数据丢失或法律追责等使用后果由使用者自行承担，项目作者及贡献者不承担相关责任。见 [上游免责声明](https://github.com/luovicter-collab/bilibinggo#免责声明)。

转载或二次发布时，请保留使用声明及适用的版权和许可说明，注明原作者 victor 并附上上游仓库链接。转载本 fork 的内容或发行包时，也请注明维护者 Komerser 并附上 [本 fork 的仓库](https://github.com/Komerser/bilibinggo)。

## 6.0.3 更新

- **组合搜索**。主动检索默认将输入词作为一条查询发往 B 站，也可切换到分别搜索。
- **断点恢复**。主动检索和数据源更新分批保存，在「日志」中可重试失败或中断的任务。
- **筛选和安全修复**。调用模型前筛除缺少活动证据的候选，修复数据库连接释放，保护 LLM API Key 并避免重复提交评论。

完整的更新内容见 [CHANGELOG](CHANGELOG.md) 和 [6.0.3 发布说明](https://github.com/Komerser/bilibinggo/releases/tag/v6.0.3)。

## 下载与安装

| 平台 | 6.0.3 下载 |
| --- | --- |
| Windows x64 | [安装包](https://github.com/Komerser/bilibinggo/releases/download/v6.0.3/Binggo-Setup-win64.exe) · [便携版](https://github.com/Komerser/bilibinggo/releases/download/v6.0.3/Binggo-Portable-win64.zip) · [SHA-256 校验](https://github.com/Komerser/bilibinggo/releases/download/v6.0.3/SHA256SUMS.txt) |
| Ubuntu | [部署包](https://github.com/Komerser/bilibinggo/releases/download/v6.0.3/Binggo-6.0.3-ubuntu.tar.gz) · [SHA-256 校验文件](https://github.com/Komerser/bilibinggo/releases/download/v6.0.3/Binggo-6.0.3-ubuntu.tar.gz.sha256) |

### Windows x64

运行安装包，或解压便携版后运行 `Binggo-RepostFix.exe`。控制台默认打开在 [127.0.0.1:8182](http://127.0.0.1:8182)，数据位于程序旁的 `user-data`。升级时先等任务结束，再退出 Binggo 并备份整个 `user-data`。覆盖或重装程序时保留该目录。

### Ubuntu

部署包内含预构建前端和安装说明。首次启动会创建空数据库。Ubuntu 24.04 可使用 systemd 安装，使服务开机自启并在进程退出后重启。

```bash
sha256sum -c Binggo-6.0.3-ubuntu.tar.gz.sha256
tar -xzf Binggo-6.0.3-ubuntu.tar.gz
cd binggo-server-6.0.3
sudo bash deploy.sh install
```

Ubuntu 22.04 可在 Docker Engine 和 Compose 就绪后，运行 `sudo bash deploy.sh docker`。同一台服务器选一种方式即可。systemd 将数据存到 `/var/lib/binggo`，Docker 将数据存到解压目录下的 `user-data`。

在自己的电脑上运行 `ssh -N -L 8182:127.0.0.1:8182 用户名@服务器地址`，再打开 [127.0.0.1:8182](http://127.0.0.1:8182)。SSH 断开后服务继续运行。服务重启后，如页面显示队列或工作流已暂停，可根据任务状态手动恢复。

## 开始使用

1. 在控制台侧栏扫码登录。需要使用转发解析或模型核验时，在「概览」配置 LLM 并完成连接测试。
2. 在「数据源」更新指定 UP 主合集或监控用户动态，也可以检索视频、核验候选后导入。刚安装时活动列表为空，更新或导入后即可开始筛选。
3. 在「活动」查看规则，选择单条参与或「开始顺序参与」。顺序参与会按当前筛选和排序跨页逐项处理未参加活动，遇到失败停止后续操作。
4. 在「日志」查看任务进度、参与结果和等待队列。等待项可调整顺序或移除，「概览」可查看 B 站请求和模型用量。

视频检索会按唯一新视频计数并保存进度，停下后可继续检索。更新、检索和参与共用任务队列，便于在同一处查看与管理。

## 数据与隐私

Cookie、模型配置、活动库和参与历史保存在运行 Binggo 的电脑或服务器。启用 LLM 功能时，需要解析的内容会发给你配置的模型服务。当前数据路径可在「概览 → 项目信息」中查看。下载包只包含程序和配置模板，活动分享文件也只包含上面列出的活动字段。

## 文档与开发

- [控制台使用指南](docs/console.md)
- [命令行说明](docs/cli.md)
- [可选的 MCP v2 接入](mcp/README.md)

Python **3.12+**，Node.js **20+**（仅构建前端时需要）。

```bash
git clone https://github.com/Komerser/bilibinggo.git && cd bilibinggo
pip install -r requirements.txt
cd web/frontend && npm ci && npm run build && cd ../..
python scripts/run_dashboard.py
```

源码模式默认使用 `127.0.0.1:8787`。运行测试前安装 `requirements-dev.txt`，然后执行 `python -m pytest tests/ -q`。

## Star History

<!-- star-history:start -->
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/star-history/star-history-dark.svg">
  <img alt="Star history" src="assets/star-history/star-history-light.svg">
</picture>
<!-- star-history:end -->

## 许可说明

上游 README 将项目标注为 MIT，当前仓库未附独立的 `LICENSE` 文件。转载与使用声明见上文。参见 [上游许可说明](https://github.com/luovicter-collab/bilibinggo#license)。

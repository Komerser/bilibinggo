# Binggo Ubuntu 部署包

部署包含预构建网页、Python 程序、依赖锁和 systemd 服务。完整说明见 [GitHub 仓库](https://github.com/Komerser/bilibinggo)。

下载对应的 `.tar.gz` 与 `.sha256`，执行 `sha256sum -c <校验文件>`，校验成功后解压。以下命令在解压目录运行。

| 操作 | 命令 |
| --- | --- |
| Ubuntu 24.04 及以上首次部署 | `sudo bash deploy.sh install` |
| 旧 systemd 部署首次升级 | `sudo bash deploy.sh upgrade` |
| Ubuntu 22.04 Docker 部署 | `sudo bash deploy.sh docker` |
| 停止服务后备份并重新启动 | `sudo bash deploy.sh backup` |

systemd 保留 `/var/lib/binggo` 的账号配置、历史和集群数据。先等正在运行的任务结束。Docker 数据保存在部署目录的 `user-data`，Docker 通过新部署包手动升级。

远程管理可使用 SSH 隧道，例如 `ssh -N -L 8182:127.0.0.1:8182 user@server`，再打开 [本地入口](http://127.0.0.1:8182/)。

以后可在「概览 → 项目信息」点击「更新此服务器」。管理员通过 SSH 在自己终端运行 `sudo cat /etc/binggo-updater/admin-token` 查看确认口令。完成过程与结果见「日志 → 本机更新日志」。如有等待项，重启后按需继续队列。

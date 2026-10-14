# 配置

`--config 文件路径` 可建立独立实例；默认 `config.json`，或环境变量 `DSH2GITHUB_CONFIG`。

| 字段 | 默认 | 说明 |
|---|---|---|
| `repo` | 必填 | 自己所有的专用私有任务仓库 |
| `dsh_home` | `DSH_HOME` 或 `~/.dsh` | DSH 本机数据目录 |
| `workspace` | `workspace` | 新建任务会话的工作目录 |
| `runtime_dir` | `.runtime` | 状态、日志、配置备份与历史镜像 |
| `poll_seconds` | 15 | GitHub 检查间隔，1～20 秒 |
| `sync_seconds` | 30 | 可见历史同步最短间隔，至少 10 秒 |

相对路径以配置文件目录为基准。模型、endpoint、密钥和沙盒策略由 DSH 管理，不放入桥接器配置。

同一运行目录只允许一个监听器；单会话只有一条桥接任务同时派发。宿主审批心跳为 60 秒租约，10 秒续期；审批等待上限 15 分钟。

重新运行 `install-host` 可更新本插件注册，原补丁文件会先备份。删除该补丁中 `# Managed by dsh2github install-host` 对应的插入块即可卸载宿主接入；请在无桥接任务时进行。不要用早期整个文件备份覆盖后续其它配置修改。

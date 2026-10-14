# dsh2github

用 GitHub Issue 远程继续本机 **DeepSeek Harness（DSH）原会话**，并在手机上允许或拒绝具体权限请求。基于 [codex2github](https://github.com/CircuitWizard-SI/codex2github) 的使用方式重新实现 DSH 执行接口。

当前版本面向 Windows 和 DSH **0.2.0-rc.2**。电脑需要在线，Harness 和监听器需要保持运行。

## 功能

- 选择本机原会话，保留原 Session ID 和历史；新 Issue 可启动新任务。
- 会话入口、续聊和新任务自动添加 `dsh-session` 标签。
- 单会话模型和思考档位选择、同轮 `/steer`、下一轮排队、等待消息撤回。
- 完成或失败后回传最终回复，Issue 保持打开。
- 将可见对话和主会话／子代理目录同步到专用私有任务仓库。
- DSH 需要确认时，GitHub 显示工具、原因和唯一请求 ID。所有者通过 `/allow ID` 或 `/deny ID` 操作；允许仅对这一次生效。
- 可选 GitHub Actions 完成提醒，与任务执行分离。

源码仓库可公开；任务、会话历史和确认指令必须放在自己的**专用私有仓库**。认证沿用本机 `gh` 和 DSH，不在项目配置中填写 API 密钥。

## 安装

需要 Python 3.11+、已登录的 GitHub CLI、已配置模型的 DeepSeek Harness。

```powershell
git clone https://github.com/CircuitWizard-SI/dsh2github.git
cd dsh2github
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
gh auth login
gh repo create YOUR_USER/dsh-tasks --private
.\.venv\Scripts\python.exe -m dsh2github init --repo YOUR_USER/dsh-tasks
.\.venv\Scripts\python.exe -m dsh2github install-host --profile desktop
.\.venv\Scripts\python.exe -m dsh2github setup
.\scripts\start.ps1
.\.venv\Scripts\python.exe -m dsh2github doctor
```

将 `YOUR_USER` 换成自己的账号。`setup` 打印会话入口链接。打开入口 Issue，发送 `/session 会话名或ID`，连接确认后直接留言。也可以新建 Issue 写任务。

`install-host` 向该 DSH profile 的补丁文件追加一个宿主插件，并先保存本机备份；不改模型路由、密钥或权限策略。Desktop 支持补丁实时加载；若 `doctor` 未检测到宿主，请在没有运行任务时重新打开 Harness。插件仅监听 `127.0.0.1`，使用随机本机令牌；令牌不上传 GitHub。支持已有 `web` profile，使用 `--profile web` 安装后启动 `dsh web`。

后台启动：`scripts/start.ps1`，运行独立监督进程，在监听意外退出后重新启动；不会安装系统服务或开机启动项。脚本等待实际留言轮询成功后才显示就绪。`doctor` 的 `ok` 表示前提条件，`ready` 还要求监听进程存活、状态新鲜且最近轮询成功。停止桥接器：`scripts/stop.ps1`。停止监听器不会中断已被 DSH 接收的任务。

## 远程指令

| 指令 | 行为 |
|---|---|
| `/session 名称或ID` | 连接已有原会话，支持唯一 ID 前缀 |
| `/models` | 当前 DSH provider、模型和档位目录 |
| `/model provider/model [档位]` | 选择当前会话后续任务模型 |
| `/model auto` | 清除桥接器覆盖，后续任务使用本机默认模型 |
| `/effort`、`/effort 档位`、`/effort auto` | 查看、选择或恢复默认思考档位 |
| `/steer 内容` | 同轮引导；确认空闲时作为下一条消息 |
| `/queue 内容`、`/queue` | 排队或查看等待消息 ID |
| `/cancel`、`/cancel 消息ID`、`/cancel all` | 撤回尚未提交给 DSH 的等待消息 |
| `/approvals` | 本 Issue 当前待确认请求 |
| `/allow 完整请求ID` | 仅允许这一次 |
| `/deny 完整请求ID` | 拒绝这一次 |
| `/status`、`/help` | 状态和用法 |
| `/close` | 关闭当前 Issue |

普通留言默认排队。只接受任务仓库所有者创建的 Issue 和评论。模型／档位覆盖按原会话隔离；等待消息保存发送时的选择，默认路由变化后覆盖失效。同轮引导沿用当前执行模型。

## 权限确认

连接到 GitHub 的会话，在监听器心跳有效时将权限确认交给远端；未连接的会话仍使用原本机确认。子代理请求回到父会话的 Issue。请求必须同时匹配完整 ID、会话、Issue 和宿主实例，过期或重复冲突操作会拒绝。没有“全部允许”、永久授权或自动批准。

审批最长等待 15 分钟，超时拒绝。监听器心跳中断后，等待请求会交还本机确认；新请求直接交给原本机确认。宿主重启、任务取消或插件卸载会使旧请求失效。每次回执明确区分“DSH 已确认”和“回执暂未确认”。

已有权限请求发生后才连接会话，不能抢接已经由本机弹窗持有的旧请求；请在任务开始前连接会话。DSH 的自动审查、安全拒绝和沙盒继续生效，本项目只处理宿主真正发出的 `approval/request`。

如果原会话采用 `never` 审批策略，DSH 会在生成权限卡之前直接拒绝相关操作。连接回执会明确提示该状态；桥接器不把它伪装成“等待远端允许”，也不会自行改成 `ask`。

## 验证与限制

查看 [验证记录](docs/VALIDATION.md)、[架构](docs/ARCHITECTURE.md)、[配置](docs/CONFIGURATION.md) 和 [安全说明](SECURITY.md)。DSH 内部接口仍在变化，本版本对模型选择使用一个经过版本检查的单会话接口；升级 DSH 后先运行 `doctor`。

历史仅导出可见用户和助手文本，不导出内部推理、工具输出、系统提示或认证配置。脱敏属于尽力处理。`watch --no-publish` 可关闭历史上传，任务回复和权限卡继续写入私有 Issue。

历史同步在独立工作线程中进行，不能阻塞留言和权限决定。最终回复明确引用的本机图片／文档，若位于会话工作目录或配置工作目录且不超过 10 MB，会发布到私有任务仓库并替换成本机之外可访问的链接；认证配置文件和工作目录之外的文件不自动发布。

```powershell
python -m unittest discover -s tests -v
node --test tests/host.test.mjs
python scripts/sync_host.py --check
python scripts/audit_public.py
python -m pip wheel --no-deps -w dist .
```

许可证：MIT。原项目来源固定为 `codex2github` 提交 `cab8038349055fadee5b737b0cea154af0fee9f4`；复用了其独立完成通知和发布审计工具，其余宿主及路由实现适配 DSH。

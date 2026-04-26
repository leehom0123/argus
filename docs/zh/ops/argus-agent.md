# Argus Agent

**Argus Executor**（#103）由两半组成。Argus 这边管 `/api/agents/*`
端点和 rerun / stop 命令队列。训练机这边跑 `argus-agent` —— 一个
小守护进程，从 Argus 拉任务，在本机起子进程。

这页是给在某台主机上装 agent 的运维看的。如果只是想用 SDK 跑训练
脚本，看 [接入训练任务](../getting-started/connect-training.md)
就够了 —— agent 是可选项。

## Agent 干什么

跑着 `argus-agent` 的主机能从 Argus 接两类命令：

- **Rerun** —— 用户在某个跑完的批次上点 *Rerun* 时，Argus 把一条
  `AgentCommand` 入队，目标是当初跑这个批次的那台主机。Agent
  把它出队、起原命令行、然后回去继续轮询。新进程通过普通的
  reporter SDK 跟 Argus 通信。
- **Stop** —— 用户对运行中的批次点 *Stop* 时，进程内的 reporter
  本来就会轮询 `/stop-requested`（5 秒一次）。Agent 这条路只兜
  reporter 还没起来的极端情况（比如 agent 起了进程但还没到第一个
  `Reporter.__enter__`）。

Agent 本身不掌握 metrics 或事件。它一旦把子进程起起来，就交给
进程内的 reporter 接管。这样 agent 的攻击面就很小。

## 安装

Agent 跟 reporter SDK 同一个 wheel：

```bash
pip install argus-reporter
```

`argus-reporter`（保留兼容的 no-op 入口）和 `argus-agent` 都会装成
console script。

```bash
argus-agent --version
```

Python 3.10+ 即可，没有深度学习依赖；只有 `requests` 和标准库。

## 注册 agent

注册是一次性的引导：运维在主机上、登着 Argus 时跑 `register`，
用自己的 **用户 JWT** 把这台主机认领下来，换回一个长期有效的
`agent_token`，之后守护进程拉任务都用它。

```bash
argus-agent register \
  --argus-url https://argus.example.com \
  --hostname gpu-01.local \
  --token <你的-用户-JWT>
```

具体过程：

1. 调 `POST /api/agents/register`，body 是
   `{hostname, capabilities, version}`。
2. Argus 生成一个新的 `ag_live_…` token，**只返回一次**。
3. Agent 把 token 写到 `~/.argus-agent/token.json`，mode `0600`：

    ```json
    {
      "argus_url": "https://argus.example.com",
      "agent_id": "agent-7d3a1e8b9c2f",
      "agent_token": "ag_live_xxxxxxxxxxxxxxxxxxxxxxxx",
      "poll_interval_s": 10
    }
    ```

4. 守护进程之后只用 agent token 鉴权。

如果重启时 token 文件丢了，对同一个 hostname 重新跑一次
`argus-agent register` 即可。Argus 会原地轮换 token —— 旧明文
立刻失效。

!!! note "用户 JWT vs agent token"
    用户 JWT 只用在一次性的 `register` 调用上。长跑的守护进程
    根本看不到它。Agent token 被偷 → 攻击者能在 **这一台主机** 上
    起命令；JWT 被偷 → 攻击者就是这个用户。让 JWT 短命（它跟用户
    session 一起过期），让 agent token 通过重新 register 轮换。

## 跑守护进程

```bash
argus-agent run
```

守护进程做的事：

- 加载 `~/.argus-agent/token.json`。
- 每 `poll_interval_s`（默认 10 秒）轮询一次
  `GET /api/agents/{agent_id}/jobs`。
- 对每条 pending 命令，起子进程，再调
  `POST /api/agents/{agent_id}/jobs/{cmd_id}/ack` 上报。
- 每 30 秒发一次
  `POST /api/agents/{agent_id}/heartbeat`，让 Argus 仪表盘能在
  主机旁边显示 *agent 在线: 是/否*。

`Ctrl+C` 停 agent；正在跑的子进程不受影响（它们由 reporter
自己驱动生命周期）。

### 调间隔

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--poll-interval` | 服务器建议（10 秒） | 多久拉一次任务。Argus 在 register 响应里给出建议值。 |
| `--heartbeat-interval` | 30 秒 | 多久顶一次 `last_seen_at`。 |
| `--max-concurrent` | 4 | Agent 同时最多带几个子进程，超的排队。 |

## Systemd 单元

如果一台主机要常驻 agent，把单元文件放到
`/etc/systemd/system/argus-agent.service`：

```ini
[Unit]
Description=Argus Executor agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ml
WorkingDirectory=/home/ml
Environment=PATH=/home/ml/.local/bin:/usr/bin:/bin
ExecStart=/home/ml/.local/bin/argus-agent run
Restart=on-failure
RestartSec=10s

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now argus-agent
sudo journalctl -u argus-agent -f
```

要按用户装（不要 root），`systemctl --user` 用法一样 —— 单元放
`~/.config/systemd/user/argus-agent.service`，再
`loginctl enable-linger ml`。

## 验证

守护进程起来后，Argus 的 *Hosts* 页会在已注册的 hostname 旁边
显示绿色 **agent 在线** 标。在这台主机上跑过的任意一个跑完的
批次上点 *Rerun* —— 大约 10 秒内应该看到一行新的 `running` 出现，
agent 的 journal 里也会打一行 `subprocess.Popen`。

## 排错

### Agent 不接 rerun

1. 看仪表盘上 *agent 在线: 是* 没显示。没的话，说明守护进程没
   heartbeat —— 重启 systemd 单元，看
   `journalctl -u argus-agent -n 100`。
2. 确认 rerun 批次的 `host_id` 跟注册的 agent 对得上。Argus 只把
   命令发给原批次所在主机。如果批次最初跑在 `gpu-01`，你又用
   `gpu-02` 重新注册，rerun 会一直在队列里等 `gpu-01`。
3. 确认 `register` 时的用户 JWT 是创建批次的同一个用户 —— agent
   只能看到自己拥有的主机上的命令。

### Token 失效 / poll 401

Agent token 自己不会过期，但同一个 hostname 在别处重新 register
会把它轮换掉。重新跑 `argus-agent register` 拿一个新的。

### host_id 弄错

如果 `~/.argus-agent/token.json` 在多台主机间被复制（或同一个
hostname 被复用到不同机器），Argus 会把任务发给最后注册的那台。
给每台主机一个独立的 `--hostname`，或直接改主机名。

### Rerun 时 env_snapshot 过期

几个月前的批次可能 `cwd=/data/old-path` 已经不存在了。Agent
照着命令跑，`subprocess.Popen` 立刻失败 —— Argus 把 rerun 标成
`failed`，`failure_reason=stale_env_path`。Agent 不会自动 "修"
过期路径。点 *Rerun* 之前，在 RerunModal 的 overrides 里改一下
路径。

### 子进程跑错 Python / conda 环境

Agent 拿 `env_snapshot.command` 里的命令字符串原样跑。如果原来
是 `conda activate ts && python …`，agent 把整串经
`subprocess.Popen(shell=True)` 交给 shell 处理，所以激活逻辑
是 shell 干的。原来跑完之后又改了 conda 环境名的话，rerun 会
失败 —— 在 rerun overrides 里改。

## 安全

Agent 通过 `subprocess.Popen(shell=True)` 跑
`env_snapshot.command` 里存的命令行。**信任模型是：当初 POST
这个批次的那个 SDK token 持有者，被信任能在这台主机上起命令。**
那个持有者把一段 `command` 字符串交给了 Argus；Argus 现在把它
原样返回给 agent 执行。没有特权升级 —— agent 以 systemd 单元里
`User=` 的身份跑。

具体规矩：

- **不要** 把 agent 装在不该跑任意用户代码的主机上。
- **不要** 把 agent token 给不可信的人。拿到 token 的任何人都能
  在 Argus 上入队命令，让守护进程在主机上跑。
- Agent token 存在 `~/.argus-agent/token.json`，权限 `0600`。
  保住这个权限位。Systemd 单元的 `User=` 必须跟文件 owner 是
  同一个 UID。
- 轮换 agent token：重新 `argus-agent register`，旧明文立即失效。
- 在 Argus 那边吊销：*Hosts → Revoke agent*（管理员按钮）把
  这行标成 revoked，下一次 agent 拉任务会 401，守护进程退出。

端点表面故意做得小：register、poll、ack、heartbeat。Agent 不开
远程 shell，不收文件上传，不接受任何不是 Argus 提前入队的命令。

## API 参考（给插件作者）

| Method | Path | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `POST` | `/api/agents/register` | 用户 JWT | 一次性。返回 `agent_token`，仅返回一次。 |
| `GET` | `/api/agents/{id}/jobs` | Agent token | 返回这台主机的 pending `AgentCommand`。 |
| `POST` | `/api/agents/{id}/jobs/{cmd_id}/ack` | Agent token | 把命令标成 in-flight，记录子进程 PID。 |
| `POST` | `/api/agents/{id}/heartbeat` | Agent token | 顶 `last_seen_at`，204 No Content。 |

详细 schema 在 `backend/backend/api/agents.py`，对应的
`AgentCommand` / `AgentHost` SQLAlchemy 模型。命令的 `kind`
目前是 `rerun` 或 `stop` 两种。

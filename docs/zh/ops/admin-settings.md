# 管理员设置

Argus 把运维参数分两层：少量的启动期 env 变量，加一组在 UI 里
实时可改的 DB-backed 配置项。容器启动 **只需要** `ARGUS_JWT_SECRET`，
其余都可以在 *Settings → Admin* 里设，重启后保留。

## 哪些东西在 DB 里

`system_config` 表按 `(group, key)` 索引，存字符串。今天发出来
五个组：

| Group | Keys | 说明 |
| --- | --- | --- |
| `oauth.github` | `client_id`, `client_secret` | 在登录页加 *Sign in with GitHub* 按钮。 |
| `oauth.google` | `client_id`, `client_secret` | 同上，Google。 |
| `smtp` | `host`, `port`, `user`, `password`, `from_addr`, `tls` | 启用邮件通道。没设之前邮件规则只往 stdout 打日志。 |
| `retention` | `events_days`, `logs_days`, `soft_deleted_days` | 每日保留任务的上限。 |
| `feature` | `demo_project_enabled`, `public_share_enabled`, `signup_open` | Feature flag。 |

读取走 `runtime_config.get_config(group, key)`，优先级：

1. 有 **DB 行** 就用它。
2. 否则用 **匹配的 `ARGUS_*` 环境变量**（比如 `smtp.host` 对应
   `ARGUS_SMTP_HOST`）。
3. 都没有用 **内置默认值**。

意思是：原本只用 `.env` 的部署不动新面板也能继续跑 —— 环境变量
一直生效，直到管理员在 DB 里覆盖它们。

## 加密的密钥存储

密钥值 —— `oauth.github.client_secret`、`oauth.google.client_secret`、
`smtp.password` —— 用 Fernet 加密落盘，每个读 API 都 **掩成
`"***"`**。明文只在管理员往表单里输入的那一刻存在。

Fernet 密钥从两个环境变量里派生，按这个顺序：

| 环境变量 | 状态 |
| --- | --- |
| `ARGUS_CONFIG_KEY` | **首选**。32+ 随机字符。跟 JWT 解耦。 |
| `ARGUS_JWT_SECRET` | 兜底。只在 `ARGUS_CONFIG_KEY` 没设时用。 |

### 轮换炸点

!!! danger "先设 ARGUS_CONFIG_KEY，再轮换 JWT"
    在你设 `ARGUS_CONFIG_KEY` 之前，加密的密钥是绑在
    `ARGUS_JWT_SECRET` 上的。这时去轮换 JWT secret，会把
    `system_config` 里所有加密行作废 —— 密文解不开了，OAuth /
    SMTP 那块退回到兜底链（环境变量，再默认）。

    存在加密行而 `ARGUS_CONFIG_KEY` 没设时，启动会打一行明显的
    警告。计划任何 secret 轮换之前都先看这条警告。

**安全的轮换步骤** 是手动的 —— Argus **不会** 自动重新加密。
启动那行警告只是记录"现在用着 JWT 兜底"，并不会迁移任何行。
你得在轮换 JWT **之前** 自己把密钥从面板里走一遍。

1. **轮换之前** —— 先生成一个 32+ 字符的 `ARGUS_CONFIG_KEY`，
   写进 `deploy/.env`，然后重启 Argus 一次：

    ```bash
    python -c "import secrets; print(secrets.token_urlsafe(48))"
    ```

    重启之后，**新写入** 用 `ARGUS_CONFIG_KEY` 加密；**旧密文**
    仍然能解，因为读路径先试 `ARGUS_CONFIG_KEY`，解不开再退回
    用 `ARGUS_JWT_SECRET` 读切换前写的旧行。
2. **逐个手动重新保存** —— 在 *Settings → Admin* 里把本页顶部
   表里标了加密的每一行（`oauth.github.client_secret`、
   `oauth.google.client_secret`、`smtp.password`）都重新填一遍。
   每次保存会用当前 Fernet key 走一圈，等于在
   `ARGUS_CONFIG_KEY` 下重新加密。表单里已经设过的值显示成
   `***`；把真实值再敲一遍点 *Save*。
3. **验证** 每一项加密密钥在 *Settings → Admin* 里都还能读
   出来，并且对应功能（OAuth 登录按钮、*Send test email*）端
   到端还能用。
4. **现在** 想轮换 `ARGUS_JWT_SECRET` 都行。用户 session 全部
   失效，但加密密钥行因为现在绑在 `ARGUS_CONFIG_KEY` 上，仍然
   可读。

!!! danger "已经错误轮换过怎么办"
    如果在还没设 `ARGUS_CONFIG_KEY`、也还没逐项重新保存之前，
    就把 `ARGUS_JWT_SECRET` 轮换掉了 —— 那些加密行就是不可
    恢复的密文。**没有恢复路径** —— Argus 不留旧 key 的副本。
    必须从 *Settings → Admin* 把每一项加密密钥重新填一遍
    （清掉旧行，存新值）。这是一次性的数据丢失场景，按这个
    严重程度对待。

## 管理员面板

打开 *Settings → Admin*（只有 `admin` 角色用户能看）。五个可
折叠区对应上面五个组：

### GitHub OAuth

填 GitHub OAuth App 的 Client ID + Client Secret。Argus 把
redirect URI 拼成
`<ARGUS_BASE_URL>/api/auth/oauth/github/callback`。保存后几秒内
登录页就出 *Sign in with GitHub* 按钮 —— 不用重启。

### Google OAuth

形状一样；redirect URI 是
`<ARGUS_BASE_URL>/api/auth/oauth/google/callback`。记得把这个
URI 原样加到 Google Cloud Console 的 *Authorized redirect URIs*
里。

### SMTP

主机、端口、用户、密码、from 地址、TLS 开关。底部一个 *发送
测试邮件* 按钮，给管理员的已验证地址发一行测试邮件 —— 在全平台
启用通知之前先确认凭证好用。

### 保留策略

`events_days` / `logs_days` / `soft_deleted_days` 直接给每日保留
任务用。小一点磁盘小，但历史曲线短；大一点仪表盘信息多，代价是
`monitor.db` 大。

### Feature flag

| Flag | 效果 |
| --- | --- |
| `demo_project_enabled` | 是否渲染只读的 `/demo` 页。 |
| `public_share_enabled` | 是否显示 *Share → Create public link*。 |
| `signup_open` | 关掉就停止新账号注册；存量账号不受影响。 |

## 审计日志

每次管理员保存都写一条 `audit_log`，记录操作者 user ID、
`(group, key)` 对、时间戳。密钥类的字段 **不** 记录值的 diff ——
只记录这个 key 改过。审计日志在 *Settings → Admin → Audit log*
里看。

## 环境变量 vs DB 行：什么时候用哪个

| 场景 | 用 |
| --- | --- |
| 单机个人部署 | `.env`。引导起来快，不用 UI 来回。 |
| 多管理员的团队部署 | DB 行。让非 root 管理员不用 SSH 也能轮换 SMTP。 |
| 离线 / 不可变容器 | `.env` + 只读 DB。兜底链照样工作。 |
| CI / 临时 dev 容器 | `.env`。Migration 重头跑，没东西要设。 |

两个不是互斥 —— DB 行按组覆盖环境变量。常见模式是：把非密钥
默认值烤进 `.env`，密钥那部分从面板里在线覆盖。

## 相关阅读

- [配置项](configuration.md) —— 完整环境变量参考，包括 DB 面板
  在没填时会兜底到的那些变量。
- [通知](../user-guide/notifications.md) —— SMTP 配好后启用什么。
- [备份与保留](retention.md) —— 在这里设的保留值驱动每日任务。

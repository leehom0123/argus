# 配置项

每个旋钮都是环境变量 —— 服务端不读 YAML 配置文件。生产环境的标准
做法是把它们都写到 compose 的 `deploy/.env` 里。

## 核心

| 变量 | 必填 | 默认值 | 备注 |
| --- | --- | --- | --- |
| `ARGUS_JWT_SECRET` | 是 | — | 32+ 随机字节；轮换后所有 session 失效 |
| `ARGUS_DB_URL` | 否 | `sqlite+aiosqlite:////app/data/monitor.db` | 见 [数据库](database.md) |
| `ARGUS_BASE_URL` | 否 | `http://localhost:8000` | 邮件和分享链接里拼绝对 URL 用 |
| `ARGUS_LOG_LEVEL` | 否 | `info` | `debug` / `info` / `warning` / `error` 之一 |
| `ARGUS_ENV` | 否 | `prod` | `dev` 启用 FastAPI 自动重载与详细错误页 |

## SMTP（邮件通知）

| 变量 | 默认值 | 备注 |
| --- | --- | --- |
| `ARGUS_SMTP_HOST` | 未设 | 不设时邮件规则改打 stdout 而不是真发 |
| `ARGUS_SMTP_PORT` | `587` | STARTTLS 端口 |
| `ARGUS_SMTP_USER` | 未设 | |
| `ARGUS_SMTP_PASS` | 未设 | 推荐用应用专用密码 |
| `ARGUS_SMTP_FROM` | `noreply@example.com` | 出站邮件的 From |

## Webhook

| 变量 | 备注 |
| --- | --- |
| `ARGUS_FEISHU_WEBHOOK` | 完整的飞书 / Lark 入站 webhook URL |

其它通道（Slack、Discord、通用 webhook）已列入 roadmap，但还没接好。

## CORS 与嵌入

| 变量 | 默认值 | 备注 |
| --- | --- | --- |
| `ARGUS_CORS_ORIGINS` | 从 `ARGUS_BASE_URL` + Vite dev origin 派生 | 逗号分隔白名单 |
| `ARGUS_EMBED_ORIGINS` | 未设 | 允许 iframe 公开链接的宿主白名单 |

## 鉴权

| 变量 | 默认值 | 备注 |
| --- | --- | --- |
| `ARGUS_REGISTRATION` | `open` | `open` / `invite` / `closed` |
| `ARGUS_OAUTH_GITHUB_CLIENT_ID` | 未设 | 可选 GitHub OAuth |
| `ARGUS_OAUTH_GITHUB_CLIENT_SECRET` | 未设 | |
| `ARGUS_OAUTH_GOOGLE_CLIENT_ID` | 未设 | 可选 Google OAuth |
| `ARGUS_OAUTH_GOOGLE_CLIENT_SECRET` | 未设 | |

启用 OAuth 后，登录页会出现对应按钮。本地账号密码登录仍然可用。

## 保留期

完整说明见 [备份与保留策略](retention.md)。

| 变量 | 默认值 | 备注 |
| --- | --- | --- |
| `ARGUS_RETAIN_EVENTS_DAYS` | `90` | 每日任务删除超期的原始 `event` 行 |
| `ARGUS_RETAIN_LOGS_DAYS` | `30` | 超期的 `log_line` 行被删 |
| `ARGUS_RETAIN_SOFT_DELETED_DAYS` | `30` | 软删除行超期后会变成硬删除 |

星标批次和置顶批次跳过保留期，永久保存。

## 运行时关掉 SDK

SDK 端只看一个变量：

| 变量 | 行为 |
| --- | --- |
| `ARGUS_DISABLE` | 设为 `1` / `true` 时，reporter 静默吃掉所有 emit |

适合在不该打面板的单元测试里用。

# 安装

最快上手的方式是仓库自带的 Docker Compose 配置。它构建一个镜像，
挂一个 SQLite 卷，然后把面板暴露在 8000 端口。不需要外置数据库，
也不需要消息总线。

## 前置条件

- Docker 24+，自带 Compose v2
- Linux、macOS 或 WSL2
- 默认 SQLite 部署需要 1 GB 空闲内存、2 GB 磁盘
- 反向代理（nginx / Caddy / Traefik）建议配，用来做 TLS；本地体验
  时可以先不配

## Docker 一分钟上手

```bash
git clone https://github.com/leehom0123/argus
cd argus/deploy
cp .env.example .env  # 给 ARGUS_JWT_SECRET 填一个长随机串
docker compose up -d
```

打开 <http://localhost:8000>，注册第一个账号 —— 第一个注册的用户
自动是管理员。

!!! warning "暴露到外网前一定要改 JWT 密钥"
    `ARGUS_JWT_SECRET` 是容器唯一必填的环境变量。没设的话 Compose
    会拒绝启动。至少 32 字节的随机串：

    ```bash
    python -c "import secrets; print(secrets.token_urlsafe(48))"
    ```

## 装了什么

| 路径 | 作用 |
| --- | --- |
| `monitor` 容器 | FastAPI 后端，同时提供 `/api/*` 与已构建的 Vue 面板 |
| `./data/monitor.db`（宿主机） | SQLite 数据库，重启容器后仍然存在 |
| `./data/artifacts/`（宿主机） | 上传的图与报告 |
| 端口 `8000` | 面板 + API。生产部署在前面套一层 nginx 做 HTTPS。 |

## 安装 Python SDK

SDK 是 PyPI 上的独立包，只装在跑实验的机器上即可：

```bash
pip install argus-reporter
```

它不依赖 PyTorch / TensorFlow，所以加到任何训练环境里都安全。

## 接下来

- [首次运行](first-run.md) —— 注册账号、创建 token、用 `curl`
  发一条事件。
- [接入训练任务](connect-training.md) —— 把 SDK 接到一个真实的
  训练脚本里。

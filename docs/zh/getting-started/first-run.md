# 首次运行

这一节把流程跑到底：从一个刚启动的容器，到第一条事件出现在面板上。
全程用 `curl`，所以不需要先配 Python 环境。

## 1. 注册管理员账号

打开 <http://localhost:8000>，点 **Register**。填邮箱和密码。第一个
注册的用户自动是管理员；之后注册的就是普通用户。

登录后，面板上批次为 0、任务为 0、GPU 面板为空 —— 这是正常的。

## 2. 创建一个 reporter token

进 **Settings → Tokens → Create token**。

- **Name：** `local-test`（标签随便起）
- **Scope：** `reporter`

完整的 token（`em_live_…`）只显示一次。现在就复制下来 —— UI 只存
了哈希，过后拿不回来。

!!! tip
    日常使用建议把 token 写到 shell 的 rc 文件里：

    ```bash
    export ARGUS_URL=http://localhost:8000
    export ARGUS_TOKEN=em_live_xxxxxxxxxxxxxxxx
    ```

    SDK 和下面的 curl 例子都会自动读。

## 3. 发一条事件

```bash
EVENT_ID=$(python -c "import uuid;print(uuid.uuid4())")
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

curl -s -X POST "$ARGUS_URL/api/events" \
  -H "Authorization: Bearer $ARGUS_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"event_id\":\"$EVENT_ID\",
    \"schema_version\":\"1.1\",
    \"event_type\":\"batch_start\",
    \"timestamp\":\"$TS\",
    \"batch_id\":\"hello-world\",
    \"source\":{\"project\":\"demo\"},
    \"data\":{\"experiment_type\":\"forecast\",\"n_total\":1}
  }"
```

预期返回：

```json
{"db_id": 1, "deduplicated": false}
```

## 4. 在面板上看到它

刷新面板。应该能看到一个名为 `hello-world` 的运行中批次，预留了一个
slot（`n_total=1`）。Active Batches 表里有 source project（`demo`）
和时间戳。

如果看不到：

- 浏览器 Network tab 里看 `/api/events` 是否有错。
- 同一个 `event_id` 重发会返回 `deduplicated: true` —— 这是设计如此。
  每条想入库的事件都用新 UUID。
- 看容器日志：`docker compose logs -f monitor`。

## 5. 标记完成

```bash
EVENT_ID=$(python -c "import uuid;print(uuid.uuid4())")
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

curl -s -X POST "$ARGUS_URL/api/events" \
  -H "Authorization: Bearer $ARGUS_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"event_id\":\"$EVENT_ID\",
    \"schema_version\":\"1.1\",
    \"event_type\":\"batch_done\",
    \"timestamp\":\"$TS\",
    \"batch_id\":\"hello-world\",
    \"source\":{\"project\":\"demo\"},
    \"data\":{\"n_done\":1,\"n_failed\":0,\"total_elapsed_s\":1.0}
  }"
```

面板上批次状态会从 **Running** 翻到 **Completed**。

## 接下来

到这里已经把链路打通了。下一步看
[接入训练任务](connect-training.md)，用 Python SDK 跑同样的流程。

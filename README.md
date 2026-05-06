# gpt-image-2 Web Service

一个自用的 FastAPI 图片生成 Web 服务，通过 OpenAI 兼容接口调用第三方供应商的 `gpt-image-2` 模型。当前版本专注于稳定的文生图工作流，暂不包含图片编辑功能。

## 当前能力

- 密码登录：只需要密码，不需要账户。
- 文生图 Web UI：左侧参数，右侧生成结果和历史记录。
- 支持 6 个常用比例：`1:1`、`3:4`、`4:3`、`9:16`、`16:9`、`21:9`。
- 支持 `auto`、`high`、`medium`、`low` 质量选项。
- 支持 OpenAI 官方图片输出格式：`png`、`jpeg`、`webp`，默认 `png`。
- 支持 Count 数量控制；如果上游 provider 忽略 `n` 只返回 1 张，后端会自动补发请求，尽量拿够指定数量。
- Job 模式：关闭浏览器后，后端任务仍会继续执行。
- 生成中可以把当前 Job 转入后台运行，不影响继续提交下一个 prompt。
- 顶部状态按钮可以打开后台 Job 弹窗，查看正在排队或运行中的任务，并可进入某个 Job 的生成页。
- 前端可取消本地 Job。若请求已经发给上游，provider 仍可能在远端继续完成。
- 后端限制最多同时存在 5 个排队或运行中的 Job。
- 历史记录持久化：刷新页面后仍可查看历史出图和 prompt。
- 历史图片懒加载：打开网页时只加载元数据，不会一次性下载所有历史图片。
- 历史记录弹窗预览、打开原图、下载、复用 prompt、复制 prompt。
- 支持勾选历史记录后批量删除、批量下载 ZIP。
- 历史记录最多保留最近 30 个，超过后自动移除最旧历史；若旧图片仍被 Job 结果引用，会保留本地文件以避免 Job 详情失效。
- 日志记录完整请求参数、上游返回、异常、重试和耗时，方便排查失败原因。
- JSON 历史和 Job 文件读写带文件锁，降低并发写入导致的数据损坏风险。

说明：当前文件锁基于 `fcntl`，适合 Linux/macOS 部署；如果要在 Windows 上运行，需要改用跨平台文件锁库。

## 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`：

```env
OPENAI_API_KEY=你的第三方供应商 API Key
OPENAI_BASE_URL=https://你的供应商域名/v1
IMAGE_MODEL=gpt-image-2

IMAGE_SIZE=1024x1024
IMAGE_QUALITY=auto
IMAGE_OUTPUT_FORMAT=png
IMAGE_RESPONSE_FORMAT=b64_json
OUTPUT_DIR=outputs
LOG_DIR=logs
REQUEST_TIMEOUT_SECONDS=600
PROVIDER_MAX_ATTEMPTS=2

APP_PASSWORD=你的登录密码
APP_SESSION_SECRET=换成一串足够长的随机字符串
```

启动服务：

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开：

```text
http://127.0.0.1:8000
```

如果配置了 `APP_PASSWORD` 和 `APP_SESSION_SECRET`，会先进入登录页。

## 页面使用

1. 在左侧填写 prompt。
2. 选择比例和分辨率档位，默认是 `1:1` + `1K`。
3. 选择质量，默认质量是 `auto`。
4. 选择输出格式，默认格式是 `png`。
5. 点击生成后，右侧会显示当前 Job 进度和生成结果。
6. Count 大于 1 时，后端会尽量返回对应数量的图片；如果 provider 单次只返回 1 张，会自动补发剩余请求。
7. 生成中可以点击“后台运行”，当前任务继续在后端执行，表单会解锁，可以提交新的 prompt。
8. 顶部状态按钮会显示后台 Job 数量，点击后可以查看正在运行的任务列表，也可以取消任务。
9. 历史记录在右侧下方，点击某条历史可以打开弹窗查看图片、prompt、质量信息，并支持复用或复制 prompt。
10. 点击“选择”后可以勾选历史记录，进行全选、批量下载 ZIP 或批量删除。

## 环境变量

| 变量 | 说明 | 示例 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 第三方供应商 API Key | `sk-...` |
| `OPENAI_BASE_URL` | OpenAI 兼容接口地址，需要包含 `/v1` | `https://example.com/v1` |
| `IMAGE_MODEL` | 图片模型 | `gpt-image-2` |
| `IMAGE_SIZE` | 默认图片尺寸 | `1024x1024` |
| `IMAGE_QUALITY` | 默认质量 | `auto` |
| `IMAGE_OUTPUT_FORMAT` | 默认输出格式 | `png`、`jpeg` 或 `webp` |
| `IMAGE_RESPONSE_FORMAT` | 上游返回格式 | `b64_json` 或 `url` |
| `OUTPUT_DIR` | 图片、历史和 Job 数据保存目录 | `outputs` |
| `LOG_DIR` | 日志目录 | `logs` |
| `REQUEST_TIMEOUT_SECONDS` | 上游请求超时时间 | `600` |
| `PROVIDER_MAX_ATTEMPTS` | 上游 5xx、超时、断流错误最大尝试次数 | `2` |
| `APP_PASSWORD` | Web 登录密码 | 自行设置 |
| `APP_SESSION_SECRET` | Cookie 签名密钥 | 随机长字符串 |

如果 `APP_PASSWORD` 或 `APP_SESSION_SECRET` 没有设置，登录鉴权会关闭，启动时会写入一条 `auth_disabled_warning` 日志。

## 比例和尺寸

网页里先选择比例，再选择 `1K`、`2K` 或 `4K` 档位，实际提交给 provider 的仍然是具体尺寸：

| 比例 | 1K 尺寸 | 2K 尺寸 | 4K 尺寸 |
| --- | --- | --- | --- |
| `1:1` | `1024x1024` | `2048x2048` | `2880x2880` |
| `3:4` | `768x1024` | `1536x2048` | `2448x3264` |
| `4:3` | `1024x768` | `2048x1536` | `3264x2448` |
| `9:16` | `576x1024` | `1152x2048` | `2160x3840` |
| `16:9` | `1024x576` | `2048x1152` | `3840x2160` |
| `21:9` | `1008x432` | `2016x864` | `3808x1632` |

所有预设尺寸的宽高都能被 `16` 整除。自定义尺寸限制为 `16` 到 `4096`，宽高都必须能被 `16` 整除，且总像素不能超过 `3840x2160` 的像素预算（`8,294,400` 像素）。

质量选项：

- `auto`
- `high`
- `medium`
- `low`

历史记录里会分别展示“选择的质量”和“provider 实际返回的质量”。有些上游服务可能会自动调整实际质量。

输出格式选项：

- `png`
- `jpeg`
- `webp`

OpenAI 官方 GPT Image 模型默认返回 `png`，也支持通过 `output_format` 请求 `jpeg` 或 `webp`。如果上游返回 base64 图片，后端会按最终输出格式保存为对应扩展名：`.png`、`.jpg` 或 `.webp`。

## Job 模式

网页默认使用 Job 模式发起生成：

1. 前端调用 `POST /v1/jobs` 创建任务。
2. 后端在后台执行图片生成。
3. 前端轮询 `GET /v1/jobs/{job_id}` 查看进度。
4. 关闭浏览器不会取消任务，重新打开网页后会继续显示未完成任务。

生成中点击“后台运行”后，当前 Job 会继续在后端执行，表单会立即解锁，可以继续提交新的 prompt。后台任务完成后，网页会刷新历史记录。

后台 Job 列表会定时刷新，当前前端每 10 秒拉取一次 `/v1/jobs`。任务完成后会从后台运行列表移除。

后端最多允许 5 个 `queued` / `running` Job 同时存在；超过后会返回 `429 Too Many Requests`，错误类型是 `TooManyActiveJobs`。

取消任务：

```bash
curl -X POST http://127.0.0.1:8000/v1/jobs/JOB_ID/cancel
```

取消说明：

- 如果任务还没真正发给上游，可以直接本地取消。
- 如果请求已经发给上游，当前只会取消本地等待，上游 provider 可能仍会继续生成。
- 当前没有依赖 provider 的远程取消接口。
- 被取消的任务状态会写入 `outputs/jobs.json`，前端会显示取消说明。

## 历史记录

生成成功后会写入：

```text
outputs/history.json
```

本地图片默认保存到：

```text
outputs/
```

后台 Job 数据保存到：

```text
outputs/jobs.json
```

历史记录支持：

- 查看历史图片。
- 查看历史 prompt。
- 查看选择质量和实际质量。
- 使用历史 prompt 重新生成。
- 复制历史 prompt。
- 勾选后批量删除。
- 勾选后批量下载 ZIP。

历史记录使用稳定 UUID 作为 ID。批量下载的 ZIP 里会包含图片和对应的 `prompt.txt`。ZIP 文件会临时生成，响应结束后自动清理。

历史记录最多保留最近 30 个；生成第 31 个时，会自动移除最旧历史记录。若对应本地图片没有被 Job 结果引用，会一起删除；如果仍被某个 Job 的 `result` 引用，则会保留文件以避免 Job 详情里的图片链接失效。请及时下载需要长期保存的结果。

## 日志

应用日志保存到：

```text
logs/app.log
```

查看实时日志：

```bash
tail -f logs/app.log
```

日志是 JSONL 格式，每行一个事件。会记录：

- 创建 Job。
- 开始生成。
- 发给 provider 的参数。
- provider HTTP 状态码。
- provider 返回内容。
- provider 报错内容。
- 网络、TLS、超时、断流等异常。
- 重试次数和重试原因。
- 图片保存结果。
- 总耗时。

日志会自动隐藏 API Key，并会截断或隐藏大体积的 base64 图片内容。

如果使用 systemd 部署，也可以看服务日志：

```bash
journalctl -u image-cli -f
```

常见失败含义：

- `stream disconnected before completion`：上游流式响应中断，通常是 provider 端连接、网关或模型服务不稳定。
- `stream error: ... INTERNAL_ERROR`：上游 HTTP/2 或网关内部错误，属于 provider/server 侧错误。
- `502`：本服务访问 provider 失败，或者 provider 返回异常网关错误。
- `429 TooManyActiveJobs`：当前已有 5 个排队或运行中的 Job，需要等待或取消后再提交。

## HTTP API

如果开启了 `APP_PASSWORD` 和 `APP_SESSION_SECRET`，API 也需要先登录拿到 cookie：

```bash
curl -c cookies.txt -X POST http://127.0.0.1:8000/login \
  -H "Content-Type: application/json" \
  -d '{"password":"你的登录密码"}'
```

后续请求加上：

```bash
-b cookies.txt
```

### 健康检查

```bash
curl http://127.0.0.1:8000/health
```

### 立即生成

```bash
curl -X POST http://127.0.0.1:8000/v1/generate \
  -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "一张赛博朋克风格的上海夜景，雨后霓虹，高细节",
    "size": "1024x1024",
    "quality": "auto",
    "n": 1
  }'
```

返回示例：

```json
{
  "model": "gpt-image-2",
  "images": [
    {
      "index": 0,
      "url": null,
      "file": "/files/xxxx-0.png",
      "revised_prompt": null
    }
  ],
  "provider_response": {}
}
```

### 创建 Job

```bash
curl -X POST http://127.0.0.1:8000/v1/jobs \
  -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "一张电影感的雪山日出，超清细节",
    "size": "2048x1152",
    "quality": "auto",
    "n": 1
  }'
```

返回示例：

```json
{
  "id": "job_uuid",
  "status": "queued",
  "prompt": "一张电影感的雪山日出，超清细节",
  "payload": {
    "model": "gpt-image-2",
    "prompt": "一张电影感的雪山日出，超清细节",
    "size": "2048x1152",
    "quality": "auto",
    "n": 1,
    "response_format": "b64_json"
  },
  "created_at": 1710000000,
  "updated_at": 1710000000
}
```

### 查看全部 Job

```bash
curl -b cookies.txt http://127.0.0.1:8000/v1/jobs
```

### 查看 Job

```bash
curl -b cookies.txt http://127.0.0.1:8000/v1/jobs/JOB_ID
```

### 取消 Job

```bash
curl -X POST -b cookies.txt http://127.0.0.1:8000/v1/jobs/JOB_ID/cancel
```

### 查看历史

```bash
curl -b cookies.txt http://127.0.0.1:8000/v1/history
```

### 查看历史详情

```bash
curl -b cookies.txt http://127.0.0.1:8000/v1/history/HISTORY_UUID
```

### 删除单条历史

```bash
curl -X DELETE -b cookies.txt http://127.0.0.1:8000/v1/history/HISTORY_UUID
```

### 批量删除历史

```bash
curl -X POST http://127.0.0.1:8000/v1/history/delete \
  -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{"ids":["history_uuid_1","history_uuid_2"]}'
```

### 批量下载历史

```bash
curl -X POST http://127.0.0.1:8000/v1/history/download \
  -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{"ids":["history_uuid_1","history_uuid_2"]}' \
  -o history-images.zip
```

## 供应商兼容说明

- 请求路径是 `{OPENAI_BASE_URL}/images/generations`。
- 使用 Bearer Token：`Authorization: Bearer {OPENAI_API_KEY}`。
- 默认要求 provider 返回 `b64_json`，服务会保存到 `outputs/` 并通过 `/files/...` 访问。
- 如果 provider 返回 `url`，服务会尝试下载图片并保存；下载失败时仍会返回原始 URL。
- 请求体里的 `extra` 字段会合并到上游请求，可用于传 provider 的额外参数。
- 当前版本不接入 `/images/edits`，只保留文生图。

示例：

```json
{
  "prompt": "一张产品摄影图",
  "size": "1024x1024",
  "quality": "auto",
  "extra": {
    "自定义参数": "自定义值"
  }
}
```

## 服务器部署

推荐用 systemd 跑 FastAPI，再用 Nginx 做 HTTPS 反向代理。

示例 `/etc/systemd/system/image-cli.service`：

```ini
[Unit]
Description=Image CLI FastAPI Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/image-cli
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/image-cli/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now image-cli
sudo systemctl status image-cli
```

Nginx 反代示例：

```nginx
server {
    listen 80;
    server_name image.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name image.example.com;

    ssl_certificate /etc/letsencrypt/live/image.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/image.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 700s;
        proxy_send_timeout 700s;
    }
}
```

## 不要提交的文件

这些文件不应该上传到 GitHub：

- `.env`
- `.venv/`
- `outputs/`
- `logs/`
- `__pycache__/`
- `*.pyc`

当前 `.gitignore` 已经忽略这些内容。

# gpt-image-2 Web Service

一个自用的 FastAPI 图片生成 Web 服务，通过 OpenAI 兼容接口调用第三方供应商的 `gpt-image-2` 模型。当前版本专注于稳定的文生图工作流，暂不包含图片编辑功能。

## 功能

- 密码登录：只需要密码，不需要账户。
- 文生图 Web UI：左侧参数，右侧生成结果和历史记录。
- 支持 6 个常用比例：`1:1`、`3:4`、`4:3`、`9:16`、`16:9`、`21:9`。
- 支持 `auto`、`high`、`medium`、`low` 质量选项。
- Job 模式：关闭浏览器后，后端任务仍会继续执行。
- 前端可取消本地 Job。若请求已经发给上游，provider 仍可能在远端继续完成。
- 历史记录持久化：刷新页面后仍可查看历史出图和 prompt。
- 历史图片懒加载：打开网页时只加载元数据，不会一次性下载所有历史图片。
- 历史记录弹窗预览、打开原图、下载、复用 prompt。
- 支持勾选历史记录后批量删除、批量下载 ZIP。
- 日志记录完整请求参数、上游返回、异常、重试和耗时，方便排查失败原因。
- JSON 历史和 Job 文件读写带文件锁，降低并发写入导致的数据损坏风险。

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

## 环境变量

| 变量 | 说明 | 示例 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 第三方供应商 API Key | `sk-...` |
| `OPENAI_BASE_URL` | OpenAI 兼容接口地址，需要包含 `/v1` | `https://example.com/v1` |
| `IMAGE_MODEL` | 图片模型 | `gpt-image-2` |
| `IMAGE_SIZE` | 默认图片尺寸 | `1024x1024` |
| `IMAGE_QUALITY` | 默认质量 | `auto` |
| `IMAGE_RESPONSE_FORMAT` | 上游返回格式 | `b64_json` 或 `url` |
| `OUTPUT_DIR` | 图片、历史和 Job 数据保存目录 | `outputs` |
| `LOG_DIR` | 日志目录 | `logs` |
| `REQUEST_TIMEOUT_SECONDS` | 上游请求超时时间 | `600` |
| `PROVIDER_MAX_ATTEMPTS` | 上游 5xx、超时、断流错误最大尝试次数 | `2` |
| `APP_PASSWORD` | Web 登录密码 | 自行设置 |
| `APP_SESSION_SECRET` | Cookie 签名密钥 | 随机长字符串 |

如果 `APP_PASSWORD` 或 `APP_SESSION_SECRET` 没有设置，登录鉴权会关闭，启动时会写入一条 `auth_disabled_warning` 日志。

## 比例和尺寸

网页里展示的是比例，实际提交给 provider 的仍然是具体尺寸：

| 比例 | 实际尺寸 | 常见用途 |
| --- | --- | --- |
| `1:1` | `1024x1024` | 头像、图标、产品主图、社媒图片 |
| `3:4` | `1536x2048` | 半身人像、竖版海报、商品详情图 |
| `4:3` | `2048x1536` | 摄影感图片、室内场景、文章配图 |
| `9:16` | `1088x1920` | 手机壁纸、短视频封面、竖屏海报 |
| `16:9` | `1920x1088` | 宽屏壁纸、视频封面、横屏场景图 |
| `21:9` | `3360x1440` | 超宽电影感画面、游戏概念图、横幅 |

`9:16` 和 `16:9` 使用 `1088` 而不是 `1080`，是因为当前 provider 要求宽高都能被 `16` 整除。

质量选项：

- `auto`
- `high`
- `medium`
- `low`

历史记录里会分别展示“选择的质量”和“provider 实际返回的质量”。有些上游服务可能会自动调整实际质量。

## Job 模式

网页默认使用 Job 模式发起生成：

1. 前端调用 `POST /v1/jobs` 创建任务。
2. 后端在后台执行图片生成。
3. 前端轮询 `GET /v1/jobs/{job_id}` 查看进度。
4. 关闭浏览器不会取消任务，重新打开网页后会继续显示未完成任务。

取消任务：

```bash
curl -X POST http://127.0.0.1:8000/v1/jobs/JOB_ID/cancel
```

取消说明：

- 如果任务还没真正发给上游，可以直接本地取消。
- 如果请求已经发给上游，当前只会取消本地等待，上游 provider 可能仍会继续生成。
- 当前没有依赖 provider 的远程取消接口。

## 历史记录

生成成功后会写入：

```text
outputs/history.json
```

本地图片默认保存到：

```text
outputs/
```

历史记录支持：

- 查看历史图片。
- 查看历史 prompt。
- 查看选择质量和实际质量。
- 使用历史 prompt 重新生成。
- 勾选后批量删除。
- 勾选后批量下载 ZIP。

历史记录使用稳定 UUID 作为 ID。批量下载的 ZIP 里会包含图片和对应的 `prompt.txt`。

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
    "size": "1920x1088",
    "quality": "auto",
    "n": 1
  }'
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

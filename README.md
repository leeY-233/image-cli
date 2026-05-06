# gpt-image-2 Web Service

一个自用的 FastAPI 图片生成/编辑 Web 服务，通过 OpenAI 兼容接口调用第三方供应商的 `gpt-image-2` 模型。当前版本已经把 Generate 和 Edit 合并到同一个主页 Studio 中，独立 `/edit` 页面已移除。

## 功能概览

- 密码登录：只需要密码，不需要账户；未配置密码和 Session Secret 时会关闭鉴权。
- 统一主页 Studio：左侧选择 `Generate` 或 `Edit`，右侧展示当前结果、后台 Job 和对应模式的历史记录。
- 文生图：支持 prompt、比例、尺寸档位、质量、输出格式、数量和 Extra JSON。
- 图片编辑：支持上传 1 到 16 张源图，可选 mask，支持和生成一致的尺寸、质量、输出格式、数量和 Extra JSON。
- Job 模式：Generate 和 Edit 都通过后台 Job 执行，关闭浏览器后任务仍会继续。
- 后台任务：任务运行中可以转入后台，继续提交新的生成或编辑任务；最多同时存在 10 个排队或运行中的 Job。
- 本地取消：前端和 API 都支持取消本地 Job；如果请求已经发给上游，provider 仍可能继续完成。
- 历史记录：生成历史和编辑历史写入同一个 `history.json`，通过 `operation` 字段区分，前端会根据当前模式自动切换展示。
- 历史操作：支持查看、打开原图、下载、批量下载 ZIP、批量删除；生成历史和编辑历史都可以一键进入再次编辑。
- 历史元数据：记录图片文件大小、图片宽高规格、请求质量、实际质量、输出格式和状态。
- 文件保护：历史超过上限清理旧记录时，如果旧图片仍被 Job 或其他历史引用，会保留本地文件避免详情失效。
- 日志排查：JSONL 日志记录请求参数、上游响应、异常、重试、耗时和图片保存结果，并隐藏 API Key 和大体积 base64 内容。
- 并发安全：`history.json` 和 `jobs.json` 读写带 `fcntl` 文件锁，降低并发写入导致的数据损坏风险。

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
IMAGE_EDIT_IMAGE_FIELD=image[]
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

打开主页：

```text
http://127.0.0.1:8000
```

如果同时配置了 `APP_PASSWORD` 和 `APP_SESSION_SECRET`，会先进入登录页。独立 `/edit` 页面已经删除，图片编辑请在主页切换到 `Edit` 模式使用。

## 页面使用

### Generate 模式

1. 在左侧选择 `Generate`。
2. 输入 prompt。
3. 选择比例和分辨率档位，默认是 `1:1` + `1K`。
4. 选择质量、输出格式和 Count。
5. 如有 provider 自定义参数，在 Extra JSON 中填写合法 JSON 对象。
6. 点击生成后，右侧显示当前 Job 状态和生成结果。
7. 运行中可以点击后台运行，当前任务继续执行，表单立即解锁。

### Edit 模式

1. 在左侧选择 `Edit`。
2. 上传一张或多张源图，最多 16 张；支持 `png`、`jpeg`、`webp`。
3. 可选上传 mask。mask 用来告诉模型哪些区域可被重绘，具体语义取决于上游 provider。
4. 输入编辑 prompt。
5. 选择尺寸、质量、输出格式和 Count。
6. 如有 provider 自定义参数，在 Extra JSON 中填写合法 JSON 对象。
7. 点击编辑后，任务会进入 `/v1/edit/jobs` 后台 Job 流程。

Clear 按钮会按当前模式工作：Generate 模式只清空 prompt；Edit 模式会清空 prompt、源图、mask 和预览。

### 历史记录

- 主页历史会根据当前模式自动显示生成历史或编辑历史，不需要用户手动选择历史类型。
- 生成历史支持 `View`、`Edit`、删除、批量下载和批量删除。
- 编辑历史支持 `View`、`Edit`、删除、批量下载和批量删除。
- 生成历史详情里保留复用 prompt 和复制 prompt。
- 编辑历史详情里不显示复用/复制 prompt，而是显示再次编辑入口。
- 历史行和详情都会展示图片规格、文件大小、质量、格式、时间和状态。
- 从历史点击 `Edit` 会直接把该图作为编辑源图加载到 Edit 模式，避免先下载再上传。

刷新页面后会保持上一次选择的 `Generate` 或 `Edit` 模式。

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
| `IMAGE_EDIT_IMAGE_FIELD` | 图片编辑转发给 provider 的源图字段名。官方多图示例常用 `image[]`，部分兼容供应商可能需要 `image` | `image[]` |
| `OUTPUT_DIR` | 图片、历史和 Job 数据保存目录 | `outputs` |
| `LOG_DIR` | 日志目录 | `logs` |
| `REQUEST_TIMEOUT_SECONDS` | 上游请求超时时间，单位秒 | `600` |
| `PROVIDER_MAX_ATTEMPTS` | 上游 5xx、超时、断流错误的最大尝试次数；Generate 和 Edit 共用 | `2` |
| `APP_PASSWORD` | Web 登录密码 | 自行设置 |
| `APP_SESSION_SECRET` | Cookie 签名密钥 | 随机长字符串 |

鉴权规则：

- 只有同时设置 `APP_PASSWORD` 和 `APP_SESSION_SECRET` 时才会启用登录。
- 任意一个缺失都会关闭鉴权，并在日志里写入 `auth_disabled_warning`。
- 登录 Cookie 使用 `APP_SESSION_SECRET` 签名，当前过期时间为 7 天。

## 尺寸、质量和格式

网页里先选择比例，再选择 `1K`、`2K` 或 `4K` 档位，实际提交给 provider 的仍然是具体尺寸：

| 比例 | 1K 尺寸 | 2K 尺寸 | 4K 尺寸 |
| --- | --- | --- | --- |
| `1:1` | `1024x1024` | `2048x2048` | `2880x2880` |
| `3:4` | `768x1024` | `1536x2048` | `2448x3264` |
| `4:3` | `1024x768` | `2048x1536` | `3264x2448` |
| `9:16` | `576x1024` | `1152x2048` | `2160x3840` |
| `16:9` | `1024x576` | `2048x1152` | `3840x2160` |
| `21:9` | `1008x432` | `2016x864` | `3808x1632` |

自定义尺寸限制：

- 宽高范围都是 `16` 到 `4096`。
- 宽高都必须能被 `16` 整除。
- 总像素不能超过 `3840x2160` 的像素预算，也就是 `8,294,400` 像素。

质量选项：

- `auto`
- `high`
- `medium`
- `low`

输出格式选项：

- `png`
- `jpeg`
- `webp`

如果上游返回 base64 图片，后端会按最终输出格式保存为对应扩展名：`.png`、`.jpg` 或 `.webp`。历史记录里会分别展示“请求质量”和“provider 实际返回质量”，因为部分上游服务可能会自动调整实际质量。

## Job 模式

Generate 和 Edit 都走统一 Job 思想：

1. Generate 前端调用 `POST /v1/jobs` 创建任务。
2. Edit 前端调用 `POST /v1/edit/jobs` 创建任务。
3. 后端把任务写入 `outputs/jobs.json`，状态从 `queued` 进入 `running`。
4. 前端轮询 `GET /v1/jobs/{job_id}` 查看进度。
5. 任务完成后写入历史记录，并在结果区展示图片。
6. 关闭浏览器不会取消任务，重新打开网页后会继续显示未完成任务。

后台 Job 列表会定时刷新，当前前端每 10 秒拉取一次 `/v1/jobs`。后端最多允许 10 个 `queued` / `running` Job 同时存在；超过后会返回 `429 Too Many Requests`，错误类型是 `TooManyActiveJobs`。

取消任务：

```bash
curl -X POST http://127.0.0.1:8000/v1/jobs/JOB_ID/cancel
```

取消说明：

- 如果任务还没真正发给上游，可以直接本地取消。
- 如果请求已经发给上游，当前只会取消本地等待，上游 provider 可能仍会继续生成或编辑。
- 当前没有依赖 provider 的远程取消接口。
- 被取消的任务状态会写入 `outputs/jobs.json`，前端会显示取消说明。

## 历史和本地文件

生成和编辑成功后都会写入：

```text
outputs/history.json
```

后台 Job 数据保存到：

```text
outputs/jobs.json
```

图片文件默认保存到：

```text
outputs/
```

历史记录字段包括：

- `operation`：`generate` 或 `edit`。
- `prompt`：当次生成或编辑 prompt。
- `file` / `url`：本地文件地址或上游 URL。
- `size`：请求尺寸。
- `requested_quality`：请求质量。
- `actual_quality`：provider 返回的实际质量。
- `output_format`：输出格式。
- `file_size_bytes`：本地图片文件大小。
- `image_width` / `image_height` / `image_dimensions`：图片像素规格。
- `status`：`success` 或 `failed`。

删除历史记录时，后端会先从 `history.json` 移除记录，再删除对应本地图片文件；如果该文件仍被其他历史记录或 Job 结果引用，则会保留文件。当前删除不是通过 `deleted: true` 软删除实现，而是直接移除历史记录。

历史记录最多保留最近 50 个；写入第 51 个时，会自动清理最旧记录。请及时下载需要长期保存的结果。

批量下载的 ZIP 里会包含图片和对应的 `prompt.txt`。ZIP 文件会临时生成，响应结束后自动清理。

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
    "output_format": "png",
    "n": 1
  }'
```

### 立即图片编辑

```bash
curl -X POST http://127.0.0.1:8000/v1/edit \
  -b cookies.txt \
  -F image=@source.png \
  -F prompt="把背景替换成暖色摄影棚，保持主体轮廓和文字不变" \
  -F size=1024x1024 \
  -F quality=auto \
  -F output_format=png \
  -F n=1
```

多源图编辑：

```bash
curl -X POST http://127.0.0.1:8000/v1/edit \
  -b cookies.txt \
  -F image=@source-a.png \
  -F image=@source-b.png \
  -F prompt="把两张图中的人物合成到同一个摄影棚场景里" \
  -F size=1024x1024 \
  -F output_format=png
```

注意：这里的 `image` 是本服务接收上传文件的字段名；`IMAGE_EDIT_IMAGE_FIELD` 控制的是本服务转发给上游 provider 时使用的字段名。

可选 mask：

```bash
curl -X POST http://127.0.0.1:8000/v1/edit \
  -b cookies.txt \
  -F image=@source.png \
  -F mask=@mask.png \
  -F prompt="只重绘透明区域，添加柔和自然光" \
  -F size=1024x1024
```

Extra JSON 示例：

```bash
curl -X POST http://127.0.0.1:8000/v1/edit \
  -b cookies.txt \
  -F image=@source.png \
  -F prompt="保留主体，替换为产品海报背景" \
  -F extra='{"provider_custom_option":"value"}'
```

### 创建生成 Job

```bash
curl -X POST http://127.0.0.1:8000/v1/jobs \
  -b cookies.txt \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "一张电影感的雪山日出，超清细节",
    "size": "2048x1152",
    "quality": "auto",
    "output_format": "png",
    "n": 1
  }'
```

返回示例：

```json
{
  "id": "job_uuid",
  "operation": "generate",
  "status": "queued",
  "prompt": "一张电影感的雪山日出，超清细节",
  "payload": {
    "model": "gpt-image-2",
    "prompt": "一张电影感的雪山日出，超清细节",
    "size": "2048x1152",
    "quality": "auto",
    "output_format": "png",
    "n": 1,
    "response_format": "b64_json"
  },
  "created_at": 1710000000,
  "updated_at": 1710000000
}
```

### 创建图片编辑 Job

```bash
curl -X POST http://127.0.0.1:8000/v1/edit/jobs \
  -b cookies.txt \
  -F image=@source.png \
  -F prompt="把背景替换成暖色摄影棚，保持主体轮廓和文字不变" \
  -F size=1024x1024 \
  -F quality=auto \
  -F output_format=png \
  -F n=1
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

- 生成请求路径是 `{OPENAI_BASE_URL}/images/generations`。
- 编辑请求路径是 `{OPENAI_BASE_URL}/images/edits`，使用 `multipart/form-data` 转发源图、可选 `mask` 和 prompt。
- 源图字段名默认是 `image[]`，可通过 `IMAGE_EDIT_IMAGE_FIELD=image` 适配部分兼容供应商。
- 使用 Bearer Token：`Authorization: Bearer {OPENAI_API_KEY}`。
- 默认要求 provider 返回 `b64_json`，服务会保存到 `outputs/` 并通过 `/files/...` 访问。
- 如果 provider 返回 `url`，服务会尝试下载图片并保存；下载失败时仍会返回原始 URL。
- 请求体或表单里的 `extra` 会合并到上游请求，可用于传 provider 的额外参数。
- Generate 和 Edit 使用同一套 provider 重试配置：`PROVIDER_MAX_ATTEMPTS`。
- 当前上传内容会读入 bytes 后再重试，因此 multipart 重试是安全的；如果未来改成流式上传，需要调整重试策略。

Extra JSON 示例：

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

## 日志和排错

应用日志保存到：

```text
logs/app.log
```

查看实时日志：

```bash
tail -f logs/app.log
```

日志是 JSONL 格式，每行一个事件。会记录：

- 登录鉴权状态。
- 创建 Job。
- 开始生成或编辑。
- 发给 provider 的参数。
- provider HTTP 状态码。
- provider 返回内容。
- provider 报错内容。
- 网络、TLS、超时、断流等异常。
- 重试次数和重试原因。
- 图片保存结果和图片元数据。
- 总耗时。

常见失败含义：

- `auth_disabled_warning`：没有同时设置 `APP_PASSWORD` 和 `APP_SESSION_SECRET`，当前服务未启用登录鉴权。
- `stream disconnected before completion`：上游流式响应中断，通常是 provider 端连接、网关或模型服务不稳定。
- `stream error: ... INTERNAL_ERROR`：上游 HTTP/2 或网关内部错误，属于 provider/server 侧错误。
- `502`：本服务访问 provider 失败，或者 provider 返回异常网关错误。
- `429 TooManyActiveJobs`：当前已有 10 个排队或运行中的 Job，需要等待或取消后再提交。

如果使用 systemd 部署，也可以看服务日志：

```bash
journalctl -u image-cli -f
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

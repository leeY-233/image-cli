# gpt-image-2 Web Service

一个轻量 FastAPI 服务，用 OpenAI 兼容接口调用第三方供应商的 `gpt-image-2` 图片生成模型。

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
```

启动服务：

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

打开 http://localhost:8000 可以直接在网页里测试。

## HTTP 调用

```bash
curl -X POST http://localhost:8000/v1/generate \
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

## 兼容说明

- 默认请求路径是 `{OPENAI_BASE_URL}/images/generations`。
- 默认使用 Bearer Token：`Authorization: Bearer {OPENAI_API_KEY}`。
- 默认要求供应商返回 `b64_json`，服务会保存到 `outputs/` 并通过 `/files/...` 暴露。
- 如果供应商只返回 `url`，服务会尝试下载图片并保存；下载失败时仍会返回原始 URL。
- 供应商的额外参数可以放在请求体的 `extra` 字段里，会合并到上游请求。

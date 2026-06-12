# CS2 饰品市场趋势情报 MVP

系统监控少量高价值外部信息，完成清洗、去重、事件聚合、市场影响判断、飞书紧急提醒和每日情报总结。它不采集饰品价格，也不提供自动交易或价格预测。

## 已实现能力

- Steam CS2 官方新闻、Steam 规则页面变化监控。
- YouTube Atom 白名单频道、Liquipedia 最近变更、授权后的 Reddit、通用 RSS 采集器。
- 带 SSRF 防护的人工 URL 补录。
- 原文精确去重、72 小时相似事件聚合和完整证据链。
- OpenAI 兼容结构化分析；无密钥时使用低置信度规则分析保持系统可用。
- 可解释重要性评分、P0/P1 飞书提醒、通知幂等和失败重试。
- 每天北京时间 08:30 生成日报。
- 只读情报列表、详情页、来源健康状态及 OpenAPI 接口。

## 本地运行

Python 3.12 及以上：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
uvicorn app.main:app --reload
```

默认配置使用项目目录下的 SQLite，适合本机验证。打开 `http://127.0.0.1:8000` 查看情报列表，`/docs` 查看接口文档。

首次启动会为规则页面建立正文基线，不会把已有规则误报为新事件。官方新闻会读取当前最近 30 条，去重后只保存一次；上线前可先保持飞书为空完成初始化。

## 日志

应用日志会同时输出到控制台和滚动文件，默认文件路径为 `logs/app.log`。可通过以下环境变量控制：

- `LOG_LEVEL=DEBUG|INFO|WARNING|ERROR`
- `LOG_TO_FILE=true|false`
- `LOG_FILE_PATH=logs/app.log`

本地查看日志：

```powershell
Get-Content .\logs\app.log -Wait
```

Docker 查看日志：

```powershell
docker compose logs -f app
```

## Docker Compose

```powershell
Copy-Item .env.example .env
# 修改 .env 中的 ADMIN_TOKEN、POSTGRES_PASSWORD、AI 和飞书配置
docker compose up --build -d
```

调度器是进程内实现，容器必须保持单 worker。需要水平扩容时，应先把调度任务拆为独立 worker 或增加分布式锁。

## 核心配置

- `AI_ENABLED=true` 后配置 `AI_BASE_URL`、`AI_API_KEY`、`AI_MODEL`。
- `FEISHU_WEBHOOK_URL` 配置群机器人；启用签名校验时同时设置 `FEISHU_SECRET`。
- `YOUTUBE_CHANNEL_IDS` 填写经过人工筛选的频道 ID，多个值用逗号分隔。
- `LIQUIPEDIA_ENABLED=true` 前必须设置真实 `CONTACT_EMAIL`，并遵守其 API 使用和限流要求。
- Reddit 仅在获得 API 权限后填写 OAuth 配置；系统不会通过匿名接口绕过授权。

人工补录示例：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/intake/url `
  -Headers @{"X-Admin-Token"="your-token"} `
  -ContentType "application/json" `
  -Body '{"url":"https://example.com/cs2-news"}'
```

## API

- `GET /api/events`：按 `alert_level`、`event_type`、`direction`、`start_at` 筛选。
- `GET /api/events/{id}`：查看结构化判断和证据链。
- `GET /api/reports/daily`：查看历史日报。
- `GET /api/sources/status`：查看来源最近成功时间和错误。
- `POST /api/intake/url`：使用 `X-Admin-Token` 补充高价值链接。
- `GET /health`：健康检查。

## 测试

```powershell
pytest
```

测试覆盖采集映射、规则页基线、清洗去重、事件聚合、硬触发提醒、通知幂等、日报缺口提示和人工 URL 安全边界。

## 重建事件

当聚合逻辑、评分逻辑或 AI 提示词发生变化时，只改代码不会自动修复已经落库的 `events` 结果，需要基于现有 `raw_items` 重建：

```powershell
# 重新请求 AI，并按最新归并/评分逻辑重建 events
python -m app.rebuild_events

# 只复用已有 analyses.result_json，跳过外部 AI 请求
python -m app.rebuild_events --reuse-analysis
```

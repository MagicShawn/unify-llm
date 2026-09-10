# Central Proxy 部署文档

本地多厂商 LLM 中转站：固定端口统一接入、模型路由、超时重试、并发监控与 Web Dashboard。

适用版本：`0.1.0`（本仓库）

---

## 1. 架构与部署形态

```
┌─────────────────┐     ┌──────────────────────────────┐     ┌─────────────────┐
│  OpenAI 客户端   │────▶│                              │────▶│ OpenAI / 兼容API │
│  /v1/chat/...   │     │   Central Proxy  :8787       │────▶│ DeepSeek/Ollama  │
└─────────────────┘     │                              │     └─────────────────┘
┌─────────────────┐     │  · 模型路由 / 别名            │     ┌─────────────────┐
│ Anthropic 客户端 │────▶│  · 超时 / 重试 / fallback     │────▶│ Anthropic API    │
│  /v1/messages   │     │  · 并发监控 / Dashboard       │     └─────────────────┘
└─────────────────┘     └──────────────────────────────┘
                               │
                        浏览器 /dashboard
```

| 项目 | 说明 |
|------|------|
| 默认监听 | `127.0.0.1:8787`（仅本机） |
| 进程模型 | 单进程 Uvicorn（async） |
| 配置 | `config.yaml`（YAML + `${ENV}` 展开） |
| 状态 | 进程内存，不落盘；重启后计数清零 |
| 日志 | stdout（Uvicorn / 应用 print） |

**推荐部署**：本机单用户 / 开发机网关。不要在未加鉴权的情况下把 8787 暴露到公网。

---

## 2. 环境要求

| 依赖 | 版本 | 说明 |
|------|------|------|
| Python | 3.10+ | 已在 3.10.7 / 3.12 验证 |
| 操作系统 | Windows 10/11、Linux、macOS | 本机为 Windows 时见下文 |
| 磁盘 | < 100 MB | 无模型权重 |
| 网络 | 能访问所配置的上游 API | 本地 Ollama 除外 |
| 上游账号 | OpenAI / Anthropic / DeepSeek 等 Key | 至少启用一个 Provider |

可选：

- `curl` 或任意 HTTP 客户端 / OpenAI·Anthropic SDK
- 浏览器（看 Dashboard）

### 2.1 Windows 环境检查

```powershell
# 确认 Python
python --version
# 若无 python，检查 py launcher
py -3 --version

# 确认 pip
python -m pip --version
```

若系统装了多个 Python，下文统一用 `python` 指向「你要用来跑中转站的那个解释器」。

### 2.2 准备目录

```powershell
cd D:\Work_space\01_Work_Projects\central_proxy
```

若从压缩包/仓库克隆得到代码，保证目录内至少有：

```
main.py
requirements.txt
config.example.yaml
central_proxy\
  app.py
  config.py
  ...
static 在 central_proxy\static\dashboard.html
```

---

## 3. 安装

### 3.1 方式 A：venv（推荐）

```powershell
cd D:\Work_space\01_Work_Projects\central_proxy

python -m venv .venv
.\.venv\Scripts\Activate.ps1
# 若提示禁止执行脚本，先（当前用户）执行：
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

依赖清单（`requirements.txt`）：

- `fastapi`
- `uvicorn[standard]`
- `httpx`
- `pydantic` ≥ 2
- `PyYAML`

### 3.2 方式 B：全局 / 现有环境

```powershell
python -m pip install -r requirements.txt
```

### 3.3 验证安装

```powershell
python -c "import fastapi, uvicorn, httpx, pydantic, yaml; print('deps ok')"
```

### 3.4 无真实 Key 的冒烟测试（可选）

```powershell
python scripts\smoke.py
# 期望输出：SMOKE OK
```

该脚本会起一个本地 dummy 上游，验证路由、别名、监控计数和 HTTP 接口，**不需要**任何厂商 API Key。

---

## 4. 配置

### 4.1 生成配置文件

```powershell
Copy-Item config.example.yaml config.yaml
```

编辑 `config.yaml`（该文件已在 `.gitignore` 中，勿提交仓库）。

### 4.2 最小可用配置示例

只启用 OpenAI + Anthropic 时：

```yaml
server:
  host: "127.0.0.1"
  port: 8787
  dashboard: true

defaults:
  timeout_seconds: 120
  connect_timeout_seconds: 10
  max_retries: 2
  retry_backoff_seconds: 0.8
  fallback_model: null   # 可选，例如 "gpt-4o-mini"

providers:
  openai:
    type: openai
    base_url: "https://api.openai.com/v1"
    api_key: "${OPENAI_API_KEY}"
    enabled: true
    models:
      - gpt-4o
      - gpt-4o-mini

  anthropic:
    type: anthropic
    base_url: "https://api.anthropic.com"
    api_key: "${ANTHROPIC_API_KEY}"
    enabled: true
    models:
      - claude-sonnet-4-20250514
      - claude-3-5-haiku-20241022

aliases:
  fast: gpt-4o-mini
  smart: claude-sonnet-4-20250514
```

### 4.3 字段说明

#### `server`

| 字段 | 默认 | 说明 |
|------|------|------|
| `host` | `127.0.0.1` | 监听地址。改 `0.0.0.0` 会对局域网开放，需自担风险 |
| `port` | `8787` | 监听端口 |
| `dashboard` | `true` | 是否提供 `/dashboard`（路由始终注册；可自行防火墙拦截） |

#### `defaults`

| 字段 | 默认 | 说明 |
|------|------|------|
| `timeout_seconds` | `120` | 单次上游总超时 |
| `connect_timeout_seconds` | `10` | 建连超时 |
| `max_retries` | `2` | 对连接错误、408/429/5xx 的额外重试次数 |
| `retry_backoff_seconds` | `0.8` | 重试间隔基数（按 attempt 递增） |
| `fallback_model` | `null` | 主上游硬失败后尝试的模型名（须已在某个 provider 的 models 里） |

#### `providers.<id>`

| 字段 | 必填 | 说明 |
|------|------|------|
| `type` | 是 | `openai` 或 `anthropic` |
| `base_url` | 是 | 上游 Base URL（见下表） |
| `api_key` | 建议 | 支持 `${ENV_VAR}`；本地 Ollama 可填任意非空串 |
| `enabled` | 否 | 默认 `true`；`false` 时不参与路由，但状态页仍可显示 |
| `models` | 是 | 该上游可路由的模型 id 列表 |
| `timeout_seconds` | 否 | 覆盖 defaults |
| `max_retries` | 否 | 覆盖 defaults |

**同一模型 id 只应配置在一个启用的 provider 下**；若重复，第一个生效。

#### 常见上游 `base_url`

| 上游 | type | base_url |
|------|------|----------|
| OpenAI 官方 | `openai` | `https://api.openai.com/v1` |
| DeepSeek | `openai` | `https://api.deepseek.com/v1` |
| Moonshot | `openai` | `https://api.moonshot.cn/v1` |
| SiliconFlow | `openai` | `https://api.siliconflow.cn/v1` |
| OpenRouter | `openai` | `https://openrouter.ai/api/v1` |
| 本地 Ollama | `openai` | `http://127.0.0.1:11434/v1` |
| Anthropic 官方 | `anthropic` | `https://api.anthropic.com` |

规则：`type: openai` 时，代理会在 `base_url` 后拼 `/chat/completions`；`type: anthropic` 时拼 `/v1/messages`（若 base_url 已含完整路径则不重复拼接）。

### 4.4 API Key 环境变量

**PowerShell（当前会话）**

```powershell
$env:OPENAI_API_KEY = "sk-..."
$env:ANTHROPIC_API_KEY = "sk-ant-..."
$env:DEEPSEEK_API_KEY = "sk-..."
```

**当前用户持久化（Windows）**

```powershell
setx OPENAI_API_KEY "sk-..."
setx ANTHROPIC_API_KEY "sk-ant-..."
# setx 写入后需新开终端才生效
```

也可在 `config.yaml` 里直接写死 `api_key: "sk-..."`，但更易泄露，**推荐用环境变量**。

### 4.5 加入更多 OpenAI 兼容厂商

在 `providers` 下追加即可，无需改代码：

```yaml
  deepseek:
    type: openai
    base_url: "https://api.deepseek.com/v1"
    api_key: "${DEEPSEEK_API_KEY}"
    enabled: true
    models:
      - deepseek-chat
      - deepseek-reasoner

  ollama:
    type: openai
    base_url: "http://127.0.0.1:11434/v1"
    api_key: "ollama"
    enabled: true
    models:
      - llama3.2
      - qwen2.5:7b
```

改完配置后**重启进程**生效。

---

## 5. 启动

### 5.1 前台启动

```powershell
cd D:\Work_space\01_Work_Projects\central_proxy
.\.venv\Scripts\Activate.ps1   # 若使用 venv
python main.py
```

启动日志示例：

```
Central Proxy listening on http://127.0.0.1:8787
  Dashboard:  http://127.0.0.1:8787/dashboard
  OpenAI:     http://127.0.0.1:8787/v1
  Anthropic:  http://127.0.0.1:8787/v1/messages
  Health:     http://127.0.0.1:8787/healthz
INFO:     Uvicorn running on http://127.0.0.1:8787 (Press CTRL+C to quit)
```

### 5.2 命令行覆盖

```powershell
python main.py -c config.yaml --port 8787 --host 127.0.0.1
```

| 参数 | 说明 |
|------|------|
| `-c` / `--config` | 配置路径，默认 `./config.yaml` |
| `--host` | 覆盖 `server.host` |
| `--port` | 覆盖 `server.port` |

### 5.3 配置缺失时

若缺少 `config.yaml`，进程会直接退出并提示复制 `config.example.yaml`。

### 5.4 健康检查

```powershell
curl http://127.0.0.1:8787/healthz
# {"ok":true,"service":"central_proxy","version":"0.1.0"}
```

PowerShell 也可用：

```powershell
Invoke-RestMethod http://127.0.0.1:8787/healthz
```

---

## 6. 客户端接入

Base：

| 协议 | Base URL |
|------|----------|
| OpenAI 兼容 | `http://127.0.0.1:8787/v1` |
| Anthropic | `http://127.0.0.1:8787` |

本地中转**不校验**客户端带来的 Authorization（v1 无鉴权）；SDK 仍要求非空 api_key，可填占位符如 `local`。

### 6.1 OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8787/v1",
    api_key="local",
)

resp = client.chat.completions.create(
    model="gpt-4o-mini",          # 或 aliases 里的名字，如 "fast"
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)

# 流式
stream = client.chat.completions.create(
    model="fast",
    messages=[{"role": "user", "content": "写一首短诗"}],
    stream=True,
)
for chunk in stream:
    delta = chunk.choices[0].delta.content
    if delta:
        print(delta, end="", flush=True)
```

### 6.2 Anthropic Python SDK

```python
import anthropic

client = anthropic.Anthropic(
    base_url="http://127.0.0.1:8787",
    api_key="local",
)

msg = client.messages.create(
    model="claude-sonnet-4-20250514",
    max_tokens=256,
    messages=[{"role": "user", "content": "你好"}],
)
print(msg.content[0].text)
```

### 6.3 curl

```powershell
curl http://127.0.0.1:8787/v1/chat/completions `
  -H "Content-Type: application/json" `
  -d '{\"model\":\"fast\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'
```

```powershell
curl http://127.0.0.1:8787/v1/messages `
  -H "Content-Type: application/json" `
  -d '{\"model\":\"smart\",\"max_tokens\":128,\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'
```

### 6.4 切换模型

请求体中的 `model` 决定上游：

1. 若命中 `aliases`，先解析为真实模型 id  
2. 再在启用的 provider 的 `models` 列表中查找  
3. 找不到 → HTTP 404 `Model not found`

```powershell
# 列出中转站暴露的全部模型（含别名）
Invoke-RestMethod http://127.0.0.1:8787/v1/models
```

### 6.5 常见软件改 Base URL

| 客户端 | 设置位置 |
|--------|----------|
| Cursor / Continue / Cline 等 | OpenAI Base URL → `http://127.0.0.1:8787/v1`，Model 填中转站中的 id |
| LangChain ChatOpenAI | `openai_api_base="http://127.0.0.1:8787/v1"` |
| 环境变量方式（OpenAI SDK） | `OPENAI_BASE_URL=http://127.0.0.1:8787/v1`（注意部分工具要带 `/v1`） |

---

## 7. 监控与运维

### 7.1 Web Dashboard

浏览器打开：

```
http://127.0.0.1:8787/dashboard
```

功能：

- 全局：当前并发、累计请求、错误数、运行时长  
- 按 Provider：active / total / errors、最近错误、模型列表  
- 在飞请求表：id、模型、协议、路径、已耗时、客户端 IP  
- 最近完成：状态、HTTP 码、延迟、错误信息  

页面每 2 秒拉取 `/api/status`。

### 7.2 状态 API

| 接口 | 用途 |
|------|------|
| `GET /api/status` | 并发与在飞快照 |
| `GET /api/history?limit=50` | 全局最近完成请求 |
| `GET /api/config` | 配置视图（**API Key 已脱敏为 `***` 或空**） |

示例：

```powershell
$s = Invoke-RestMethod http://127.0.0.1:8787/api/status
$s.totals
$s.providers | Format-Table id, active, total, errors
```

### 7.3 指标含义

| 字段 | 含义 |
|------|------|
| `active` | 当前正在向上游发起、尚未结束的请求数 |
| `total` | 本进程启动以来完成的请求数 |
| `errors` | 以错误结束的请求数（HTTP≥400 或异常） |
| `in_flight[]` | 正在处理的请求详情 |
| `recent[]` | 该 Provider 最近完成记录（环形缓冲） |

注意：监控数据在**进程内存**中，重启即清空。

### 7.4 以 Windows 计划任务 / 开机自启（可选）

**当前用户登录后启动：**

1. 新建 `start_proxy.bat`（与项目同级或任意路径）：

```bat
@echo off
cd /d D:\Work_space\01_Work_Projects\central_proxy
call .venv\Scripts\activate.bat
python main.py
```

2. `Win+R` → `shell:startup` → 把该 bat 的快捷方式放进启动文件夹。

**用任务计划程序更稳：**

```powershell
$action  = New-ScheduledTaskAction -Execute "D:\Work_space\01_Work_Projects\central_proxy\start_proxy.bat"
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "CentralProxy" -Action $action -Trigger $trigger -Description "Local LLM gateway"
```

手动启停：

```powershell
Start-ScheduledTask -TaskName "CentralProxy"
Stop-ScheduledTask  -TaskName "CentralProxy"
```

进程内停止：在前台窗口按 `Ctrl+C`；后台进程可用：

```powershell
# 按端口找 PID 后结束（管理员可能不需要）
Get-NetTCPConnection -LocalPort 8787 -State Listen |
  Select-Object -ExpandProperty OwningProcess |
  ForEach-Object { Stop-Process -Id $_ -Force }
```

### 7.5 systemd（Linux 服务器可选）

`/etc/systemd/system/central-proxy.service`：

```ini
[Unit]
Description=Central Proxy LLM Gateway
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/opt/central_proxy
Environment=OPENAI_API_KEY=sk-...
Environment=ANTHROPIC_API_KEY=sk-ant-...
ExecStart=/opt/central_proxy/.venv/bin/python main.py -c config.yaml
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now central-proxy
sudo systemctl status central-proxy
journalctl -u central-proxy -f
```

---

## 8. 升级与配置变更

| 变更类型 | 操作 |
|----------|------|
| 改模型列表 / 新增 Provider / 改 Key | 编辑 `config.yaml` 后**重启**进程 |
| 升级依赖 | 激活 venv → `pip install -r requirements.txt` → 重启 |
| 升级代码 | 备份 `config.yaml` → 覆盖代码 → 重启 → 看 healthz |
| 换端口 | 改 `server.port` 或 `python main.py --port xxxx` |

配置**不会热加载**；改完必须重启。

---

## 9. 安全建议

1. **保持 `host: 127.0.0.1`**，除非明确要在局域网内共用。  
2. **不要把 8787 直接暴露公网**；v1 无客户端鉴权，任何人能打到该端口即可花你的上游额度。  
3. API Key 优先用环境变量；`config.yaml` 不要提交到 Git。  
4. `/api/status` 不回显 Key，但 `/api/config` 会展示 base_url 与模型名，仍属敏感拓扑信息，勿对不可信方开放。  
5. 若必须局域网共享：先在系统防火墙限制来源 IP，后续版本可加网关 API Key。

---

## 10. 故障排查

| 现象 | 可能原因 | 处理 |
|------|----------|------|
| 启动报 `Config not found` | 无 `config.yaml` | `Copy-Item config.example.yaml config.yaml` |
| 启动报 `Config validation failed` | YAML 缩进/类型错误 | 对照第 4 节检查；YAML 用空格勿用 Tab |
| 启动报 `No providers configured` | `providers` 为空 | 至少配置一个 provider |
| `Model not found: xxx` | 模型未写入任何启用 provider 的 `models`，或别名未配置 | 改配置并重启；用 `/v1/models` 核对 |
| 上游 `HTTP 401` | Key 错误 / 未设置 / `${ENV}` 未展开 | 确认环境变量在**同一终端**已设置再启动 |
| 上游 `HTTP 404` | `base_url` 少了 `/v1` 或路径不对 | 按 4.3 表核对 base_url |
| 连接超时 / `ConnectError` | 网络、代理、上游不可达 | 检查能否 `curl` 上游；公司代理需配置系统代理 |
| 流式中途断开 | 上游网络中断 | Dashboard/history 会记 `stream: ...`；可重试 |
| Dashboard 打不开 | 端口占用或进程未起 | `Get-NetTCPConnection -LocalPort 8787`；看启动日志 |
| 端口被占用 | 已有进程占用 8787 | 换 `--port` 或结束占用进程 |
| PowerShell 无法激活 venv | 执行策略限制 | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| 客户端报 SSL/URL 错 | Base URL 写错（漏 `/v1` 等） | OpenAI 用 `.../v1`；Anthropic 用主机根路径 |

### 10.1 日志怎么看

前台启动时 stdout 会打印每个 HTTP 请求（Uvicorn access log）。应用错误也会打在终端。Windows 下可重定向：

```powershell
python main.py *>> proxy.log
```

### 10.2 快速自检清单

```powershell
# 1) 进程是否监听
Test-NetConnection 127.0.0.1 -Port 8787

# 2) 健康
Invoke-RestMethod http://127.0.0.1:8787/healthz

# 3) 模型是否出现在目录
(Invoke-RestMethod http://127.0.0.1:8787/v1/models).data.id

# 4) 一次真实对话（替换 model）
Invoke-RestMethod http://127.0.0.1:8787/v1/chat/completions `
  -Method Post -ContentType "application/json" `
  -Body '{"model":"fast","messages":[{"role":"user","content":"ping"}]}'

# 5) 看错误是否进监控
(Invoke-RestMethod http://127.0.0.1:8787/api/status).providers |
  Select-Object id, total, errors, last_error
```

---

## 11. 端口与路径速查

| 路径 | 方法 | 说明 |
|------|------|------|
| `/healthz` | GET | 存活 |
| `/dashboard` | GET | 监控页 |
| `/api/status` | GET | 并发 JSON |
| `/api/history` | GET | 历史 |
| `/api/config` | GET | 配置（脱敏） |
| `/docs` | GET | FastAPI 交互文档 |
| `/v1/models` | GET | 模型列表 |
| `/v1/chat/completions` | POST | OpenAI Chat |
| `/v1/messages` | POST | Anthropic Messages |

---

## 12. 卸载 / 停用

1. 结束进程（Ctrl+C 或结束 8787 监听进程）  
2. 删除计划任务（若有）：`Unregister-ScheduledTask -TaskName "CentralProxy" -Confirm:$false`  
3. 删除项目目录或仅保留 `config.yaml` 备份  
4. 如需清除密钥：删除相关用户环境变量  

---

## 13. 相关文档

- 架构与模块设计：[`DESIGN.md`](./DESIGN.md)  
- 功能速览与示例：[`README.md`](./README.md)  

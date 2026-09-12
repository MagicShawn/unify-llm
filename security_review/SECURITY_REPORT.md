# central_proxy (unify_llm) 安全评估报告

- 日期：2026-09-12
- 范围：整仓代码 review + 密码管理/平台安全评估
- 方法：通读全部源码（Python 网关、前端 HTML/JS、js-proxy、脚本、CI），并编写自动化审计脚本在**真实运行实例**上做闭环验证（mock 上游 + 临时 DB/配置 + 随机端口，含真实局域网 IP 绑定模拟远程攻击者）。
- 复现材料：`security_review/audit_live.py`（25 项检查）、`security_review/audit_log.txt`（运行日志，9 项漏洞现场复现）。
- **修复状态（2026-09-12，branch `fix/security-hardening`）**：F1–F8 已全部修复。闭环复验：`python -m pytest tests/test_security.py -q` + `python security_review/audit_live.py` → **25 checks, 0 remaining findings**。

## 修复对照

| # | 修复 |
|---|------|
| F1 | localhost 特权判定只用 TCP peer（`_peer_ip`）；XFF/X-Real-IP 仅当 peer ∈ `auth.trusted_proxies` 时采纳。uvicorn `proxy_headers=False`（默认）避免本机/反代场景下 peer 被 XFF 改写。 |
| F2 | 限流桶键改为 peer（或可信代理后的 XFF）；配合 F1 的 proxy_headers 关闭，轮换 XFF 不再开新桶。 |
| F3 | 新增 `LoginGuard`（IP + 账号滑动窗口，默认 30/10 次失败 / 60s，锁定 30s）；注册接口共用 IP 配额。 |
| F4 | 错误密码一律 401 且文案一致；仅在**密码正确但账号 pending/disabled** 时返回 403。 |
| F5 | 改密调用 `delete_sessions_for_user(..., keep_token=current)`：吊销其他会话，保留当前设备。 |
| F6 | 会话只存 SHA-256；启动时迁移旧明文 token，`PRAGMA secure_delete=ON` + VACUUM。 |
| F7 | `auth.session_cookie_secure` 可开 Secure；LAN 文档要求 TLS 反代路径。 |
| F8 | scrypt N=2^15（旧 N=2^14 仍可验）+ n/r/p 上限；密码 min 10 / max 128；主密钥 `hmac.compare_digest`；LAN 无 key 启动打印醒目警告。 |

## 结论速览

整体代码质量较高、无恶意行为、密码存储方案正确；但存在 **1 个严重级别的认证旁路**（XFF 伪造 → 管理员接管，特定配置下）以及若干中危问题（登录无限流、限流可绕过、改密不吊销会话等）。全部发现均已脚本复现。

| # | 等级 | 问题 | 验证 |
|---|------|------|------|
| F1 | **严重** | 无网关密钥 + 局域网绑定时，伪造 `X-Forwarded-For: 127.0.0.1` 即可远程调用全部 admin API：创建自己的管理员账号、获取有效会话、给真实管理员签发 API key | B2/B3/B4 现场复现 |
| F2 | 高 | `/v1` 每客户端 RPM 限流可被轮换 XFF 头完全绕过（15/15 绕过） | C13 现场复现 |
| F3 | 高 | `/api/auth/login` 无任何速率限制/锁定（实测 ~300 次/秒），且每次尝试消耗一次 scrypt（CPU/内存放大，可 DoS） | C5 现场复现 |
| F4 | 中 | 登录接口账号枚举：pending 账号 + 任意密码 → 403，未知邮箱 → 401，无需密码即可探测注册邮箱 | C6 现场复现 |
| F5 | 中 | 用户改密后旧会话全部仍然有效（未吊销其他会话） | C10 现场复现 |
| F6 | 中 | 会话令牌明文存于 SQLite（DB 文件泄露 = 会话劫持）；API key 则正确地只存哈希 | A5 现场复现 |
| F7 | 中(信息) | 全程明文 HTTP：session cookie `Secure=False`、密码/密钥明文过网（LAN 设计取舍，文档已提示，但建议提供 TLS 路径） | C8 现场复现 |
| F8 | 低 | scrypt N=2^14 低于 OWASP 2023 建议（N≥2^17）；密码最短 8 位无复杂度要求、无最大长度限制；注册接口开放可刷 pending 账号；主密钥比较非常量时间 | A1/代码确认 |

## 1. 代码 Review：无不安全/恶意行为

- **无恶意代码**：无 `eval`/`exec`/动态导入混淆、无对外数据回传、无隐藏网络端点；js-proxy 仅转发到配置的 upstream 且默认绑定 `127.0.0.1`；依赖面极小（fastapi/httpx/uvicorn/pydantic/yaml），CI 只跑 pytest + smoke。
- **SQL 注入**：所有查询参数化，注入 payload 实测 401（C7）。
- **密钥泄露面**：上游 provider key 不入日志（monitor 仅记录白名单 header）、`/api/config` 掩码为 `***`（C4 实测）；原始 API key 只在创建响应中出现一次，DB 中仅存 SHA-256（A3 实测）；`config.yaml`、`data/*.db` 均已被 `.gitignore` 正确排除，未入库。
- **SSRF**：upstream base_url 只能来自 config.yaml，admin API 无法改写 URL（只能 toggle enabled），无 SSRF 注入点。
- **前端 XSS**：两个静态页对用户可控字段（display_name、badge、username、model、user_agent、headers、错误消息）在所有 `innerHTML` 拼接处统一经 `esc()` 转义（静态核查约 100+ 处均覆盖；存储不转义、渲染时转义，模式一致可接受）。注意：dashboard 将网关密钥存于 `localStorage`（XSS 即泄露，但 XSS 面已被上述转义收窄）。

## 2. 密码管理评估

**做对了的**（A1/A2/A4 实测）：
- scrypt（N=2^14, r=8, p=1, dklen=32）+ 每哈希 16 字节随机盐；校验用 `hmac.compare_digest` 常量时间比较。
- 明文密码与明文 API key 从不落库；管理员重置密码不写日志。
- API key 为 `secrets` 生成的 128 位随机值，只存 SHA-256 + 展示前缀，原始值仅创建时返回一次。
- 会话有服务端过期、禁用/待审用户即吊销（C11 实测）、非 admin 会话访问 admin API 被拒（C9 实测）。

**问题**：见上表 F3/F4/F5/F6/F7/F8。其中改密不吊销旧会话（F5）与"admin 重置密码会清会话"（已实现）行为不一致，建议统一。

## 3. 平台安全评估

**认证矩阵**（C1/C2/C3/C9/C11 实测）：master key / 用户 key / 会话 cookie 三轨正确；用户 key 计费（10→9 分）与零余额 402 拦截正确；RBAC 正确。

**核心根因——`_client_ip()` 盲信请求头**（`unify_llm/app.py:246`）：`X-Forwarded-For` / `X-Real-IP` 优先于 TCP 对端地址，而"无网关密钥时 localhost 免鉴权"的回退（`app.py:639-650, 680-686`）与每客户端限流桶都建立在这个可被客户端任意伪造的值上。这是 F1 + F2 的共同根因。当前仓库 `config.yaml` 绑定 `127.0.0.1` 且未设 key，暂不可利用；但 DEPLOYMENT/LAN 文档的 0.0.0.0 场景仅"推荐"设 key，一旦照做不设即触发 F1。

## 4. 修复建议（按优先级）

1. **localhost/限流判定改用 `request.client.host`**（或增加 `trusted_proxies` 配置，仅对可信代理信任 XFF）。同时消除 F1、F2，改动点在 `_client_ip` 的两个调用方。
2. **给 `/api/auth/login` 加失败限速**：按 IP + 按账号（如 5 次/分钟 + 递增延迟）；顺带统一 pending/未知邮箱的响应为 401（修 F3、F4）。
3. **改密/重置密码统一 `delete_sessions_for_user`**（当前 `/api/me/password` 有意保留当前会话，可保留当前 token、吊销其余；修 F5）。
4. **会话令牌只存 SHA-256**（修 F6，与 API key 同法，需一次性迁移）。
5. LAN 部署文档把"必须设 `UNIFY_GATEWAY_KEY`"从推荐改为硬性要求，并在应用启动绑非回环地址且无 key 时打印醒目警告或拒绝启动；中期提供 TLS（自签或反代）（缓解 F7）。
6. 加固项：scrypt 新哈希升到 N=2^15~2^17（格式自带参数、天然向后兼容）、密码最小 8 提到 10-12 或加复杂度、注册加简单验证/上限、主密钥比较改 `hmac.compare_digest`。

## 附：验证闭环

```
security_review/audit_live.py   # 25 项检查：Part A 存储/加密 5 项；Part B 真实 HTTP 20 项
security_review/audit_log.txt   # 完整运行日志（含 9 项 CONFIRMED FINDING 证据）
```
脚本使用临时目录与临时端口，不触碰真实 `data/` 与 `config.yaml`，可重复执行。

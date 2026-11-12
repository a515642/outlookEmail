# OutlookEmail 对外 API v1 接入文档

本文面向使用 API Key 接入 OutlookEmail 邮箱池的外部开发者。

- 基础地址：`http(s)://<你的部署域名>`
- 接口前缀：`/api/v1`
- 数据格式：请求与响应均为 JSON（`Content-Type: application/json`）
- 时间格式：UTC，ISO 8601 文本（如 `2026-01-01T08:30:00Z`）

> 本文只介绍新的 `/api/v1` 接口。旧的 `/api/external/*` 接口和旧全局 API Key 保持不变；两种 Key 不能混用，`/api/v1` 的 Key 无法调用旧接口。

## 1. 准备工作

接入前需要管理员在「设置 → API Key 管理」中为你创建一个 API Key，并确认：

| 配置项 | 说明 |
| --- | --- |
| 名称 | 用于区分接入方，仅后台可见 |
| 权限范围 | `mail:read`（读邮件）、`account:claim`（领取邮箱），按需授予 |
| 作用分组 | Key 能访问的邮箱分组；自动包含所有子分组 |
| 过期时间 | 可留空表示永不过期 |
| 每分钟限速 | 0 表示不限速 |
| IP 白名单 | 可留空；留空表示不限制来源 IP |

创建或轮换成功后，页面会**只显示一次**完整密钥（以 `okv1_` 开头）。关闭弹窗后无法再次查看，请立即保存到调用方的密钥管理系统。

## 2. 认证

每个请求必须携带 Key，支持两种方式：

```http
X-API-Key: okv1_xxxxxxxxxxxxxxxxxxxxxxxx
```

```http
Authorization: Bearer okv1_xxxxxxxxxxxxxxxxxxxxxxxx
```

- Key **只能放在 Header**，查询参数传 Key 会被忽略并返回 `missing_api_key`。
- Key 不区分大小写字段名，但值必须完全一致。
- 不要在浏览器前端代码中硬编码 Key；服务端调用更安全。

### curl 示例

```bash
curl -s "https://mail.example.com/api/v1/mail/folders?email=user@example.com" \
  -H "X-API-Key: okv1_xxxxxxxxxxxxxxxxxxxxxxxx"
```

### Python 示例

```python
import requests

BASE = "https://mail.example.com"
HEADERS = {"X-API-Key": "okv1_xxxxxxxxxxxxxxxxxxxxxxxx"}

resp = requests.get(
    f"{BASE}/api/v1/mail/folders",
    params={"email": "user@example.com"},
    headers=HEADERS,
    timeout=30,
)
resp.raise_for_status()
print(resp.json())
```

### Node.js 示例

```js
const BASE = "https://mail.example.com";
const headers = { "X-API-Key": "okv1_xxxxxxxxxxxxxxxxxxxxxxxx" };

const resp = await fetch(
  `${BASE}/api/v1/mail/folders?email=${encodeURIComponent("user@example.com")}`,
  { headers }
);
console.log(await resp.json());
```

## 3. 接口总览

| 方法 | 路径 | 所需权限 | 说明 |
| --- | --- | --- | --- |
| POST | `/api/v1/accounts/claim` | `account:claim` | 从授权分组内领取一个邮箱 |
| POST | `/api/v1/accounts/release` | `account:claim` | 释放当前 Key 领取的邮箱 |
| GET | `/api/v1/mail/folders` | `mail:read` | 获取可读取的文件夹 |
| GET | `/api/v1/mail/messages` | `mail:read` | 获取邮件列表 |
| GET | `/api/v1/mail/messages/<message_id>` | `mail:read` | 获取邮件正文 |

所有成功响应的顶层结构为：

```json
{ "success": true, "data": { } }
```

## 4. 领取与释放邮箱

### 4.1 领取规则

- 从 Key 绑定的分组及其子分组中，随机选择一个状态正常的邮箱。
- **同一 Key 领取过的邮箱，10 分钟内不会再次发给该 Key**；10 分钟后自动回到可领取池。
- 调用释放接口可立即归还，使该邮箱马上可被同一 Key 再次领取。
- 不同 Key 之间不互斥：其他 Key 仍可能领取到同一个邮箱。
- 领取结果**只有邮箱地址和分组信息**，不包含密码、Client ID、Refresh Token 或别名。

### 4.2 POST `/api/v1/accounts/claim`

无请求体。

成功响应：

```json
{
  "success": true,
  "data": {
    "email": "user@example.com",
    "group_id": 3,
    "group_name": "客户 A"
  }
}
```

可领取池为空时：

```json
{
  "success": false,
  "error_code": "no_account_available",
  "error": "暂无可领取的邮箱"
}
```

HTTP 状态码：`404`。

### 4.3 POST `/api/v1/accounts/release`

请求体：

```json
{ "email": "user@example.com" }
```

成功响应：

```json
{
  "success": true,
  "data": {
    "email": "user@example.com",
    "released": true
  }
}
```

当前 Key 对该邮箱没有有效领取记录时返回 `404 claim_not_found`。

### 4.4 最小流程

```text
POST /accounts/claim        → 拿到 email
GET  /mail/messages         → 读取该邮箱邮件
GET  /mail/messages/<id>    → 读取正文（需要时）
POST /accounts/release      → 提前归还（可选）
```

不调用 release 时，该邮箱会在 10 分钟后自动回归。

## 5. 读取邮件

### 5.1 GET `/api/v1/mail/folders`

参数：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `email` | 是 | 邮箱地址 |

响应：

```json
{
  "success": true,
  "data": {
    "folders": [
      { "name": "inbox", "label": "收件箱" },
      { "name": "junkemail", "label": "垃圾邮件" },
      { "name": "deleteditems", "label": "已删除" }
    ]
  }
}
```

### 5.2 GET `/api/v1/mail/messages`

参数：

| 参数 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `email` | 是 | - | 邮箱地址 |
| `folder` | 否 | `inbox` | `inbox` / `junkemail` / `deleteditems` |
| `skip` | 否 | 0 | 跳过条数，用于分页 |
| `top` | 否 | 20 | 返回条数，最大 50 |

响应：

```json
{
  "success": true,
  "data": {
    "email": "user@example.com",
    "folder": "inbox",
    "skip": 0,
    "top": 20,
    "emails": [
      {
        "id": "AAMkAG...",
        "subject": "欢迎邮件",
        "from": "noreply@example.com",
        "to": "user@example.com",
        "date": "2026-01-01T08:30:00Z",
        "is_read": true,
        "has_attachments": false,
        "body_preview": "这是一封欢迎邮件……",
        "folder": "inbox",
        "id_mode": "uid"
      }
    ],
    "has_more": false
  }
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `id` | 邮件 ID，用于读取正文 |
| `subject` | 主题；无主题时为 `无主题` |
| `from` / `to` | 发件人 / 收件人 |
| `date` | 收件时间（UTC） |
| `is_read` | 是否已读 |
| `has_attachments` | 是否有附件（本 API 不提供附件下载） |
| `body_preview` | 正文预览 |
| `folder` | 邮件所在文件夹 |
| `id_mode` | IMAP 邮件的 ID 类型，读取正文时原样传回 |

`has_more` 为 `true` 时可增大 `skip` 继续翻页。

列表按收件时间**从新到旧**返回，因此：

- 只取最新一封：`top=1`
- 取最新 5 封：`top=5`（不超过 50）
- 读取更早的邮件：增大 `skip`，如 `skip=5&top=5`

**用别名 / plus 地址查询时**（`email` 与账号主邮箱不同，例如 `user+shop@outlook.com`），接口只返回收件人（To/Cc）包含该地址的邮件，分页和 `has_more` 也按过滤后的结果计算。用主邮箱查询则返回全部邮件。

当前没有按日期区间或“仅未读”过滤的参数；如需按时间筛选，请用返回的 `date` 字段自行过滤。

### 5.2.1 等待验证码时的轮询建议

服务端**不做邮件列表缓存**，每次请求都会向上游读取最新邮件，所以实时性有保障。按下面的方式轮询，可以兼顾实时性和稳定性：

1. **轮询间隔建议 2–3 秒**，不要低于 1 秒；
2. **只等最新一封时用 `top=1`**，需要最近几封用 `top=5`，不要每次都拉 20 封；
3. **只需要收件箱时不要用 `folder=all`**，它会同时读取 inbox 和 junkemail，等于双倍请求；
4. **保持参数稳定**（相同的 `folder` / `skip` / `top`）。服务端会把同一时刻的相同请求合并为一次上游调用，多个业务轮询同一个邮箱时尤其有用；
5. 验证码通常 5–10 分钟有效，2–3 秒的间隔足够，不必高频空转；
6. 同一邮箱被多个接入方同时轮询时，上游会对“应用 + 邮箱”限流（10 分钟内 10,000 次、最多 4 个并发），建议同一业务复用同一个 Key，并固定查询参数。

示例（Python，轮询最新一封邮件）：

```python
import time
import requests

API = "https://your-domain.example"
HEADERS = {"X-API-Key": "okv1_xxx"}

def wait_for_code(email, timeout=120, interval=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = requests.get(
            f"{API}/api/v1/mail/messages",
            headers=HEADERS,
            params={"email": email, "folder": "inbox", "top": 1},
            timeout=15,
        )
        if resp.status_code == 429:
            time.sleep(int(resp.headers.get("Retry-After", "5")))
            continue
        data = resp.json()
        if data.get("success") and data["data"]["emails"]:
            return data["data"]["emails"][0]
        time.sleep(interval)
    return None
```

### 5.3 GET `/api/v1/mail/messages/<message_id>`

参数：

| 参数 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `email` | 是 | - | 邮箱地址 |
| `folder` | 否 | `inbox` | 同上 |
| `method` | 否 | `graph` | `graph` 或 `imap` |
| `id_mode` | 否 | 空 | `uid` 或 `sequence`，建议传列表返回的 `id_mode` |

请求示例：

```bash
curl -s "https://mail.example.com/api/v1/mail/messages/AAMkAG...?email=user@example.com&folder=inbox&method=graph" \
  -H "X-API-Key: okv1_xxxxxxxxxxxxxxxxxxxxxxxx"
```

响应：

```json
{
  "success": true,
  "data": {
    "email": "user@example.com",
    "message_id": "AAMkAG...",
    "subject": "欢迎邮件",
    "from": "noreply@example.com",
    "to": "user@example.com",
    "date": "2026-01-01T08:30:00Z",
    "body": "<p>正文 HTML</p>",
    "body_type": "html",
    "has_attachments": false
  }
}
```

说明：

- `body` 可能是 HTML，`body_type` 为 `html` 或 `text`；HTML 内容请自行消毒后再渲染。
- 不返回附件内容、原始邮件头或任何账号凭据。
- 读取失败统一返回 `502 mail_fetch_failed`，不会暴露上游服务细节。

## 6. 错误处理

错误响应统一格式：

```json
{
  "success": false,
  "error_code": "invalid_api_key",
  "error": "API Key 无效"
}
```

| HTTP | error_code | 含义 | 建议处理 |
| --- | --- | --- | --- |
| 400 | `missing_email` | 缺少 `email` 参数 | 检查请求参数 |
| 400 | `invalid_folder` | `folder` 不受支持 | 使用 `inbox` / `junkemail` / `deleteditems` |
| 400 | `invalid_method` | `method` 不是 `graph` / `imap` | 修正参数 |
| 400 | `invalid_id_mode` | `id_mode` 不是 `uid` / `sequence` | 修正参数 |
| 401 | `missing_api_key` | 未提供 Key | 添加认证 Header |
| 401 | `invalid_api_key` | Key 不存在或哈希不匹配 | 检查 Key；必要时轮换 |
| 401 | `key_revoked` | Key 已被撤销 | 联系管理员重新分发 |
| 401 | `key_expired` | Key 已过期 | 联系管理员续期 |
| 403 | `ip_not_allowed` | 来源 IP 不在白名单 | 联系管理员添加出口 IP |
| 403 | `scope_denied` | Key 缺少对应权限 | 联系管理员补充 scope |
| 403 | `group_not_allowed` | 邮箱不在 Key 的授权分组内 | 只能访问被授权分组的邮箱 |
| 404 | `account_not_found` | 邮箱不存在 | 检查地址 |
| 404 | `mail_not_found` | 邮件不存在，或不属于所查询的别名 | 用主邮箱查询，或检查 `to` 地址 |
| 404 | `no_account_available` | 暂无可领取邮箱 | 稍后重试 |
| 404 | `claim_not_found` | 当前 Key 未领取该邮箱 | 先 claim 再 release |
| 429 | `rate_limited` | 超过每分钟限速 | 按 `Retry-After` 秒数等待后重试 |
| 502 | `mail_fetch_failed` | 上游邮件服务失败 | 稍后重试；持续失败联系管理员 |

### 重试建议

- `429`：读取响应头 `Retry-After`，等待指定秒数后再重试。
- `502`：使用指数退避（如 1s、2s、4s），最多重试 3 次。
- `401` / `403`：不要自动重试，检查配置或联系管理员。
- `404 no_account_available`：按业务需要间隔重试，避免高频空转。

## 7. 限速、IP 白名单与 CORS

- **限速**：按 Key 统计每分钟请求数。被限流时返回 `429` 与 `Retry-After`。
- **IP 白名单**：为空时不限制；填写后仅白名单内的 IP/CIDR 可访问。若你的出口 IP 不固定，请联系管理员放行网段。
- **CORS**：`/api/v1/*` 允许跨域读取，`Access-Control-Allow-Origin: *`，不支持携带 Cookie 的跨域请求。出于密钥安全，仍建议由服务端调用。

## 8. 常见问题

**Q：为什么领取邮箱不返回密码或 Refresh Token？**

v1 的 `account:claim` 只用于分配邮箱地址，不提供账号凭据。邮件读取请使用本文的 `/mail/*` 接口。

**Q：一个 Key 可以给多个业务使用吗？**

可以，但建议按接入方/业务分别创建 Key，便于独立限速、撤销和审计。

**Q：10 分钟后邮箱会被别的 Key 抢走吗？**

不会“抢走”。领取不是全局独占；其他 Key 始终可能领取到同一邮箱。10 分钟只是限制同一 Key 的重复领取。

**Q：如何确认邮件是本次领取后才收到的？**

领取接口不返回时间戳。建议在领取时记录本地时间，然后按 `date` 字段过滤邮件列表。

**Q：支持附件下载、发信、删除邮件或修改账号吗？**

当前 v1 不开放这些能力。

**Q：Key 泄露了怎么办？**

立即在后台撤销或轮换。撤销/轮换立即生效，旧 Key 的后续请求会返回 `401`。

## 9. 版本与兼容性

- 当前版本：`v1`。
- 新能力会在 `/api/v1` 内以新增字段/新增接口的方式提供，不破坏既有字段。
- 旧版 `/api/external/*` 与本文无关，互不影响。

# 研究生生活驾驶舱

线上地址：
`https://life.72-11-130-223.sslip.io:8444/`

旧入口 `https://snappython.github.io/admin/` 负责跳转，并在 VPS 尚无数据时
一次性迁移原来保存在 GitHub Pages 域名下的浏览器数据。

## 组成

- `static/index.html`：移动端优先的驾驶舱和 30 分钟时间块时间轴。
- `app.py`：FastAPI 接口、SSE 多端更新通知和后台日历同步。
- `database.py`：SQLite 数据模型、修订号和旧数据迁移。
- `google_calendar.py`：Google Calendar OAuth 与双向事件同步。
- `life-cockpit.service`：VPS systemd 服务。
- `Caddyfile`：Authelia SSO、静态页面和 API 反向代理配置片段。

## VPS 位置

- 程序：`/opt/life-cockpit`
- 页面：`/var/www/life-cockpit/index.html`
- 数据库：`/var/lib/life-cockpit/life-cockpit.db`
- 私有配置：`/opt/life-cockpit/.env`
- 本地端口：`127.0.0.1:9550`
- SSO 用户：`admin`

`.env`、SQLite 文件和 Google 令牌不会提交到 GitHub。Google 令牌在写入
SQLite 前使用 `LC_GOOGLE_TOKEN_KEY` 加密。

## Google Calendar

在 Google Cloud 中启用 Google Calendar API，创建 OAuth 2.0
“Web application”客户端，并配置：

- Authorized JavaScript origin：
  `https://life.72-11-130-223.sslip.io:8444`
- Authorized redirect URI：
  `https://life.72-11-130-223.sslip.io:8444/api/google/callback`

然后把客户端信息写入 VPS 的 `/opt/life-cockpit/.env`：

```dotenv
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
```

重启 `life-cockpit.service` 后，页面中的 Google 日历按钮会从“待配置”
变为“连接”。当前同步对象是主日历中的有起止时间事件；全天事件不会转换成
时间块。

## 验证

在本目录运行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s .\tests -v
```

VPS 健康检查：

```bash
/opt/life-cockpit/.venv/bin/python /opt/life-cockpit/smoke.py
```

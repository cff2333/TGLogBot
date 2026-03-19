# TG Log Bot

Telegram 群组发言日志机器人，自动记录群组消息、检测重复发言并自动删除，配套独立管理后台提供统计、搜索与导出功能。

---

## 功能特性

- **消息记录** — 自动捕获群组所有文本消息，持久化存储到 MySQL
- **重复检测** — 同一用户 30 秒内重复发言超过阈值自动删除，触发后后续重复消息逐条即时删除
- **群组白名单** — 支持指定监听群组，留空则监听所有群组
- **代理支持** — 支持 HTTP / SOCKS5 代理，适配国内服务器
- **管理后台** — 独立 Web 后台，提供发言排行、活跃时段、关键词搜索、CSV 导出、重复日志查询
- **容器化部署** — 机器人与管理后台完全独立，各自提供 Docker Compose 配置

---

## 项目结构

```
tg-log/
├── README.md
├── .gitignore
├── sql/
│   └── tg_log.sql             # 数据库建表语句
├── bot/
│   ├── tg_bot.py              # 机器人核心（消息监听 + 重复检测）
│   └── docker-compose.yml     # 机器人容器化部署
└── admin/
    ├── html/
    │   └── index.html             # 管理前端（单页应用，无需构建）
    ├── conf/
    │   └── nginx.conf             # Nginx 配置（静态文件 + API 反代）
    ├── admin_api.py               # 管理后端（FastAPI）
    └── docker-compose.yml         # 管理后台容器化部署
```

---

## 环境要求

| 组件 | 版本 |
|------|------|
| Python | 3.9+ |
| MySQL | 8.0+ |
| Docker | 20.10+ |
| Docker Compose | 2.0+ |

---

## 部署步骤

### 第一步：创建 Telegram Bot

在 Telegram 中找到 [@BotFather](https://t.me/BotFather)，发送 `/newbot`，按提示创建机器人并获取 **Bot Token**。

### 第二步：获取群组 Chat ID

将机器人加入目标群组并设为管理员，在群内发一条消息后访问：

```
https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
```

在返回 JSON 的 `message.chat.id` 字段获取群组 ID（负数，如 `-1001234567890`）。

### 第三步：初始化数据库

```bash
mysql -u root -p < sql/tg_log.sql
```

### 第四步：部署机器人

编辑 `bot/docker-compose.yml`，在 `environment` 区块填写配置：

```yaml
environment:
  - TG_BOT_TOKEN=your_bot_token_here
  - TG_ALLOWED_CHATS=-1001234567890,-1009876543210
  - DB_HOST=192.168.1.100
  - DB_USER=tg_bot
  - DB_PASS=your_password
```

启动：

```bash
cd bot
docker compose up -d
docker compose logs -f
```

### 第五步：部署管理后台

编辑 `admin/docker-compose.yml`，在 `tg-api` 服务的 `environment` 区块填写数据库配置：

```yaml
environment:
  - DB_HOST=192.168.1.100
  - DB_USER=tg_bot
  - DB_PASS=your_password
```

启动：

```bash
cd admin
docker compose up -d
docker compose logs -f
```

浏览器访问 `http://服务器IP:8080`，左下角 API 地址填 `/api`。

---

## 配置参数

### 机器人 `bot/docker-compose.yml`

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `TG_BOT_TOKEN` | ✅ | — | BotFather 颁发的 Token |
| `TG_ALLOWED_CHATS` | | 空（监听全部） | 群组白名单，逗号分隔多个 chat_id |
| `PROXY_URL` | | 空 | 代理地址，国内服务器需填写 |
| `DB_HOST` | ✅ | — | MySQL 服务器地址 |
| `DB_PORT` | | `3306` | MySQL 端口 |
| `DB_USER` | ✅ | — | 数据库用户名 |
| `DB_PASS` | ✅ | — | 数据库密码 |
| `DB_NAME` | | `tg_log` | 数据库名 |
| `DUP_LIMIT` | | `3` | 触发删除的重复次数 |
| `DUP_WINDOW` | | `30` | 重复检测时间窗口（秒） |
| `CLEANUP_INTERVAL` | | `60` | 内存清理间隔（秒） |
| `BATCH_INTERVAL` | | `2` | 批量写库间隔（秒） |

### 管理后台 `admin/docker-compose.yml`

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `DB_HOST` | ✅ | — | MySQL 服务器地址 |
| `DB_PORT` | | `3306` | MySQL 端口 |
| `DB_USER` | ✅ | — | 数据库用户名 |
| `DB_PASS` | ✅ | — | 数据库密码 |
| `DB_NAME` | | `tg_log` | 数据库名 |
| `WEB_PORT` | | `8080` | 前端访问端口 |

---

## 代理配置

国内服务器访问 Telegram API 需配置代理，在 `bot/docker-compose.yml` 的 `PROXY_URL` 填写：

```
# HTTP 代理
http://127.0.0.1:7890

# SOCKS5 代理
socks5://127.0.0.1:7891

# 带认证的代理
http://user:pass@127.0.0.1:7890
```

> 注意：`127.0.0.1` 在容器内指向容器自身，代理运行在宿主机时需改为宿主机 IP。

---

## 管理后台功能

| 页面 | 功能 |
|------|------|
| 概览 | 全局统计、每日消息趋势图、活跃时段分布图、群组列表 |
| 发言排行 | 按时间段查询发言次数，饼图 + 进度条可视化 |
| 关键词搜索 | 全文搜索消息内容，关键词高亮，支持分页 |
| 重复消息日志 | 查看所有触发自动删除的重复消息记录 |
| 导出 | 按群组和时间范围导出 CSV 文件 |

---

## 数据库表结构

| 表名 | 说明 |
|------|------|
| `groups` | 群组信息 |
| `users` | 用户信息 |
| `messages` | 消息记录，`content_hash` 使用 `BINARY(32)` 存储 SHA256 |
| `duplicate_log` | 重复消息触发删除日志 |

---

## 重复检测机制

```
用户发相同内容 × 3  →  触发删除，全部清除，进入持续监控状态
用户继续发相同内容  →  每条立即删除
30 秒内无相同内容   →  监控状态自动解除，重新计数
```

触发阈值（`DUP_LIMIT`）和时间窗口（`DUP_WINDOW`）均可通过环境变量调整。

---

## 机器人权限要求

在目标群组中，机器人需要以下管理员权限：

| 权限 | 用途 |
|------|------|
| 读取消息 | 监听群组所有发言 |
| 删除消息 | 自动清除重复内容 |

---

## 本地开发

```bash
# 安装依赖
pip install python-telegram-bot aiomysql        # 机器人
pip install fastapi uvicorn aiomysql            # 管理后端

# 启动机器人
cd bot
TG_BOT_TOKEN=xxx DB_HOST=127.0.0.1 DB_USER=root DB_PASS=xxx python tg_bot.py

# 启动管理后端
cd admin
DB_HOST=127.0.0.1 DB_USER=root DB_PASS=xxx uvicorn admin_api:app --reload --port 8000

# 前端直接用浏览器打开 admin/index.html，左下角 API 地址填 http://127.0.0.1:8000
```

---

## 常用运维命令

```bash
# ── 机器人 ──────────────────────────────────────────
cd bot

docker compose logs -f            # 实时查看日志
docker compose restart            # 重启
docker compose down               # 停止并移除容器
docker compose pull && docker compose up -d  # 更新镜像

# ── 管理后台 ─────────────────────────────────────────
cd admin

docker compose logs -f tg-api     # 查看后端日志
docker compose logs -f tg-web     # 查看前端日志
```

---

## License

Apache-2.0 license
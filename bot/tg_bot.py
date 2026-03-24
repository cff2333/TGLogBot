#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram 发言日志机器人 — 完整优化版 (Python 3.9 兼容)

优化项:
  1. 定期清理僵尸桶 (cleanup_tracker)
  2. content_hash 用 16 位短摘要做内存 key，数据库存 BINARY(32)
  3. 数据库写入批量异步提交 (batch_writer)
  4. upsert_user 缓存用户签名，信息未变则跳过数据库
  5. upsert_group 内存缓存，重启后预热
  6. 启动时预热 _seen_users / _seen_groups 缓存
  7. 消息文本规范化（去除首尾空白、合并空格）
  8. 重复检测仅对同一用户 DUP_WINDOW 秒内有效
  9. 重复消息不写入数据库，只记录 duplicate_log
  10. 哨兵机制：首次触发后每条重复消息立即删除

依赖: pip3 install python-telegram-bot aiomysql
"""

import os
import re
import asyncio
import hashlib
import json
import logging
from collections import defaultdict
from time import time
from typing import Dict, List, Tuple, Set, Optional

import aiomysql
from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from telegram.error import BadRequest

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ─── 配置（直接修改或通过环境变量注入） ──────────────────────────────────────
BOT_TOKEN        = os.getenv("TG_BOT_TOKEN",        "your_bot_token_here")
DB_HOST          = os.getenv("DB_HOST",              "127.0.0.1")
DB_PORT          = int(os.getenv("DB_PORT",          "3306"))
DB_USER          = os.getenv("DB_USER",              "root")
DB_PASS          = os.getenv("DB_PASS",              "")
DB_NAME          = os.getenv("DB_NAME",              "tg_log")

DUP_LIMIT        = int(os.getenv("DUP_LIMIT",        "3"))    # 触发删除的重复次数
DUP_WINDOW       = int(os.getenv("DUP_WINDOW",       "30"))   # 重复检测时间窗口（秒）
CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL", "60"))   # 僵尸桶清理间隔（秒）
BATCH_INTERVAL   = float(os.getenv("BATCH_INTERVAL", "2"))    # 批量写库间隔（秒）

# 群组白名单：逗号分隔多个 chat_id，留空则监听所有群组
_whitelist_raw = os.getenv("TG_ALLOWED_CHATS", "")
ALLOWED_CHATS: Set[int] = {
    int(cid.strip()) for cid in _whitelist_raw.split(",") if cid.strip()
}

# 代理配置：留空则不使用代理
PROXY_URL = os.getenv("PROXY_URL", "")

# ─── 全局状态 ─────────────────────────────────────────────────────────────────

pool: Optional[aiomysql.Pool] = None

# 重复检测追踪器: {chat_id: {user_id: {short_hash: [(message_id, timestamp)]}}}
dup_tracker: Dict[int, Dict[int, Dict[str, List[Tuple[int, float]]]]] = \
    defaultdict(lambda: defaultdict(lambda: defaultdict(list)))

# 群组缓存：已写入过的 chat_id
_seen_groups: Set[int] = set()

# 用户缓存：{user_id: (username, first_name, last_name)}
# 签名未变则跳过 upsert
_seen_users: Dict[int, Tuple] = {}

# 批量写库队列: 每项为 (message_id, chat_id, user_id, content, full_hash_bytes)
write_queue: asyncio.Queue = asyncio.Queue()


# ─── 文本规范化 ───────────────────────────────────────────────────────────────

def normalize(text: str) -> str:
    """去除首尾空白，合并连续空白为单个空格"""
    return re.sub(r"\s+", " ", text.strip())


def full_hash(text: str) -> bytes:
    """SHA256 二进制摘要（32 字节），对应数据库 BINARY(32)"""
    return hashlib.sha256(normalize(text).encode()).digest()


def short_hash(text: str) -> str:
    """16 位十六进制短摘要，用于内存追踪器 key"""
    return hashlib.sha256(normalize(text).encode()).hexdigest()[:16]


# ─── 数据库初始化 ─────────────────────────────────────────────────────────────

async def init_db():
    global pool
    # 每次 _run_bot 启动时重新创建连接池
    # 旧 pool 属于已关闭的 event loop，不能复用
    pool = await aiomysql.create_pool(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASS,
        db=DB_NAME, charset="utf8mb4",
        autocommit=True, minsize=2, maxsize=10,
    )

    # 预热缓存：把已有的 id 加载进内存，避免重启后重复 upsert
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT id FROM `groups`")
            rows = await cur.fetchall()
            _seen_groups.update(r[0] for r in rows)

            await cur.execute("SELECT id, username, first_name, last_name FROM `users`")
            rows = await cur.fetchall()
            for r in rows:
                _seen_users[r[0]] = (r[1], r[2], r[3])

    log.info(
        f"数据库连接池已初始化，"
        f"缓存预热：{len(_seen_groups)} 个群组，{len(_seen_users)} 个用户"
    )


# ─── 数据库操作 ───────────────────────────────────────────────────────────────

async def upsert_group(chat_id: int, title: str, username: Optional[str]):
    """写入或更新群组信息，内存缓存命中则跳过"""
    if chat_id in _seen_groups:
        return
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO `groups` (id, title, username)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE
                  title    = IF(title    != VALUES(title),    VALUES(title),    title),
                  username = IF(username != VALUES(username), VALUES(username), username)
                """,
                (chat_id, title, username),
            )
    _seen_groups.add(chat_id)


async def upsert_user(user):
    """写入或更新用户信息，签名未变则跳过数据库"""
    signature = (user.username, user.first_name, user.last_name)
    if _seen_users.get(user.id) == signature:
        return
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO `users` (id, username, first_name, last_name, is_bot)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                  username   = IF(username   != VALUES(username),   VALUES(username),   username),
                  first_name = IF(first_name != VALUES(first_name), VALUES(first_name), first_name),
                  last_name  = IF(last_name  != VALUES(last_name),  VALUES(last_name),  last_name)
                """,
                (user.id, user.username, user.first_name, user.last_name, user.is_bot),
            )
    _seen_users[user.id] = signature


async def batch_insert_messages(rows: List[Tuple]):
    """批量写入正常消息"""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT IGNORE INTO `messages`
                  (message_id, chat_id, user_id, content, content_hash)
                VALUES (%s, %s, %s, %s, %s)
                """,
                rows,
            )


async def write_duplicate_log(
    chat_id:      int,
    user_id:      int,
    fhash:        bytes,
    content:      str,
    repeat_count: int,
    deleted_ids:  List[int],
):
    """记录重复触发事件，不写入 messages 表"""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO `duplicate_log`
                  (chat_id, user_id, content_hash, content, repeat_count, deleted_ids)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (chat_id, user_id, fhash, content, repeat_count, json.dumps(deleted_ids)),
            )


# ─── 后台任务：批量写库 ───────────────────────────────────────────────────────

async def batch_writer():
    """每 BATCH_INTERVAL 秒把队列里积累的消息批量写入数据库"""
    while True:
        await asyncio.sleep(BATCH_INTERVAL)
        if write_queue.empty():
            continue

        batch: List[Tuple] = []
        while not write_queue.empty():
            batch.append(write_queue.get_nowait())

        try:
            await batch_insert_messages(batch)
            log.debug(f"批量写入 {len(batch)} 条消息")
        except Exception as e:
            log.error(f"批量写库失败: {e}")


# ─── 后台任务：清理僵尸桶 ─────────────────────────────────────────────────────

async def cleanup_tracker():
    """每 CLEANUP_INTERVAL 秒扫描整个追踪器，清理过期条目和空 key"""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        now = time()
        total_removed = 0

        for chat_id in list(dup_tracker):
            for user_id in list(dup_tracker[chat_id]):
                for h in list(dup_tracker[chat_id][user_id]):
                    bucket = dup_tracker[chat_id][user_id][h]
                    before = len(bucket)
                    bucket[:] = [
                        (mid, ts) for mid, ts in bucket if now - ts <= DUP_WINDOW
                    ]
                    total_removed += before - len(bucket)
                    if not bucket:
                        del dup_tracker[chat_id][user_id][h]

                if not dup_tracker[chat_id][user_id]:
                    del dup_tracker[chat_id][user_id]

            if not dup_tracker[chat_id]:
                del dup_tracker[chat_id]

        if total_removed:
            log.debug(f"僵尸桶清理完成，移除过期条目 {total_removed} 个")


# ─── 重复消息处理 ─────────────────────────────────────────────────────────────

async def handle_duplicate(
    bot:     Bot,
    chat_id: int,
    user_id: int,
    shash:   str,
    fhash:   bytes,
    content: str,
):
    bucket  = dup_tracker[chat_id][user_id][shash]
    msg_ids = [mid for mid, _ in bucket if mid != -1]   # 排除哨兵
    deleted: List[int] = []

    for mid in msg_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
            deleted.append(mid)
        except BadRequest as e:
            log.warning(f"删除消息 {mid} 失败: {e}")

    # 重复消息不写入 messages 表，只记录触发日志
    await write_duplicate_log(chat_id, user_id, fhash, content, len(msg_ids), deleted)

    # 清空真实消息，放入带当前时间戳的哨兵
    # 哨兵 message_id=-1，DUP_WINDOW 秒内存活，过期后 cleanup_tracker 自然清理
    bucket.clear()
    bucket.append((-1, time()))

    log.info(
        f"群 {chat_id} 用户 {user_id} — {DUP_WINDOW}s 内重复 {len(msg_ids)} 次，"
        f"已删除 message_id: {deleted}"
    )


# ─── 消息事件处理 ─────────────────────────────────────────────────────────────

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type not in ("group", "supergroup"):
        return
    if not msg.text:
        return

    # 白名单校验：配置了白名单才生效，留空则不限制
    if ALLOWED_CHATS and chat.id not in ALLOWED_CHATS:
        return

    content = msg.text
    shash   = short_hash(content)
    fhash   = full_hash(content)
    now     = time()

    await upsert_group(chat.id, chat.title or "", chat.username)
    await upsert_user(user)

    # 重复检测（先检测，再决定是否入队）
    bucket = dup_tracker[chat.id][user.id][shash]
    bucket[:] = [(mid, ts) for mid, ts in bucket if now - ts <= DUP_WINDOW]
    bucket.append((msg.message_id, now))

    has_sentinel = any(mid == -1 for mid, _ in bucket)
    if has_sentinel or len(bucket) >= DUP_LIMIT:
        # 垃圾消息：删除，不入库
        await handle_duplicate(
            context.bot, chat.id, user.id, shash, fhash, content
        )
    else:
        # 正常消息：入队批量写库
        await write_queue.put((msg.message_id, chat.id, user.id, content, fhash))


# ─── 启动 ─────────────────────────────────────────────────────────────────────

async def post_init(app: Application):
    await init_db()
    asyncio.create_task(batch_writer(),    name="batch_writer")
    asyncio.create_task(cleanup_tracker(), name="cleanup_tracker")
    log.info("后台任务已启动: batch_writer / cleanup_tracker")


def main():
    if not BOT_TOKEN or BOT_TOKEN == "your_bot_token_here":
        log.error("请设置 BOT_TOKEN")
        return

    import time as _time
    from telegram.request import HTTPXRequest

    if ALLOWED_CHATS:
        log.info(f"群组白名单已启用，监听范围: {ALLOWED_CHATS}")
    else:
        log.info("群组白名单未配置，监听所有群组")

    retry_count     = 0
    max_retry       = int(os.getenv("MAX_RETRY",       "0"))
    retry_delay     = int(os.getenv("RETRY_DELAY",     "10"))
    retry_max_delay = int(os.getenv("RETRY_MAX_DELAY", "300"))

    while True:
        try:
            request_kwargs = dict(
                connection_pool_size=8,
                connect_timeout=15.0,
                read_timeout=30.0,
                write_timeout=30.0,
                pool_timeout=15.0,
            )
            if PROXY_URL:
                request_kwargs["proxy"] = PROXY_URL
                log.info(f"代理已启用: {PROXY_URL}")
            else:
                log.info("未配置代理，直连 Telegram")

            request = HTTPXRequest(**request_kwargs)
            app = (
                Application.builder()
                .token(BOT_TOKEN)
                .request(request)
                .get_updates_request(request)
                .post_init(post_init)
                .build()
            )
            app.add_handler(
                MessageHandler(filters.TEXT & filters.ChatType.GROUPS, on_message)
            )
            log.info("Bot 启动，开始监听群组消息...")
            app.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
                poll_interval=1.0,
                timeout=20,
            )
            log.info("Bot 正常退出")
            break

        except Exception as e:
            retry_count += 1
            delay = min(retry_delay * (2 ** (retry_count - 1)), retry_max_delay)
            log.error(f"Bot 运行异常（第 {retry_count} 次）: {e}")

            if max_retry and retry_count >= max_retry:
                log.error(f"已达到最大重试次数 {max_retry}，退出")
                break

            log.info(f"{delay} 秒后自动重启...")
            _time.sleep(delay)


if __name__ == "__main__":
    main()

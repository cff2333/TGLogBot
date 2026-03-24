#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TG 日志管理后端 — FastAPI
依赖: pip3 install fastapi uvicorn aiomysql
启动: uvicorn admin_api:app --host 0.0.0.0 --port 8000
"""

import asyncio
import logging
import os
import csv
import io
from datetime import date
from typing import Optional, List, Dict, Any

log = logging.getLogger(__name__)

import aiomysql
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="TG 日志管理后台", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── 配置 ─────────────────────────────────────────────────────────────────────
DB_HOST          = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT          = int(os.getenv("DB_PORT", "3306"))
DB_USER          = os.getenv("DB_USER", "root")
DB_PASS          = os.getenv("DB_PASS", "")
DB_NAME          = os.getenv("DB_NAME", "tg_log")
GROUPS_CACHE_TTL = int(os.getenv("GROUPS_CACHE_TTL", "21600"))  # 群组缓存 TTL（秒），默认 6 小时
CLEAN_INTERVAL   = int(os.getenv("CLEAN_INTERVAL",   "86400"))  # duplicate_log 清理间隔（秒），默认 1 天
DUP_LOG_TTL      = int(os.getenv("DUP_LOG_TTL",      "3"))      # duplicate_log 保留天数

pool: Optional[aiomysql.Pool] = None

# ─── 群组缓存 ──────────────────────────────────────────────────────────────────
# 缓存群组列表查询结果，避免每次请求都执行慢查询
_groups_cache: Optional[List[Dict[str, Any]]] = None
_groups_cache_task: Optional[asyncio.Task] = None
_clean_task: Optional[asyncio.Task] = None


# ─── 生命周期 ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    global pool, _groups_cache_task, _clean_task
    pool = await aiomysql.create_pool(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASS,
        db=DB_NAME, charset="utf8mb4",
        autocommit=True, minsize=2, maxsize=10,
    )
    # 启动时立即填充一次缓存，之后每 GROUPS_CACHE_TTL 秒自动刷新
    await _refresh_groups_cache()
    _groups_cache_task = asyncio.create_task(_groups_cache_loop())
    _clean_task        = asyncio.create_task(_clean_duplicate_log_loop())


@app.on_event("shutdown")
async def shutdown():
    for task in (_groups_cache_task, _clean_task):
        if task:
            task.cancel()
    pool.close()
    await pool.wait_closed()


# ─── 工具函数 ─────────────────────────────────────────────────────────────────

async def query(sql: str, args=()) -> List[Dict]:
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(sql, args)
            return await cur.fetchall()


async def query_one(sql: str, args=()) -> Optional[Dict]:
    rows = await query(sql, args)
    return rows[0] if rows else None


def date_conditions(
    alias: str,
    date_from: Optional[date],
    date_to: Optional[date],
    conditions: List[str],
    params: List,
):
    """向 conditions / params 追加日期过滤条件"""
    if date_from:
        conditions.append(f"DATE({alias}.created_at) >= %s")
        params.append(date_from)
    if date_to:
        conditions.append(f"DATE({alias}.created_at) <= %s")
        params.append(date_to)


# ─── 群组缓存刷新 ──────────────────────────────────────────────────────────────

async def _fetch_groups_from_db() -> List[Dict[str, Any]]:
    """执行群组列表慢查询"""
    return await query("""
        SELECT
          g.id, g.title, g.username, g.is_active, g.joined_at,
          COUNT(m.id)                              AS total_messages,
          COALESCE(SUM(d.repeat_count), 0)         AS deleted_messages
        FROM `groups` g
        LEFT JOIN `messages` m       ON m.chat_id = g.id
        LEFT JOIN `duplicate_log` d  ON d.chat_id = g.id
        GROUP BY g.id
        ORDER BY total_messages DESC
    """)


async def _refresh_groups_cache():
    """刷新群组缓存，查询失败时保留旧缓存"""
    global _groups_cache
    try:
        _groups_cache = await _fetch_groups_from_db()
    except Exception as e:
        log.error(f"群组缓存刷新失败，保留旧缓存: {e}")


async def _groups_cache_loop():
    """后台循环，每 GROUPS_CACHE_TTL 秒刷新一次群组缓存"""
    while True:
        await asyncio.sleep(GROUPS_CACHE_TTL)
        await _refresh_groups_cache()


async def _clean_duplicate_log_loop():
    """后台循环，每 CLEAN_INTERVAL 秒清理超过 DUP_LOG_TTL 天的 duplicate_log 记录"""
    while True:
        await asyncio.sleep(CLEAN_INTERVAL)
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        DELETE FROM `duplicate_log`
                        WHERE triggered_at < NOW() - INTERVAL %s DAY
                    """, (DUP_LOG_TTL,))
                    if cur.rowcount:
                        log.info(f"duplicate_log 清理完成：删除 {cur.rowcount} 条过期记录")
        except Exception as e:
            log.error(f"duplicate_log 清理失败: {e}")


# ─── 概览 ─────────────────────────────────────────────────────────────────────

@app.get("/overview")
async def overview():
    """首页统计数据：群组数、用户数、消息数、重复删除事件数"""
    groups = await query_one("SELECT COUNT(*) AS n FROM `groups`")
    users  = await query_one("SELECT COUNT(*) AS n FROM `users`")
    msgs   = await query_one("SELECT COUNT(*) AS n FROM `messages`")
    dup    = await query_one("SELECT COUNT(*) AS n FROM `duplicate_log`")
    dup_deleted = await query_one("SELECT COALESCE(SUM(repeat_count), 0) AS n FROM `duplicate_log`")
    return {
        "groups":       groups["n"],
        "users":        users["n"],
        "messages":     msgs["n"],
        "dup_events":   dup["n"],
        "deleted":      dup_deleted["n"],
    }


# ─── 群组 ─────────────────────────────────────────────────────────────────────

@app.get("/groups")
async def list_groups():
    """返回群组列表（读缓存，每 GROUPS_CACHE_TTL 秒自动更新）"""
    if _groups_cache is None:
        # 缓存尚未就绪（极少发生，仅启动瞬间），实时查询兜底
        return await _fetch_groups_from_db()
    return _groups_cache


@app.post("/groups/refresh")
async def refresh_groups():
    """手动触发群组缓存刷新"""
    await _refresh_groups_cache()
    return {"ok": True, "count": len(_groups_cache) if _groups_cache else 0}


# ─── 发言排行 ─────────────────────────────────────────────────────────────────

@app.get("/stats/rank")
async def rank(
    chat_id:   int,
    date_from: Optional[date] = None,
    date_to:   Optional[date] = None,
    limit:     int = Query(20, le=100),
):
    conditions = ["m.chat_id = %s"]
    params: List = [chat_id]
    date_conditions("m", date_from, date_to, conditions, params)
    where = " AND ".join(conditions)

    return await query(f"""
        SELECT
          u.id   AS user_id,
          COALESCE(u.username, u.first_name) AS name,
          COUNT(*) AS msg_count
        FROM `messages` m
        JOIN `users` u ON u.id = m.user_id
        WHERE {where}
        GROUP BY u.id
        ORDER BY msg_count DESC
        LIMIT %s
    """, (*params, limit))


# ─── 活跃时段（按小时） ───────────────────────────────────────────────────────

@app.get("/stats/hourly")
async def hourly(
    chat_id:   int,
    date_from: Optional[date] = None,
    date_to:   Optional[date] = None,
):
    where_parts = []
    params: List = []
    if date_from:
        where_parts.append("DATE(created_at) >= %s")
        params.append(date_from)
    if date_to:
        where_parts.append("DATE(created_at) <= %s")
        params.append(date_to)
    extra = (" AND " + " AND ".join(where_parts)) if where_parts else ""

    return await query(f"""
        SELECT HOUR(created_at) AS hour, COUNT(*) AS count
        FROM `messages`
        WHERE chat_id = %s{extra}
        GROUP BY hour
        ORDER BY hour
    """, (chat_id, *params))


# ─── 每日消息趋势 ─────────────────────────────────────────────────────────────

@app.get("/stats/daily")
async def daily(
    chat_id:   int,
    date_from: Optional[date] = None,
    date_to:   Optional[date] = None,
):
    where_parts = ["chat_id = %s"]
    params: List = [chat_id]
    if date_from:
        where_parts.append("DATE(created_at) >= %s")
        params.append(date_from)
    if date_to:
        where_parts.append("DATE(created_at) <= %s")
        params.append(date_to)
    where = " AND ".join(where_parts)

    return await query(f"""
        SELECT DATE(created_at) AS day, COUNT(*) AS count
        FROM `messages`
        WHERE {where}
        GROUP BY day
        ORDER BY day
    """, params)


# ─── 关键词搜索 ───────────────────────────────────────────────────────────────

@app.get("/search")
async def search(
    chat_id: int,
    keyword: str  = Query(..., min_length=1),
    page:    int  = Query(1, ge=1),
    size:    int  = Query(20, le=100),
):
    offset = (page - 1) * size
    like   = f"%{keyword}%"

    total = await query_one("""
        SELECT COUNT(*) AS n FROM `messages`
        WHERE chat_id=%s AND content LIKE %s
    """, (chat_id, like))

    rows = await query("""
        SELECT
          m.id, m.message_id,
          m.created_at,
          COALESCE(u.username, u.first_name) AS name,
          m.content
        FROM `messages` m
        JOIN `users` u ON u.id = m.user_id
        WHERE m.chat_id=%s AND m.content LIKE %s
        ORDER BY m.created_at DESC
        LIMIT %s OFFSET %s
    """, (chat_id, like, size, offset))

    return {"total": total["n"], "page": page, "size": size, "rows": rows}


# ─── 重复消息日志 ─────────────────────────────────────────────────────────────

@app.get("/duplicate-log")
async def dup_log(
    chat_id: int,
    page:    int = Query(1, ge=1),
    size:    int = Query(20, le=100),
):
    offset = (page - 1) * size
    total  = await query_one(
        "SELECT COUNT(*) AS n FROM `duplicate_log` WHERE chat_id=%s", (chat_id,)
    )
    rows = await query("""
        SELECT
          d.id, d.repeat_count, d.deleted_ids, d.triggered_at,
          d.content,
          COALESCE(u.username, u.first_name) AS name
        FROM `duplicate_log` d
        LEFT JOIN `users` u ON u.id = d.user_id
        WHERE d.chat_id=%s
        ORDER BY d.triggered_at DESC
        LIMIT %s OFFSET %s
    """, (chat_id, size, offset))
    return {"total": total["n"], "page": page, "rows": rows}


# ─── 导出 CSV ─────────────────────────────────────────────────────────────────

@app.get("/export/csv")
async def export_csv(
    chat_id:   int,
    date_from: Optional[date] = None,
    date_to:   Optional[date] = None,
):
    where_parts = ["m.chat_id = %s"]
    params: List = [chat_id]
    if date_from:
        where_parts.append("DATE(m.created_at) >= %s")
        params.append(date_from)
    if date_to:
        where_parts.append("DATE(m.created_at) <= %s")
        params.append(date_to)
    where = " AND ".join(where_parts)

    rows = await query(f"""
        SELECT
          m.created_at,
          u.id          AS user_id,
          COALESCE(u.username, u.first_name) AS name,
          m.content
        FROM `messages` m
        JOIN `users` u ON u.id = m.user_id
        WHERE {where}
        ORDER BY m.created_at ASC
    """, params)

    buf = io.StringIO()
    writer = csv.DictWriter(
        buf, fieldnames=["created_at", "user_id", "name", "content"]
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({
            "created_at": str(row["created_at"]),
            "user_id":    row["user_id"],
            "name":       row["name"],
            "content":    row["content"],
        })
    buf.seek(0)

    filename = f"log_{chat_id}_{date.today()}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

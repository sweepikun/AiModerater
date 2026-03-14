import aiosqlite
from datetime import datetime, timedelta
from typing import Optional
from pathlib import Path

from .models import ViolationRecord


class ViolationDB:
    """违规记录数据库操作"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def init(self):
        """初始化数据库连接和表结构"""
        db_dir = Path(self.db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._execute("""
            CREATE TABLE IF NOT EXISTS violations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                user_name TEXT DEFAULT '',
                group_id TEXT NOT NULL,
                group_name TEXT DEFAULT '',
                content TEXT DEFAULT '',
                content_type TEXT DEFAULT 'text',
                reason TEXT DEFAULT '',
                category TEXT DEFAULT '',
                confidence REAL DEFAULT 0.0,
                timestamp TEXT NOT NULL,
                punishment TEXT DEFAULT '',
                violation_count INTEGER DEFAULT 0,
                api_used TEXT DEFAULT ''
            )
        """)
        await self._execute("""
            CREATE INDEX IF NOT EXISTS idx_user_group
            ON violations(user_id, group_id)
        """)
        await self._execute("""
            CREATE INDEX IF NOT EXISTS idx_timestamp
            ON violations(timestamp)
        """)
        await self._db.commit()

    async def close(self):
        """关闭数据库连接"""
        if self._db:
            await self._db.close()
            self._db = None

    async def _execute(self, sql: str, params: tuple = ()):
        if not self._db:
            raise RuntimeError("Database not initialized")
        return await self._db.execute(sql, params)

    async def add_violation(self, record: ViolationRecord) -> int:
        """添加违规记录，返回记录ID"""
        cursor = await self._execute(
            """INSERT INTO violations
               (user_id, user_name, group_id, group_name, content, content_type,
                reason, category, confidence, timestamp, punishment, violation_count, api_used)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.user_id,
                record.user_name,
                record.group_id,
                record.group_name,
                record.content[:500],
                record.content_type,
                record.reason,
                record.category,
                record.confidence,
                record.timestamp.isoformat(),
                record.punishment,
                record.violation_count,
                record.api_used,
            ),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def get_violation_count(self, user_id: str, group_id: str, expire_days: int = 0) -> int:
        """获取用户在某群的违规次数"""
        if expire_days > 0:
            expire_time = (datetime.now() - timedelta(days=expire_days)).isoformat()
            cursor = await self._execute(
                """SELECT COUNT(*) as cnt FROM violations
                   WHERE user_id = ? AND group_id = ? AND timestamp > ?""",
                (user_id, group_id, expire_time),
            )
        else:
            cursor = await self._execute(
                """SELECT COUNT(*) as cnt FROM violations
                   WHERE user_id = ? AND group_id = ?""",
                (user_id, group_id),
            )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def query_violations(
        self,
        user_id: Optional[str] = None,
        group_id: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        content_type: Optional[str] = None,
        limit: int = 20,
    ) -> list[ViolationRecord]:
        """查询违规记录"""
        conditions = []
        params = []

        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if group_id:
            conditions.append("group_id = ?")
            params.append(group_id)
        if start_time:
            conditions.append("timestamp >= ?")
            params.append(start_time)
        if end_time:
            conditions.append("timestamp <= ?")
            params.append(end_time)
        if content_type:
            conditions.append("content_type = ?")
            params.append(content_type)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM violations WHERE {where} ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        cursor = await self._execute(sql, tuple(params))
        rows = await cursor.fetchall()

        records = []
        for row in rows:
            records.append(
                ViolationRecord(
                    id=row[0],
                    user_id=row[1],
                    user_name=row[2],
                    group_id=row[3],
                    group_name=row[4],
                    content=row[5],
                    content_type=row[6],
                    reason=row[7],
                    category=row[8],
                    confidence=row[9],
                    timestamp=datetime.fromisoformat(row[10]),
                    punishment=row[11],
                    violation_count=row[12],
                    api_used=row[13],
                )
            )
        return records

    async def get_stats(self, group_id: Optional[str] = None) -> dict:
        """获取统计信息"""
        if group_id:
            cursor = await self._execute(
                "SELECT COUNT(*) as total FROM violations WHERE group_id = ?", (group_id,)
            )
            total = (await cursor.fetchone())[0]

            cursor = await self._execute(
                """SELECT COUNT(DISTINCT user_id) as users FROM violations
                   WHERE group_id = ?""",
                (group_id,),
            )
            users = (await cursor.fetchone())[0]

            cursor = await self._execute(
                """SELECT category, COUNT(*) as cnt FROM violations
                   WHERE group_id = ? AND category != ''
                   GROUP BY category ORDER BY cnt DESC LIMIT 10""",
                (group_id,),
            )
            categories = await cursor.fetchall()
        else:
            cursor = await self._execute("SELECT COUNT(*) as total FROM violations")
            total = (await cursor.fetchone())[0]

            cursor = await self._execute("SELECT COUNT(DISTINCT user_id) as users FROM violations")
            users = (await cursor.fetchone())[0]

            cursor = await self._execute(
                """SELECT category, COUNT(*) as cnt FROM violations
                   WHERE category != ''
                   GROUP BY category ORDER BY cnt DESC LIMIT 10"""
            )
            categories = await cursor.fetchall()

        today = datetime.now().strftime("%Y-%m-%d")
        cursor = await self._execute(
            "SELECT COUNT(*) FROM violations WHERE timestamp >= ?", (today,)
        )
        today_count = (await cursor.fetchone())[0]

        return {
            "total": total,
            "unique_users": users,
            "today": today_count,
            "categories": {row[0]: row[1] for row in categories},
        }

    async def cleanup_expired(self, expire_days: int) -> int:
        """清理过期记录，返回清理数量"""
        if expire_days <= 0:
            return 0
        expire_time = (datetime.now() - timedelta(days=expire_days)).isoformat()
        cursor = await self._execute(
            "DELETE FROM violations WHERE timestamp < ?", (expire_time,)
        )
        await self._db.commit()
        return cursor.rowcount

import json
import os
import time

import psycopg
from psycopg.rows import dict_row

from build import (
    KIND,
    POLICY_QUEUE,
    REASON_BAD_BOX,
    REASON_POLICY_MISMATCH,
    REASON_POLICY_UNSET,
    inspect_scan,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ods_scan (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    terminal_code TEXT NOT NULL,
    raw_payload JSONB NOT NULL,
    landed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dwd_move (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    source_id BIGINT NOT NULL REFERENCES ods_scan(id),
    terminal_code TEXT NOT NULL,
    container_no TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    event_at TIMESTAMP NOT NULL,
    box_source TEXT NOT NULL DEFAULT 'compliant'
);

CREATE TABLE IF NOT EXISTS build_runs (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    accepted_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    policy_used TEXT,
    built_by TEXT NOT NULL,
    built_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS build_rejects (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES build_runs(id),
    source_id BIGINT NOT NULL REFERENCES ods_scan(id),
    reason TEXT NOT NULL
);

-- 每个业务日一条补码办法，写定后本日不可改口
CREATE TABLE IF NOT EXISTS box_policies (
    biz_date DATE PRIMARY KEY,
    policy TEXT NOT NULL,
    policy_name TEXT NOT NULL,
    set_by TEXT NOT NULL,
    set_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 坏号队列：只收非空且不合规的箱号；全空扫描不进队列
CREATE TABLE IF NOT EXISTS bad_box_queue (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    raw_box TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    reject_count INTEGER NOT NULL DEFAULT 0,
    fixed_count INTEGER NOT NULL DEFAULT 0,
    fixed_box TEXT,
    fixed_by TEXT,
    fixed_at TIMESTAMPTZ,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (biz_date, raw_box)
);
-- 补码尝试台账：办法不符、争用、成功补位/拦收都留痕
CREATE TABLE IF NOT EXISTS completion_attempts (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    raw_box TEXT NOT NULL,
    policy_attempt TEXT NOT NULL,
    policy_name TEXT NOT NULL,
    fixed_box TEXT,
    result TEXT NOT NULL,
    detail TEXT,
    operator TEXT NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_completion_attempts_day
    ON completion_attempts (biz_date, id);
"""


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def init_db():
    last_error = None
    for _ in range(30):
        try:
            with connect() as conn:
                conn.execute(SCHEMA)
                # 老库平滑升级：明细层补来源标记，构建记录补办法字段
                conn.execute("ALTER TABLE dwd_move ADD COLUMN IF NOT EXISTS box_source TEXT NOT NULL DEFAULT 'compliant'")
                conn.execute("ALTER TABLE build_runs ADD COLUMN IF NOT EXISTS policy_used TEXT")
                count = conn.execute("SELECT COUNT(*) AS n FROM ods_scan").fetchone()["n"]
                if count == 0:
                    _seed(conn)
                conn.commit()
            return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(1)
    raise last_error


def _land(conn, biz_date, terminal, payload):
    conn.execute(
        """
        INSERT INTO ods_scan (biz_date, terminal_code, raw_payload)
        VALUES (%s, %s, %s::jsonb)
        """,
        (biz_date, terminal, json.dumps(payload, ensure_ascii=False)),
    )


def _seed(conn):
    built = [
        {"scan": "IN", "box": "CSNU1029384", "at": "2026-11-11T07:40:00", "gate": "T1"},
        {"scan": "OUT", "box": "CSNU1029384", "at": "2026-11-11T16:05:00", "gate": "T1"},
    ]
    waiting = [
        {"scan": "IN", "box": "TCLU5566778", "at": "2026-11-12T09:15:00", "gate": "T2"},
        {"scan": "XX", "box": "TCLU5566778", "at": "2026-11-12T11:00:00", "gate": "T2"},
        {"scan": "RS", "box": "BAD", "at": "2026-11-12T12:00:00", "gate": "T2"},
    ]
    # 演示坏号队列：同一坏号被多个闸口扫到、缺位可补、以及全空扫描不进队列
    bad_day = [
        {"scan": "IN", "box": "CSNU102938", "at": "2026-11-13T08:10:00", "gate": "T1"},
        {"scan": "RS", "box": "CSNU102938", "at": "2026-11-13T08:40:00", "gate": "T2"},
        {"scan": "OUT", "box": "MSKU214", "at": "2026-11-13T09:05:00", "gate": "T1"},
        {"scan": "IN", "box": "  ", "at": "2026-11-13T09:32:00", "gate": "T3"},
        {"scan": "OUT", "box": "TCLU5566778", "at": "2026-11-13T10:02:00", "gate": "T2"},
    ]
    for payload in built:
        _land(conn, "2026-11-11", payload["gate"], payload)
    for payload in waiting:
        _land(conn, "2026-11-12", payload["gate"], payload)
    for payload in bad_day:
        _land(conn, "2026-11-13", payload["gate"], payload)
    rebuild_day(conn, "2026-11-11", "admin", None)


def _enqueue_bad_box(conn, biz_date: str, raw_box: str):
    """坏号入队，幂等；剔除/补成次数由 _recompute_queue_counts 统一重算。"""
    conn.execute(
        """
        INSERT INTO bad_box_queue (biz_date, raw_box)
        VALUES (%s, %s)
        ON CONFLICT (biz_date, raw_box) DO NOTHING
        """,
        (biz_date, raw_box),
    )


def _reject(conn, run_id: int, source_id: int, reason: str):
    conn.execute(
        "INSERT INTO build_rejects (run_id, source_id, reason) VALUES (%s, %s, %s)",
        (run_id, source_id, reason),
    )


def _recompute_queue_counts(conn, biz_date: str):
    """剔除/补成次数按当天贴源全量重算，幂等且跨收成轮次保留累计口径。

    剔除次数：当天原文箱号等于该坏号、且不合规的扫描条数（不管当前办法拦不拦）。
    补成次数：当天该坏号经补位落进明细的扫描条数。
    """
    conn.execute(
        """
        UPDATE bad_box_queue q
           SET reject_count = (
                SELECT COUNT(*)
                  FROM ods_scan o
                 WHERE o.biz_date = %s
                   AND btrim(upper(o.raw_payload->>'box')) = q.raw_box
                   AND btrim(upper(o.raw_payload->>'box')) !~ '^[A-Z]{4}[0-9]{7}$'
               )
         WHERE q.biz_date = %s
        """,
        (biz_date, biz_date),
    )
    conn.execute(
        """
        UPDATE bad_box_queue q
           SET fixed_count = (
                SELECT COUNT(*)
                  FROM ods_scan o
                 WHERE o.biz_date = q.biz_date
                   AND btrim(upper(o.raw_payload->>'box')) = q.raw_box
                   AND EXISTS (
                        SELECT 1 FROM dwd_move m
                         WHERE m.source_id = o.id AND m.box_source = 'padded'
                   )
               )
         WHERE q.biz_date = %s
        """,
        (biz_date,),
    )


def rebuild_day(conn, biz_date: str, username: str, request_policy):
    """按当前补码办法把贴源收成明细。

    request_policy 为收成请求自带的办法；与补码设置不一致的坏号整条拒写。
    """
    setting = conn.execute(
        "SELECT policy FROM box_policies WHERE biz_date = %s",
        (biz_date,),
    ).fetchone()
    configured = setting["policy"] if setting else None
    policy_mismatch = (
        request_policy is not None
        and configured is not None
        and request_policy != configured
    )

    conn.execute("DELETE FROM dwd_move WHERE biz_date = %s", (biz_date,))
    old_runs = conn.execute(
        "SELECT id FROM build_runs WHERE biz_date = %s", (biz_date,)
    ).fetchall()
    for old in old_runs:
        conn.execute("DELETE FROM build_rejects WHERE run_id = %s", (old["id"],))
    conn.execute("DELETE FROM build_runs WHERE biz_date = %s", (biz_date,))

    run = conn.execute(
        """
        INSERT INTO build_runs (biz_date, accepted_count, rejected_count, policy_used, built_by)
        VALUES (%s, 0, 0, %s, %s)
        RETURNING id
        """,
        (biz_date, request_policy, username),
    ).fetchone()

    rows = conn.execute(
        "SELECT id, terminal_code, raw_payload FROM ods_scan WHERE biz_date = %s ORDER BY id",
        (biz_date,),
    ).fetchall()
    accepted = 0
    rejected = 0
    for row in rows:
        payload = row["raw_payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        scan, raw_box, event_at, error = inspect_scan(payload)
        if error == REASON_BAD_BOX:
            _enqueue_bad_box(conn, biz_date, raw_box)
            if configured is None:
                reason = REASON_POLICY_UNSET
            elif policy_mismatch:
                reason = REASON_POLICY_MISMATCH
            elif configured == POLICY_QUEUE:
                reason = REASON_BAD_BOX
            else:
                mapping = conn.execute(
                    """
                    SELECT fixed_box FROM bad_box_queue
                     WHERE biz_date = %s AND raw_box = %s AND status = 'padded'
                    """,
                    (biz_date, raw_box),
                ).fetchone()
                if not mapping:
                    reason = REASON_BAD_BOX
                else:
                    # 走到这里事件码、时间已通过 inspect_scan 前置校验，scan/event_at 可用
                    conn.execute(
                        """
                        INSERT INTO dwd_move
                            (biz_date, source_id, terminal_code, container_no, event_kind, event_at, box_source)
                        VALUES (%s, %s, %s, %s, %s, %s, 'padded')
                        """,
                        (
                            biz_date,
                            row["id"],
                            row["terminal_code"],
                            mapping["fixed_box"],
                            KIND[scan],
                            event_at,
                        ),
                    )
                    accepted += 1
                    continue
            _reject(conn, run["id"], row["id"], reason)
            rejected += 1
            continue
        if error:
            # 全空箱号、坏事件码、坏时间：直接剔除，全空扫描不进坏号队列
            _reject(conn, run["id"], row["id"], error)
            rejected += 1
            continue
        conn.execute(
            """
            INSERT INTO dwd_move
                (biz_date, source_id, terminal_code, container_no, event_kind, event_at, box_source)
            VALUES (%s, %s, %s, %s, %s, %s, 'compliant')
            """,
            (
                biz_date,
                row["id"],
                row["terminal_code"],
                raw_box,
                KIND[scan],
                event_at,
            ),
        )
        accepted += 1

    _recompute_queue_counts(conn, biz_date)
    conn.execute(
        "UPDATE build_runs SET accepted_count = %s, rejected_count = %s WHERE id = %s",
        (accepted, rejected, run["id"]),
    )
    return run["id"]

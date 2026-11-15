import os

from flask import Flask, jsonify, request

from auth import authenticate, issue_token, read_user
from build import (
    POLICIES,
    POLICY_NAME,
    REASON_POLICY_LOCKED,
    REASON_POLICY_MISMATCH,
    REASON_POLICY_UNSET,
    REASON_PADDING_CONTENTION,
    check_fixed_box,
)
from db import connect, init_db, rebuild_day

app = Flask(__name__)
app.json.ensure_ascii = False

ATTEMPT_RESULT_NAME = {
    "padded": "补位入库",
    "blocked": "队列拦收",
    "mismatch": REASON_POLICY_MISMATCH,
    "contention": REASON_PADDING_CONTENTION,
    "rejected": "拒收",
}


def _stamp(value):
    if value is None:
        return None
    return value.isoformat(sep=" ", timespec="minutes")


def _user():
    return read_user(request.headers.get("Authorization"))


def _writer():
    user = _user()
    if not user:
        return None, (jsonify({"detail": "未登录"}), 401)
    if user["role"] != "writer":
        return None, (jsonify({"detail": "核对员只能查看，不能落库"}), 403)
    return user, None


@app.get("/api/health")
def health():
    with connect() as conn:
        conn.execute("SELECT 1")
    return jsonify({"status": "ok", "service": "gate-event-lake"})


@app.post("/api/login")
def login():
    body = request.get_json(silent=True) or {}
    user = authenticate(str(body.get("username", "")).strip(), str(body.get("password", "")))
    if not user:
        return jsonify({"detail": "用户名或密码错误"}), 401
    return jsonify(
        {
            "access_token": issue_token(user),
            "username": user["username"],
            "role": user["role"],
            "title": user["title"],
        }
    )


@app.get("/api/dates")
def list_dates():
    if not _user():
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT d.biz_date,
                   (SELECT COUNT(*) FROM ods_scan o WHERE o.biz_date = d.biz_date) AS ods_count,
                   (SELECT COUNT(*) FROM dwd_move m WHERE m.biz_date = d.biz_date) AS dwd_count,
                   EXISTS(SELECT 1 FROM build_runs r WHERE r.biz_date = d.biz_date) AS built,
                   (SELECT p.policy FROM box_policies p WHERE p.biz_date = d.biz_date) AS policy
            FROM (SELECT DISTINCT biz_date FROM ods_scan) d
            ORDER BY d.biz_date
            """
        ).fetchall()
    for row in rows:
        row["biz_date"] = row["biz_date"].isoformat()
        row["policy_name"] = POLICY_NAME.get(row["policy"])
    return jsonify(rows)


def _fetch_detail(conn, biz_date):
    ods = conn.execute(
        """
        SELECT id, terminal_code, raw_payload
        FROM ods_scan WHERE biz_date = %s ORDER BY id
        """,
        (biz_date,),
    ).fetchall()
    moves = conn.execute(
        """
        SELECT source_id, terminal_code, container_no, event_kind, event_at, box_source
        FROM dwd_move WHERE biz_date = %s ORDER BY event_at, id
        """,
        (biz_date,),
    ).fetchall()
    run = conn.execute(
        """
        SELECT id, accepted_count, rejected_count, policy_used, built_by, built_at
        FROM build_runs WHERE biz_date = %s ORDER BY id DESC LIMIT 1
        """,
        (biz_date,),
    ).fetchone()
    rejects = []
    if run:
        rejects = conn.execute(
            """
            SELECT source_id, reason FROM build_rejects
            WHERE run_id = %s ORDER BY source_id
            """,
            (run["id"],),
        ).fetchall()
    for move in moves:
        move["event_at"] = _stamp(move["event_at"])
    if run:
        run["built_at"] = _stamp(run["built_at"])
        run["policy_used_name"] = POLICY_NAME.get(run["policy_used"])
    return {"biz_date": biz_date, "ods": ods, "moves": moves, "run": run, "rejects": rejects}


@app.get("/api/dates/<biz_date>")
def date_detail(biz_date):
    if not _user():
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM ods_scan WHERE biz_date = %s LIMIT 1",
            (biz_date,),
        ).fetchone()
        if not exists:
            return jsonify({"detail": "这一天没有贴源扫描"}), 404
        payload = _fetch_detail(conn, biz_date)
    return jsonify(payload)


@app.post("/api/dates/<biz_date>/build")
def build_date(biz_date):
    user, error = _writer()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    request_policy = str(body.get("policy") or "").strip()
    if request_policy not in POLICIES:
        return jsonify({"detail": "收成请求必须带补码办法：队列拦收或缺位补位"}), 400
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM ods_scan WHERE biz_date = %s LIMIT 1",
            (biz_date,),
        ).fetchone()
        if not exists:
            return jsonify({"detail": "这一天没有贴源扫描"}), 404
        rebuild_day(conn, biz_date, user["username"], request_policy)
        conn.commit()
        payload = _fetch_detail(conn, biz_date)
    return jsonify(payload)


@app.get("/api/dates/<biz_date>/box-policy")
def get_box_policy(biz_date):
    if not _user():
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        row = conn.execute(
            "SELECT biz_date, policy, policy_name, set_by, set_at FROM box_policies WHERE biz_date = %s",
            (biz_date,),
        ).fetchone()
    if row:
        row["biz_date"] = row["biz_date"].isoformat()
        row["set_at"] = _stamp(row["set_at"])
    return jsonify({"biz_date": biz_date, "policy": row})


@app.post("/api/dates/<biz_date>/box-policy")
def set_box_policy(biz_date):
    user, error = _writer()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    policy = str(body.get("policy") or "").strip()
    if policy not in POLICIES:
        return jsonify({"detail": "补码办法只能选队列拦收或缺位补位"}), 400
    with connect() as conn:
        existing = conn.execute(
            "SELECT policy FROM box_policies WHERE biz_date = %s FOR UPDATE",
            (biz_date,),
        ).fetchone()
        if existing:
            if existing["policy"] != policy:
                return jsonify({"detail": REASON_POLICY_LOCKED}), 409
            row = conn.execute(
                "SELECT biz_date, policy, policy_name, set_by, set_at FROM box_policies WHERE biz_date = %s",
                (biz_date,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                INSERT INTO box_policies (biz_date, policy, policy_name, set_by)
                VALUES (%s, %s, %s, %s)
                RETURNING biz_date, policy, policy_name, set_by, set_at
                """,
                (biz_date, policy, POLICY_NAME[policy], user["username"]),
            ).fetchone()
        conn.commit()
    row["biz_date"] = row["biz_date"].isoformat()
    row["set_at"] = _stamp(row["set_at"])
    return jsonify({"biz_date": biz_date, "policy": row})


@app.get("/api/dates/<biz_date>/boxes")
def box_board(biz_date):
    """箱号列表 + 坏号队列 + 补码详情，一次取齐四个页面要用的数。"""
    if not _user():
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM ods_scan WHERE biz_date = %s LIMIT 1",
            (biz_date,),
        ).fetchone()
        if not exists:
            return jsonify({"detail": "这一天没有贴源扫描"}), 404
        setting = conn.execute(
            "SELECT policy, policy_name, set_by, set_at FROM box_policies WHERE biz_date = %s",
            (biz_date,),
        ).fetchone()
        queue = conn.execute(
            """
            SELECT id, raw_box, status, reject_count, fixed_count,
                   fixed_box, fixed_by, fixed_at
              FROM bad_box_queue
             WHERE biz_date = %s
             ORDER BY status DESC, id
            """,
            (biz_date,),
        ).fetchall()
        attempts = conn.execute(
            """
            SELECT id, raw_box, policy_attempt, policy_name, fixed_box,
                   result, detail, operator, attempted_at
              FROM completion_attempts
             WHERE biz_date = %s
             ORDER BY id DESC
            """,
            (biz_date,),
        ).fetchall()
        details = conn.execute(
            """
            SELECT m.source_id, m.terminal_code, m.event_kind, m.event_at,
                   m.container_no, m.box_source,
                   btrim(upper(o.raw_payload->>'box')) AS raw_box
              FROM dwd_move m
              JOIN ods_scan o ON o.id = m.source_id
             WHERE m.biz_date = %s
             ORDER BY m.event_at, m.id
            """,
            (biz_date,),
        ).fetchall()
    if setting:
        setting["set_at"] = _stamp(setting["set_at"])
    for row in queue:
        row["fixed_at"] = _stamp(row["fixed_at"])
        row["policy_name"] = setting["policy_name"] if setting else None
        row["status_name"] = {
            "pending": "待处理",
            "padded": "已补位",
            "blocked": "已拦收",
        }.get(row["status"], row["status"])
    for row in attempts:
        row["attempted_at"] = _stamp(row["attempted_at"])
        row["result_name"] = ATTEMPT_RESULT_NAME.get(row["result"], row["result"])
    for row in details:
        row["event_at"] = _stamp(row["event_at"])
    return jsonify(
        {
            "biz_date": biz_date,
            "policy": setting,
            "queue": queue,
            "attempts": attempts,
            "details": details,
        }
    )


def _record_attempt(conn, biz_date, raw_box, policy, fixed_box, result, detail, operator):
    conn.execute(
        """
        INSERT INTO completion_attempts
            (biz_date, raw_box, policy_attempt, policy_name, fixed_box, result, detail, operator)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            biz_date,
            raw_box,
            policy,
            POLICY_NAME[policy],
            fixed_box,
            result,
            detail,
            operator,
        ),
    )


@app.post("/api/dates/<biz_date>/completions")
def resolve_box(biz_date):
    """两侧作业员抢同一条坏箱号：对队列行加锁，只放行与补码设置相符的第一记。"""
    user, error = _writer()
    if error:
        return error
    body = request.get_json(silent=True) or {}
    policy = str(body.get("policy") or "").strip()
    raw_box = str(body.get("raw_box") or "").strip().upper().replace(" ", "")
    fixed_box = str(body.get("fixed_box") or "").strip().upper().replace(" ", "")
    if policy not in POLICIES:
        return jsonify({"detail": "补码办法只能选队列拦收或缺位补位"}), 400
    if not raw_box:
        return jsonify({"detail": "坏箱号不能为空，全空扫描不在坏号队列"}), 400

    with connect() as conn:
        setting = conn.execute(
            "SELECT policy FROM box_policies WHERE biz_date = %s",
            (biz_date,),
        ).fetchone()
        if not setting:
            return jsonify({"detail": REASON_POLICY_UNSET}), 409

        # 行锁串行化同一条坏箱号的并发补码
        queued = conn.execute(
            "SELECT id, status FROM bad_box_queue WHERE biz_date = %s AND raw_box = %s FOR UPDATE",
            (biz_date, raw_box),
        ).fetchone()
        if not queued:
            return jsonify({"detail": "该箱号不在坏号队列，不能补码"}), 404

        if policy != setting["policy"]:
            _record_attempt(
                conn, biz_date, raw_box, policy, None,
                "mismatch", REASON_POLICY_MISMATCH, user["username"],
            )
            conn.commit()
            return jsonify({"detail": REASON_POLICY_MISMATCH}), 409

        if queued["status"] != "pending":
            _record_attempt(
                conn, biz_date, raw_box, policy, fixed_box or None,
                "contention", REASON_PADDING_CONTENTION, user["username"],
            )
            conn.commit()
            return jsonify({"detail": REASON_PADDING_CONTENTION}), 409

        if setting["policy"] == "pad_to_detail":
            fixed_box, fix_error = check_fixed_box(raw_box, fixed_box)
            if fix_error:
                _record_attempt(
                    conn, biz_date, raw_box, policy, None,
                    "rejected", fix_error, user["username"],
                )
                conn.commit()
                return jsonify({"detail": fix_error}), 400
            conn.execute(
                """
                UPDATE bad_box_queue
                   SET status = 'padded', fixed_box = %s,
                       fixed_by = %s, fixed_at = NOW()
                 WHERE id = %s
                """,
                (fixed_box, user["username"], queued["id"]),
            )
            _record_attempt(
                conn, biz_date, raw_box, policy, fixed_box,
                "padded", "补齐缺位后入库到明细", user["username"],
            )
        else:
            conn.execute(
                """
                UPDATE bad_box_queue
                   SET status = 'blocked', fixed_by = %s, fixed_at = NOW()
                 WHERE id = %s
                """,
                (user["username"], queued["id"]),
            )
            _record_attempt(
                conn, biz_date, raw_box, policy, None,
                "blocked", "挡在坏号队列不收成", user["username"],
            )
        conn.commit()
    return jsonify({"ok": True})


init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))

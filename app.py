from __future__ import annotations

import io
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import qrcode
import qrcode.image.svg
from flask import Flask, Response, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("GAME_DB", str(BASE_DIR / "game.db"))
ROUND_SECONDS = 120
SESSION_TTL = 6 * 60 * 60

with open(BASE_DIR / "questions.json", "r", encoding="utf-8") as f:
    QUESTIONS = json.load(f)
with open(BASE_DIR / "escape_questions.json", "r", encoding="utf-8") as f:
    ESCAPE_QUESTIONS = json.load(f)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config["JSON_AS_ASCII"] = False

# Game 4: Code Race – Bài 24
from code_race import code_race_bp
app.register_blueprint(code_race_bp)

# -------------------- INSERTION SORT CLASSROOM --------------------
def now_ms() -> int:
    return int(time.time() * 1000)


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS rooms (
                code TEXT PRIMARY KEY,
                teacher_key TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'lobby',
                started_at INTEGER,
                ends_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS participants (
                id TEXT PRIMARY KEY,
                room_code TEXT NOT NULL,
                name TEXT NOT NULL,
                joined_at INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                UNIQUE(room_code, name)
            );
            CREATE TABLE IF NOT EXISTS results (
                participant_id TEXT PRIMARY KEY,
                room_code TEXT NOT NULL,
                name TEXT NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0,
                completed_at INTEGER,
                elapsed_ms INTEGER,
                FOREIGN KEY(participant_id) REFERENCES participants(id)
            );
            """
        )


init_db()


def random_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    for _ in range(30):
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        with db() as c:
            if not c.execute("SELECT 1 FROM rooms WHERE code=?", (code,)).fetchone():
                return code
    raise RuntimeError("Could not create room code")


def get_room(code: str | None):
    code = (code or "").strip().upper()
    with db() as c:
        row = c.execute("SELECT * FROM rooms WHERE code=?", (code,)).fetchone()
    return dict(row) if row else None


def normalized_room(room):
    if not room:
        return None
    room = dict(room)
    if room["status"] == "playing" and room.get("ends_at") and now_ms() >= room["ends_at"]:
        with db() as c:
            c.execute("UPDATE rooms SET status='finished' WHERE code=? AND status='playing'", (room["code"],))
        room["status"] = "finished"
    return room


def require_teacher(room, key: str | None) -> bool:
    return bool(room and secrets.compare_digest(room["teacher_key"], key or ""))


@app.get("/")
def home():
    # Backward compatibility for QR codes created before this site became a game hub.
    if request.args.get("room"):
        return redirect(url_for("insertion_game", **request.args))
    return render_template("index.html")


@app.get("/insertion-sort")
def insertion_game():
    return render_template("insertion.html")


@app.post("/api/rooms")
def create_room():
    code = random_code()
    key = secrets.token_urlsafe(24)
    with db() as c:
        c.execute(
            "INSERT INTO rooms(code,teacher_key,created_at,status) VALUES(?,?,?,?)",
            (code, key, now_ms(), "lobby"),
        )
    return jsonify({"ok": True, "code": code, "teacher_key": key, "round_seconds": ROUND_SECONDS})


@app.get("/api/rooms/<code>/status")
def room_status(code):
    room = normalized_room(get_room(code))
    if not room:
        return jsonify({"ok": False, "error": "Không tìm thấy phòng"}), 404
    with db() as c:
        participants = c.execute(
            "SELECT COUNT(*) n FROM participants WHERE room_code=?", (room["code"],)
        ).fetchone()["n"]
        completed = c.execute(
            "SELECT COUNT(*) n FROM results WHERE room_code=? AND completed=1", (room["code"],)
        ).fetchone()["n"]
    remaining = 0
    if room["status"] == "playing" and room.get("ends_at"):
        remaining = max(0, room["ends_at"] - now_ms())
    return jsonify(
        {
            "ok": True,
            "code": room["code"],
            "status": room["status"],
            "started_at": room.get("started_at"),
            "ends_at": room.get("ends_at"),
            "remaining_ms": remaining,
            "participants": participants,
            "completed": completed,
            "round_seconds": ROUND_SECONDS,
        }
    )


@app.post("/api/rooms/<code>/join")
def join_room(code):
    room = normalized_room(get_room(code))
    if not room:
        return jsonify({"ok": False, "error": "Mã phòng không đúng"}), 404
    data = request.get_json(silent=True) or {}
    name = " ".join(str(data.get("name", "")).split())[:50]
    if len(name) < 2:
        return jsonify({"ok": False, "error": "Hãy nhập họ tên"}), 400
    pid = secrets.token_urlsafe(18)
    ts = now_ms()
    with db() as c:
        old = c.execute(
            "SELECT id FROM participants WHERE room_code=? AND name=?", (room["code"], name)
        ).fetchone()
        if old:
            pid = old["id"]
            c.execute("UPDATE participants SET last_seen=? WHERE id=?", (ts, pid))
        else:
            c.execute(
                "INSERT INTO participants(id,room_code,name,joined_at,last_seen) VALUES(?,?,?,?,?)",
                (pid, room["code"], name, ts, ts),
            )
    return jsonify({"ok": True, "participant_id": pid, "name": name, "status": room["status"]})


@app.post("/api/rooms/<code>/start")
def start_room(code):
    room = get_room(code)
    data = request.get_json(silent=True) or {}
    if not require_teacher(room, data.get("teacher_key")):
        return jsonify({"ok": False, "error": "Không có quyền giáo viên"}), 403
    start = now_ms()
    end = start + ROUND_SECONDS * 1000
    with db() as c:
        c.execute(
            "UPDATE rooms SET status='playing', started_at=?, ends_at=? WHERE code=?",
            (start, end, room["code"]),
        )
        c.execute("DELETE FROM results WHERE room_code=?", (room["code"],))
    return jsonify({"ok": True, "started_at": start, "ends_at": end, "round_seconds": ROUND_SECONDS})


@app.post("/api/rooms/<code>/finish")
def finish_room(code):
    room = get_room(code)
    data = request.get_json(silent=True) or {}
    if not require_teacher(room, data.get("teacher_key")):
        return jsonify({"ok": False, "error": "Không có quyền giáo viên"}), 403
    with db() as c:
        c.execute("UPDATE rooms SET status='finished' WHERE code=?", (room["code"],))
    return jsonify({"ok": True})


@app.post("/api/rooms/<code>/reset")
def reset_room(code):
    room = get_room(code)
    data = request.get_json(silent=True) or {}
    if not require_teacher(room, data.get("teacher_key")):
        return jsonify({"ok": False, "error": "Không có quyền giáo viên"}), 403
    with db() as c:
        c.execute(
            "UPDATE rooms SET status='lobby', started_at=NULL, ends_at=NULL WHERE code=?",
            (room["code"],),
        )
        c.execute("DELETE FROM results WHERE room_code=?", (room["code"],))
    return jsonify({"ok": True})


@app.post("/api/rooms/<code>/submit")
def submit_result(code):
    room = normalized_room(get_room(code))
    data = request.get_json(silent=True) or {}
    pid = str(data.get("participant_id", ""))
    if not room or room["status"] not in ("playing", "finished"):
        return jsonify({"ok": False, "error": "Phòng chưa bắt đầu hoặc không tồn tại"}), 400
    with db() as c:
        p = c.execute(
            "SELECT * FROM participants WHERE id=? AND room_code=?", (pid, room["code"])
        ).fetchone()
        if not p:
            return jsonify({"ok": False, "error": "Không xác định được học sinh"}), 403
        completed_at = now_ms()
        in_time = bool(room.get("ends_at")) and completed_at <= room["ends_at"]
        elapsed = max(0, completed_at - room["started_at"]) if room.get("started_at") else None
        c.execute(
            """INSERT INTO results(participant_id,room_code,name,completed,completed_at,elapsed_ms)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(participant_id) DO UPDATE SET completed=excluded.completed,
               completed_at=excluded.completed_at, elapsed_ms=excluded.elapsed_ms""",
            (pid, room["code"], p["name"], 1 if in_time else 0, completed_at, elapsed),
        )
    return jsonify({"ok": True, "accepted": in_time, "elapsed_ms": elapsed})


@app.get("/api/rooms/<code>/results")
def insertion_results(code):
    room = normalized_room(get_room(code))
    key = request.args.get("teacher_key", "")
    if not require_teacher(room, key):
        return jsonify({"ok": False, "error": "Không có quyền giáo viên"}), 403
    with db() as c:
        people = c.execute(
            "SELECT id,name,joined_at,last_seen FROM participants WHERE room_code=? ORDER BY joined_at",
            (room["code"],),
        ).fetchall()
        rows = c.execute(
            "SELECT participant_id,name,completed,completed_at,elapsed_ms FROM results WHERE room_code=? ORDER BY completed DESC, elapsed_ms ASC",
            (room["code"],),
        ).fetchall()
    result_map = {r["participant_id"]: dict(r) for r in rows}
    merged = []
    for p in people:
        r = result_map.get(p["id"])
        merged.append(
            {
                "id": p["id"],
                "name": p["name"],
                "completed": bool(r and r["completed"]),
                "elapsed_ms": r["elapsed_ms"] if r else None,
            }
        )
    merged.sort(
        key=lambda x: (
            0 if x["completed"] else 1,
            x["elapsed_ms"] if x["elapsed_ms"] is not None else 10**15,
            x["name"],
        )
    )
    return jsonify(
        {
            "ok": True,
            "status": room["status"],
            "participants": len(people),
            "completed": sum(1 for x in merged if x["completed"]),
            "results": merged,
            "ends_at": room.get("ends_at"),
            "started_at": room.get("started_at"),
        }
    )


@app.get("/qr/<code>.png")
def insertion_qr(code):
    room = get_room(code)
    if not room:
        return "Room not found", 404
    target = url_for("insertion_game", room=room["code"], role="student", _external=True)
    img = qrcode.make(target)
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return send_file(bio, mimetype="image/png", max_age=30)


# -------------------- MILLIONAIRE CLASSROOM --------------------
SESSIONS: dict[str, dict[str, Any]] = {}
LOCK = threading.RLock()


def now() -> float:
    return time.time()


def cleanup_sessions() -> None:
    cutoff = now() - SESSION_TTL
    with LOCK:
        stale = [gid for gid, s in SESSIONS.items() if s.get("created_at", 0) < cutoff]
        for gid in stale:
            SESSIONS.pop(gid, None)


def new_game_id() -> str:
    return secrets.token_hex(4).upper()


def get_session(game_id: str):
    with LOCK:
        s = SESSIONS.get(game_id)
        if s and s.get("audience", {}).get("state") == "open":
            ends_at = float(s["audience"].get("ends_at") or 0)
            if ends_at and now() >= ends_at:
                s["audience"]["state"] = "closed"
        return s


def teacher_ok(s) -> bool:
    if not s:
        return False
    token = request.headers.get("X-Teacher-Token", "")
    return bool(token) and secrets.compare_digest(token, s.get("teacher_token", ""))


@app.get("/millionaire")
def millionaire_game():
    return render_template("millionaire.html", questions=QUESTIONS)


@app.post("/api/millionaire/new")
def api_new_game():
    cleanup_sessions()
    game_id = new_game_id()
    token = secrets.token_urlsafe(24)
    with LOCK:
        SESSIONS[game_id] = {
            "created_at": now(),
            "teacher_token": token,
            "audience": {"state": "idle", "question_index": None, "ends_at": None, "votes": {}},
        }
    audience_url = url_for("audience_page", game_id=game_id, _external=True)
    qr_url = url_for("qr_svg", game_id=game_id, _external=True)
    return jsonify(
        {
            "ok": True,
            "game_id": game_id,
            "teacher_token": token,
            "audience_url": audience_url,
            "qr_url": qr_url,
        }
    )


@app.get("/millionaire/audience/<game_id>")
def audience_page(game_id: str):
    if not get_session(game_id):
        return render_template("audience.html", game_id=game_id, questions=QUESTIONS, missing=True), 404
    return render_template("audience.html", game_id=game_id, questions=QUESTIONS, missing=False)


@app.get("/millionaire/qr/<game_id>.svg")
def qr_svg(game_id: str):
    if not get_session(game_id):
        return Response("Không tìm thấy phiên chơi", status=404)
    target = url_for("audience_page", game_id=game_id, _external=True)
    factory = qrcode.image.svg.SvgPathImage
    img = qrcode.make(target, image_factory=factory, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), mimetype="image/svg+xml", headers={"Cache-Control": "no-store"})


@app.post("/api/millionaire/<game_id>/audience/prepare")
def audience_prepare(game_id: str):
    s = get_session(game_id)
    if not teacher_ok(s):
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    data = request.get_json(silent=True) or {}
    qi = data.get("question_index")
    if not isinstance(qi, int) or not (0 <= qi < len(QUESTIONS)):
        return jsonify({"ok": False, "error": "invalid_question"}), 400
    with LOCK:
        s["audience"] = {"state": "waiting", "question_index": qi, "ends_at": None, "votes": {}}
    return jsonify({"ok": True})


@app.post("/api/millionaire/<game_id>/audience/start")
def audience_start(game_id: str):
    s = get_session(game_id)
    if not teacher_ok(s):
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    with LOCK:
        a = s["audience"]
        if a.get("state") not in ("waiting", "closed") or a.get("question_index") is None:
            return jsonify({"ok": False, "error": "not_prepared"}), 409
        a["state"] = "open"
        a["ends_at"] = now() + 15
        a["votes"] = {}
    return jsonify({"ok": True, "ends_at": s["audience"]["ends_at"]})


@app.post("/api/millionaire/<game_id>/audience/close")
def audience_close(game_id: str):
    s = get_session(game_id)
    if not teacher_ok(s):
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    with LOCK:
        if s["audience"].get("state") in ("open", "waiting"):
            s["audience"]["state"] = "closed"
    return jsonify({"ok": True})


@app.get("/api/millionaire/<game_id>/audience/status")
def audience_status(game_id: str):
    s = get_session(game_id)
    if not s:
        return jsonify({"ok": False, "error": "not_found"}), 404
    with LOCK:
        a = s["audience"]
        counts = [0, 0, 0, 0]
        for choice in a.get("votes", {}).values():
            if isinstance(choice, int) and 0 <= choice < 4:
                counts[choice] += 1
        payload = {
            "ok": True,
            "state": a.get("state", "idle"),
            "question_index": a.get("question_index"),
            "ends_at": a.get("ends_at"),
            "counts": counts,
            "total": sum(counts),
        }
    return jsonify(payload)


@app.post("/api/millionaire/<game_id>/audience/vote")
def audience_vote(game_id: str):
    s = get_session(game_id)
    if not s:
        return jsonify({"ok": False, "error": "not_found"}), 404
    data = request.get_json(silent=True) or {}
    voter_id = str(data.get("voter_id") or "")[:100]
    choice = data.get("choice")
    qi = data.get("question_index")
    if not voter_id or not isinstance(choice, int) or not (0 <= choice < 4):
        return jsonify({"ok": False, "error": "invalid_vote"}), 400
    with LOCK:
        a = s["audience"]
        if a.get("state") != "open":
            return jsonify({"ok": False, "error": "closed"}), 409
        if now() >= float(a.get("ends_at") or 0):
            a["state"] = "closed"
            return jsonify({"ok": False, "error": "closed"}), 409
        if qi != a.get("question_index"):
            return jsonify({"ok": False, "error": "wrong_question"}), 409
        if voter_id in a["votes"]:
            return jsonify({"ok": False, "error": "already_voted"}), 409
        a["votes"][voter_id] = choice
    return jsonify({"ok": True})


# -------------------- ESCAPE ROOM BÀI 29 --------------------
ESCAPE_ROOMS: dict[str, dict[str, Any]] = {}

def escape_new_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        if code not in ESCAPE_ROOMS:
            return code

def escape_room_or_none(code: str | None):
    return ESCAPE_ROOMS.get((code or "").strip().upper())

def escape_refresh(room) -> None:
    if not room:
        return
    if room["phase"] == "question" and room.get("started_at"):
        elapsed = time.time() - room["started_at"]
        if elapsed >= 20:
            qidx = room["qidx"]
            correct_answer = ESCAPE_QUESTIONS[qidx]["answer"]
            # Chốt kết quả sau đúng 20 giây. Không trả lời cũng tính là sai và bị loại.
            for sid, stu in room["students"].items():
                if not stu["active"]:
                    continue
                ans = room["answers"].get(sid)
                if ans is None:
                    stu["active"] = False
                    stu["eliminated_at"] = qidx
                    stu["eliminated_reason"] = "no_answer"
                elif ans != correct_answer:
                    stu["active"] = False
                    stu["eliminated_at"] = qidx
                    stu["eliminated_reason"] = "wrong"
            room["phase"] = "review"
            room["ended_at"] = time.time()

def escape_public_state(room, sid: str | None = None, teacher: bool = False):
    escape_refresh(room)
    qidx = room["qidx"]
    q = ESCAPE_QUESTIONS[qidx] if 0 <= qidx < len(ESCAPE_QUESTIONS) else None
    active = sum(1 for s in room["students"].values() if s["active"])
    joined = len(room["students"])
    answered = len(room["answers"]) if room["phase"] in ("question", "review") else 0
    correct = 0
    wrong = 0
    if q is not None:
        correct = sum(1 for a in room["answers"].values() if a == q["answer"])
        wrong = sum(1 for a in room["answers"].values() if a != q["answer"])
    start_active = int(room.get("start_active", active))
    unanswered = max(0, start_active - answered) if room["phase"] in ("question", "review") else 0
    eliminated_this_question = wrong + (unanswered if room["phase"] == "review" else 0)
    data = {
        "ok": True,
        "code": room["code"],
        "phase": room["phase"],
        "qidx": qidx,
        "total_questions": len(ESCAPE_QUESTIONS),
        "active": active,
        "joined": joined,
        "answered": answered,
        "correct": correct,
        "wrong": wrong,
        "unanswered": unanswered,
        "eliminated_this_question": eliminated_this_question,
        "start_active": start_active,
        "next_eligible": active,
        "finished_reason": room.get("finished_reason"),
        "room_no": q["room"] if q else None,
        "room_name": q["room_name"] if q else None,
    }
    if q:
        data["question"] = {
            "room": q["room"],
            "room_name": q["room_name"],
            "icon": q["icon"],
            "prompt": q["prompt"],
            "code": q["code"],
            "choices": q["choices"],
        }
    if room["phase"] == "question":
        data["remain"] = max(0, 20 - int(time.time() - room["started_at"]))
    else:
        data["remain"] = 0
    if room["phase"] == "review" and q:
        data["answer"] = q["answer"]
        data["answer_text"] = q["choices"][q["answer"]]
        data["explain"] = q["explain"]
    if sid:
        stu = room["students"].get(sid)
        if stu:
            data["student"] = {
                "name": stu["name"],
                "active": stu["active"],
                "eliminated_at": stu.get("eliminated_at"),
                "eliminated_reason": stu.get("eliminated_reason"),
            }
            if sid in room["answers"]:
                data["my_answer"] = room["answers"][sid]
                data["my_correct"] = room["answers"][sid] == q["answer"] if q else False
    if teacher:
        data["students"] = [
            {"name": s["name"], "active": s["active"]} for s in room["students"].values()
        ]
    return data

@app.get("/escape-bai29")
def escape_bai29_teacher():
    return render_template("escape_teacher.html")

@app.get("/escape-bai29/join")
def escape_bai29_join():
    return render_template("escape_student.html")

@app.post("/api/escape/create")
def api_escape_create():
    code = escape_new_code()
    ESCAPE_ROOMS[code] = {
        "code": code,
        "phase": "lobby",
        "qidx": 0,
        "started_at": None,
        "students": {},
        "answers": {},
        "start_active": 0,
        "created_at": time.time(),
    }
    return jsonify({
        "ok": True,
        "code": code,
        "join_url": request.host_url.rstrip("/") + "/escape-bai29/join?room=" + code,
    })

@app.post("/api/escape/join")
def api_escape_join():
    body = request.get_json(force=True)
    room = escape_room_or_none(body.get("room"))
    if not room:
        return jsonify({"ok": False, "error": "Không tìm thấy phòng"}), 404
    # Khóa danh sách khi Câu 1 bắt đầu để tổng số học sinh đủ quyền chơi luôn chính xác.
    if room["phase"] != "lobby":
        return jsonify({"ok": False, "error": "Phòng đã bắt đầu. Em hãy tham gia ở lượt chơi mới."}), 409
    name = str(body.get("name") or "").strip()[:40]
    if not name:
        return jsonify({"ok": False, "error": "Hãy nhập họ tên"}), 400
    sid = secrets.token_urlsafe(12)
    room["students"][sid] = {
        "name": name,
        "active": True,
        "eliminated_at": None,
        "eliminated_reason": None,
    }
    return jsonify({"ok": True, "sid": sid})

@app.get("/api/escape/state")
def api_escape_state():
    room = escape_room_or_none(request.args.get("room"))
    if not room:
        return jsonify({"ok": False, "error": "Không tìm thấy phòng"}), 404
    sid = request.args.get("sid")
    teacher = request.args.get("teacher") == "1"
    return jsonify(escape_public_state(room, sid=sid, teacher=teacher))

@app.post("/api/escape/start")
def api_escape_start():
    body = request.get_json(force=True)
    room = escape_room_or_none(body.get("room"))
    if not room:
        return jsonify({"ok": False, "error": "Không tìm thấy phòng"}), 404
    escape_refresh(room)
    if room["phase"] not in ("lobby", "review"):
        return jsonify({"ok": False, "error": "Không thể bắt đầu lúc này"}), 400
    active_count = sum(1 for s in room["students"].values() if s["active"])
    if room["phase"] == "lobby" and active_count == 0:
        return jsonify({"ok": False, "error": "Chưa có học sinh nào tham gia phòng."}), 400
    if room["phase"] == "review":
        if active_count == 0:
            room["phase"] = "finished"
            room["finished_reason"] = "no_survivors"
            return jsonify({"ok": True, "finished": True})
        room["qidx"] += 1
    if room["qidx"] >= len(ESCAPE_QUESTIONS):
        room["phase"] = "finished"
        room["finished_reason"] = "completed"
        return jsonify({"ok": True, "finished": True})
    room["answers"] = {}
    room["start_active"] = sum(1 for s in room["students"].values() if s["active"])
    room["phase"] = "question"
    room["started_at"] = time.time()
    return jsonify({"ok": True})

@app.post("/api/escape/answer")
def api_escape_answer():
    body = request.get_json(force=True)
    room = escape_room_or_none(body.get("room"))
    if not room:
        return jsonify({"ok": False, "error": "Không tìm thấy phòng"}), 404
    escape_refresh(room)
    sid = body.get("sid")
    stu = room["students"].get(sid)
    if not stu or not stu["active"]:
        return jsonify({"ok": False, "error": "Bạn đã bị loại"}), 403
    if room["phase"] != "question":
        return jsonify({"ok": False, "error": "Câu hỏi đã kết thúc"}), 400
    if sid in room["answers"]:
        return jsonify({"ok": False, "error": "Bạn đã trả lời"}), 400
    try:
        ans = int(body.get("answer"))
    except Exception:
        return jsonify({"ok": False, "error": "Đáp án không hợp lệ"}), 400
    room["answers"][sid] = ans
    correct = ans == ESCAPE_QUESTIONS[room["qidx"]]["answer"]
    if not correct:
        stu["active"] = False
        stu["eliminated_at"] = room["qidx"]
        stu["eliminated_reason"] = "wrong"
    return jsonify({"ok": True, "correct": correct})

@app.get("/escape-bai29/qr.png")
def escape_bai29_qr():
    code = (request.args.get("room") or "").strip().upper()
    if not code:
        return Response("Thiếu mã phòng", status=400)
    url = request.host_url.rstrip("/") + "/escape-bai29/join?room=" + code
    img = qrcode.make(url)
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return send_file(bio, mimetype="image/png", max_age=30)



# ============================================================================
# GAME 5: DAU TRUONG TRI THUC - 10-12 DOI - FLASK / RENDER
# Self-contained: all 10 questions and server API are included in app.py.
# The only new HTML file is templates/team_arena.html.
# ============================================================================
import hashlib
import unicodedata
from contextlib import contextmanager
from flask import Blueprint

arena_bp = Blueprint("team_arena", __name__)
ARENA_BASE_DIR = BASE_DIR
ARENA_DB_PATH = DB_PATH
ARENA_QUESTIONS = json.loads(r'''
[
  {
    "number": 1,
    "difficulty": "Trung bình",
    "blocks": [
      {
        "type": "text",
        "value": "Để định nghĩa một hàm do người dùng tự tạo trong Python, ta bắt buộc phải sử dụng từ khóa nào sau đây?"
      }
    ],
    "options": [
      "function",
      "define",
      "def",
      "fun"
    ],
    "answer": 2,
    "explanation": "Từ khóa def được dùng để định nghĩa hàm trong Python."
  },
  {
    "number": 2,
    "difficulty": "Trung bình",
    "blocks": [
      {
        "type": "text",
        "value": "Cú pháp gọi một hàm đã được thiết kế sẵn hoặc tự định nghĩa trong Python có dạng tổng quát nào?"
      }
    ],
    "options": [
      "<tên hàm>[danh sách đối số]",
      "<tên hàm>(danh sách đối số)",
      "<danh sách đối số>(tên hàm)",
      "call <tên hàm>"
    ],
    "answer": 1,
    "explanation": "Cú pháp gọi hàm là <tên hàm>(danh sách đối số). Đối số là dữ liệu được truyền vào khi gọi hàm; tham số được khai báo khi định nghĩa hàm."
  },
  {
    "number": 3,
    "difficulty": "Trung bình",
    "blocks": [
      {
        "type": "text",
        "value": "Phát biểu nào sau đây là ĐÚNG khi nói về tham số của hàm trong Python?"
      }
    ],
    "options": [
      "Một hàm luôn luôn phải có ít nhất một tham số.",
      "Tham số của hàm được đặt trong cặp dấu ngoặc vuông [ ].",
      "Hàm có thể có một, nhiều tham số hoặc không có tham số nào.",
      "Chỉ những hàm do Python thiết kế sẵn mới có tham số."
    ],
    "answer": 2,
    "explanation": "Khi định nghĩa hàm, có thể khai báo không có tham số, một tham số hoặc nhiều tham số. Ví dụ, phần đầu def chao(): khai báo một hàm không có tham số."
  },
  {
    "number": 4,
    "difficulty": "Khá",
    "blocks": [
      {
        "type": "text",
        "value": "Khi đang thực thi một hàm, nếu chương trình gặp lệnh \"return\" thì điều gì sẽ xảy ra?"
      }
    ],
    "options": [
      "Hàm sẽ tiếp tục chạy các lệnh bên dưới lệnh return cho đến hết.",
      "Hàm sẽ ngay lập tức kết thúc và trả về giá trị (nếu có) cho nơi gọi hàm.",
      "Chương trình sẽ báo lỗi cú pháp vì lệnh return chỉ được dùng ở cuối chương trình chính.",
      "Hàm sẽ khởi động lại từ đầu."
    ],
    "answer": 1,
    "explanation": "Lệnh return đóng vai trò kết thúc hàm ngay lập tức và trả giá trị (nếu được chỉ định) về cho nơi gọi hàm."
  },
  {
    "number": 5,
    "difficulty": "Khá",
    "blocks": [
      {
        "type": "text",
        "value": "Cho đoạn mã lệnh sau:"
      },
      {
        "type": "code",
        "value": "def xinchao(ten):\n    print(\"Chào mừng\", ten)"
      },
      {
        "type": "text",
        "value": "Hàm trên thuộc loại hàm nào và giá trị trả lại là gì nếu ta gọi xinchao(\"Minh\")?"
      }
    ],
    "options": [
      "Là hàm có trả lại giá trị, giá trị trả lại là xâu \"Chào mừng Minh\".",
      "Hàm không có lệnh return, in ra \"Chào mừng Minh\" và lời gọi hàm trả về None.",
      "Là hàm có trả lại giá trị, giá trị trả lại là biến \"ten\".",
      "Báo lỗi vì thiếu lệnh return ở cuối hàm."
    ],
    "answer": 1,
    "explanation": "Lời gọi xinchao(\"Minh\") hiển thị \"Chào mừng Minh\" nhờ lệnh print. Vì hàm không có lệnh return, lời gọi hàm trả về giá trị None. Kết quả in ra màn hình không phải giá trị trả về."
  },
  {
    "number": 6,
    "difficulty": "Khá",
    "blocks": [
      {
        "type": "text",
        "value": "Mục đích chính yếu nhất của việc gom các khối lệnh thành các chương trình con (hàm) là gì?"
      }
    ],
    "options": [
      "Để máy tính thực thi chương trình với tốc độ phần cứng nhanh hơn.",
      "Để mã hóa mã nguồn, ngăn người khác đọc được cấu trúc chương trình.",
      "Để tự động phát hiện mọi lỗi cú pháp trong Python.",
      "Tránh việc phải viết đi viết lại nhiều lần một khối lệnh, giúp chương trình gọn gàng và dễ quản lý."
    ],
    "answer": 3,
    "explanation": "Sử dụng hàm giúp tránh lặp lại mã lệnh, tiết kiệm công sức và làm chương trình dễ hiểu, dễ gỡ lỗi hơn."
  },
  {
    "number": 7,
    "difficulty": "Khá",
    "blocks": [
      {
        "type": "text",
        "value": "Xét đoạn chương trình sau:"
      },
      {
        "type": "code",
        "value": "def tinh_toan(x, y):\n    return x * y\n    print(\"Đã tính xong\")\n\nket_qua = tinh_toan(3, 4)"
      },
      {
        "type": "text",
        "value": "Sau khi đoạn chương trình chạy xong, điều gì sẽ xảy ra?"
      }
    ],
    "options": [
      "Biến ket_qua nhận giá trị 12 và màn hình in ra chữ \"Đã tính xong\".",
      "Biến ket_qua nhận giá trị 12, màn hình KHÔNG in ra gì vì lệnh print nằm sau return.",
      "Chương trình báo lỗi vì không được đặt lệnh print bên trong hàm.",
      "Màn hình in ra \"Đã tính xong\" nhưng biến ket_qua không có giá trị."
    ],
    "answer": 1,
    "explanation": "Khi gặp lệnh return x * y, hàm kết thúc và trả về 12. Câu lệnh print(\"Đã tính xong\") nằm phía sau return sẽ không bao giờ được thực thi (unreachable code)."
  },
  {
    "number": 8,
    "difficulty": "Khá",
    "blocks": [
      {
        "type": "text",
        "value": "Nếu muốn viết một hàm nhận vào một số nguyên x và trả về lập phương của số đó, cách viết lệnh return nào sau đây là đúng chuẩn?"
      }
    ],
    "options": [
      "print(x * x * x)",
      "return x * x * x",
      "output(x ** 3)",
      "return"
    ],
    "answer": 1,
    "explanation": "Để trả về giá trị lập phương, ta cần tính toán (x * x * x hoặc x ** 3) và kết hợp với từ khóa return."
  },
  {
    "number": 9,
    "difficulty": "Khó",
    "blocks": [
      {
        "type": "text",
        "value": "Phân tích đoạn chương trình sau:"
      },
      {
        "type": "code",
        "value": "def kiem_tra(so):\n    if so % 2 == 0:\n        return \"Chẵn\"\n    return \"Lẻ\"\n\na = kiem_tra(5)"
      },
      {
        "type": "text",
        "value": "Tại sao hàm trên lại hoạt động hợp lệ dù có đến hai lệnh return? Giá trị của biến a là bao nhiêu?"
      }
    ],
    "options": [
      "Hàm bị lỗi do có hai lệnh return, a không nhận giá trị nào.",
      "Hàm chạy hợp lệ. Vì 5 không chia hết cho 2, lệnh return đầu tiên bị bỏ qua, hàm chạy đến lệnh return thứ hai. a nhận giá trị \"Lẻ\".",
      "Hàm chạy hợp lệ. Hàm sẽ trả về cả hai giá trị \"Chẵn\" và \"Lẻ\" cùng lúc.",
      "Hàm chạy hợp lệ nhưng a nhận giá trị là None vì thiếu khối else."
    ],
    "answer": 1,
    "explanation": "Hàm có thể chứa nhiều lệnh return. Khi gọi kiem_tra(5), điều kiện if sai, chương trình bỏ qua return đầu tiên và thực thi return \"Lẻ\", sau đó hàm kết thúc."
  },
  {
    "number": 10,
    "difficulty": "Khó",
    "blocks": [
      {
        "type": "text",
        "value": "Cho đoạn mã Python sau:"
      },
      {
        "type": "code",
        "value": "def xu_ly_xau(s):\n    dem = 0\n    for k in s:\n        if k == \"h\":\n            dem = dem + 1\n    return dem\n\nso_luong = xu_ly_xau(\"hoc sinh hoc tin hoc\")"
      },
      {
        "type": "text",
        "value": "Yêu cầu học sinh theo dõi logic của vòng lặp và điều kiện bên trong hàm. Giá trị cuối cùng của biến \"so_luong\" là bao nhiêu?"
      }
    ],
    "options": [
      "1",
      "2",
      "3",
      "4"
    ],
    "answer": 3,
    "explanation": "Vòng lặp duyệt từng kí tự và tăng biến dem mỗi khi gặp kí tự \"h\". Xâu \"hoc sinh hoc tin hoc\" có 4 chữ \"h\": một trong \"hoc\" đầu, một trong \"sinh\", một trong \"hoc\" giữa và một trong \"hoc\" cuối. Do đó so_luong bằng 4."
  }
]
''')
assert len(ARENA_QUESTIONS) == 10 and all(len(q["options"]) == 4 for q in ARENA_QUESTIONS)

MAX_TEAMS = 12
QUESTION_SECONDS = 20
READY_SECONDS = 4  # Cho các mạng có độ trễ khác nhau nhận tín hiệu trước khi bắt đầu.
ROOM_TTL_SECONDS = 24 * 3600


@contextmanager
def arena_db():
    conn = sqlite3.connect(ARENA_DB_PATH, timeout=15)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_arena_db():
    with arena_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS arena_rooms (
                code TEXT PRIMARY KEY,
                teacher_hash TEXT NOT NULL,
                created_at REAL NOT NULL,
                phase TEXT NOT NULL DEFAULT 'waiting',
                question_index INTEGER NOT NULL DEFAULT -1,
                starts_at REAL,
                ends_at REAL
            );
            CREATE TABLE IF NOT EXISTS arena_teams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_code TEXT NOT NULL,
                name TEXT NOT NULL,
                name_key TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                joined_at REAL NOT NULL,
                UNIQUE(room_code, name_key)
            );
            CREATE TABLE IF NOT EXISTS arena_answers (
                room_code TEXT NOT NULL,
                question_index INTEGER NOT NULL,
                team_id INTEGER NOT NULL,
                choice INTEGER NOT NULL,
                received_at REAL NOT NULL,
                elapsed_ms INTEGER NOT NULL,
                correct INTEGER NOT NULL,
                PRIMARY KEY(room_code, question_index, team_id)
            );
            CREATE INDEX IF NOT EXISTS arena_teams_room ON arena_teams(room_code);
            CREATE INDEX IF NOT EXISTS arena_answers_room ON arena_answers(room_code, question_index);
        """)


init_arena_db()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def arena_get_room(conn, code: str):
    return conn.execute(
        "SELECT * FROM arena_rooms WHERE code=? AND created_at>?",
        (code.upper(), time.time() - ROOM_TTL_SECONDS),
    ).fetchone()


def refresh_room(conn, room):
    """Chốt pha theo thời gian MÁY CHỦ, cả khi không có trình duyệt nào đang mở."""
    if room is None:
        return None
    now = time.time()
    if room["phase"] == "countdown" and now >= room["starts_at"]:
        conn.execute("UPDATE arena_rooms SET phase='answering' WHERE code=? AND phase='countdown'", (room["code"],))
    if room["phase"] in ("countdown", "answering") and now >= room["ends_at"]:
        conn.execute("UPDATE arena_rooms SET phase='review' WHERE code=? AND phase IN ('countdown','answering')", (room["code"],))
    return conn.execute("SELECT * FROM arena_rooms WHERE code=?", (room["code"],)).fetchone()


def _error(message, status=400):
    return jsonify({"ok": False, "error": message}), status


def _teacher_authorized(room):
    token = request.headers.get("X-Arena-Teacher", "")
    return bool(token and secrets.compare_digest(hash_token(token), room["teacher_hash"]))


def _team_for_token(conn, room):
    token = request.headers.get("X-Arena-Team", "")
    if not token:
        return None
    return conn.execute("SELECT * FROM arena_teams WHERE room_code=? AND token_hash=?",
                        (room["code"], hash_token(token))).fetchone()


def _require_teacher_room(conn, code):
    room = refresh_room(conn, arena_get_room(conn, code))
    if room is None:
        return None, _error("Không tìm thấy phòng hoặc phòng đã hết hạn.", 404)
    if not _teacher_authorized(room):
        return None, _error("Không có quyền điều khiển của giáo viên.", 403)
    return room, None


def _make_public_url(endpoint, **kwargs):
    """Ưu tiên PUBLIC_BASE_URL=https://...onrender.com để QR luôn dùng HTTPS."""
    relative = url_for(endpoint, **kwargs)
    base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if base:
        return base + relative
    url = url_for(endpoint, _external=True, **kwargs)
    # Flask/ProxyFix sau Render thường xác định đúng scheme HTTPS; phòng hờ header.
    if request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https":
        url = url.replace("http://", "https://", 1)
    return url


@arena_bp.after_request
def arena_no_store(response):
    if request.path.startswith("/api/dau-truong/"):
        response.headers["Cache-Control"] = "no-store, private"
    return response


@arena_bp.get("/dau-truong")
def arena_teacher_page():
    return render_template("team_arena.html", arena_role="teacher")


@arena_bp.get("/dau-truong/tham-gia")
def arena_team_page():
    return render_template("team_arena.html", arena_role="team")


@arena_bp.post("/api/dau-truong/phong")
def arena_create_room():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    token = secrets.token_urlsafe(32)
    with arena_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # Dọn phòng cũ để cơ sở dữ liệu không phình to sau nhiều buổi dạy.
        old_codes = [r["code"] for r in conn.execute("SELECT code FROM arena_rooms WHERE created_at < ?", (time.time() - ROOM_TTL_SECONDS,))]
        for old_code in old_codes:
            conn.execute("DELETE FROM arena_answers WHERE room_code=?", (old_code,))
            conn.execute("DELETE FROM arena_teams WHERE room_code=?", (old_code,))
            conn.execute("DELETE FROM arena_rooms WHERE code=?", (old_code,))
        for _ in range(50):
            code = "".join(secrets.choice(alphabet) for _ in range(6))
            if not conn.execute("SELECT 1 FROM arena_rooms WHERE code=?", (code,)).fetchone():
                break
        else:
            return _error("Chưa thể tạo mã phòng mới, vui lòng thử lại.", 503)
        conn.execute("INSERT INTO arena_rooms(code,teacher_hash,created_at) VALUES(?,?,?)",
                     (code, hash_token(token), time.time()))
    return jsonify({
        "ok": True, "code": code, "teacher_token": token,
        "join_url": _make_public_url("team_arena.arena_team_page", room=code),
        "qr_url": _make_public_url("team_arena.arena_room_qr", code=code),
        "question_seconds": QUESTION_SECONDS,
        "ready_seconds": READY_SECONDS,
        "max_teams": MAX_TEAMS,
    })


@arena_bp.get("/dau-truong/qr/<code>.png")
def arena_room_qr(code):
    code = code.strip().upper()
    with arena_db() as conn:
        if not arena_get_room(conn, code):
            return _error("Không tìm thấy phòng.", 404)
    target = _make_public_url("team_arena.arena_team_page", room=code)
    img = qrcode.make(target)
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    response = send_file(buffer, mimetype="image/png", max_age=0)
    response.headers["Cache-Control"] = "no-store"
    return response


@arena_bp.post("/api/dau-truong/<code>/tham-gia")
def arena_join(code):
    body = request.get_json(silent=True) or {}
    raw = str(body.get("name", ""))
    name = " ".join(raw.split())[:35]
    if len(name) < 2 or any(ord(c) < 32 for c in name):
        return _error("Tên đội phải có ít nhất 2 kí tự.")
    name_key = unicodedata.normalize("NFKC", name).casefold()
    with arena_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        room = refresh_room(conn, arena_get_room(conn, code))
        if not room:
            return _error("Mã phòng không tồn tại hoặc đã hết hạn.", 404)
        # Late-arriving teams may join while the lesson is underway.
        # Joining never resets the current question or its server deadline.
        count = conn.execute("SELECT COUNT(*) FROM arena_teams WHERE room_code=?", (room["code"],)).fetchone()[0]
        if count >= MAX_TEAMS:
            return _error(f"Phòng đã đủ {MAX_TEAMS} đội.", 409)
        if conn.execute("SELECT 1 FROM arena_teams WHERE room_code=? AND name_key=?", (room["code"], name_key)).fetchone():
            return _error("Tên đội đã được sử dụng. Hãy chọn tên khác.", 409)
        token = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO arena_teams(room_code,name,name_key,token_hash,joined_at) VALUES(?,?,?,?,?)",
                     (room["code"], name, name_key, hash_token(token), time.time()))
        return jsonify({"ok": True, "code": room["code"], "name": name, "team_token": token})


@arena_bp.post("/api/dau-truong/<code>/tiep-tuc")
def arena_advance(code):
    with arena_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        room, err = _require_teacher_room(conn, code)
        if err:
            return err
        if room["phase"] not in ("waiting", "review"):
            return _error("Chỉ được mở câu mới khi chờ bắt đầu hoặc đã kết thúc câu trước.", 409)
        # No minimum team count: the teacher decides when to begin.
        next_index = room["question_index"] + 1
        if next_index >= len(ARENA_QUESTIONS):
            conn.execute("UPDATE arena_rooms SET phase='finished' WHERE code=?", (room["code"],))
            return jsonify({"ok": True, "phase": "finished"})
        start_at = time.time() + READY_SECONDS
        end_at = start_at + QUESTION_SECONDS
        conn.execute("""UPDATE arena_rooms SET phase='countdown', question_index=?,
                    starts_at=?, ends_at=? WHERE code=?""",
                     (next_index, start_at, end_at, room["code"]))
        return jsonify({"ok": True, "phase": "countdown", "question_number": next_index + 1,
                        "starts_at_ms": round(start_at * 1000), "ends_at_ms": round(end_at * 1000)})


@arena_bp.post("/api/dau-truong/<code>/tra-loi")
def arena_submit_answer(code):
    body = request.get_json(silent=True) or {}
    choice = body.get("choice")
    if isinstance(choice, bool) or not isinstance(choice, int) or choice not in range(4):
        return _error("Đáp án phải là A, B, C hoặc D.")
    with arena_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        room = refresh_room(conn, arena_get_room(conn, code))
        if room is None:
            return _error("Không tìm thấy phòng.", 404)
        team = _team_for_token(conn, room)
        if team is None:
            return _error("Thiết bị chưa đăng ký hợp lệ vào phòng.", 403)
        # Lấy thời gian tại máy chủ, tuyệt đối không tin thời gian do điện thoại gửi lên.
        now = time.time()
        if room["phase"] != "answering" or now < room["starts_at"] or now >= room["ends_at"]:
            return _error("Câu hỏi chưa mở hoặc đã hết 20 giây.", 409)
        if conn.execute("""SELECT 1 FROM arena_answers WHERE room_code=?
                        AND question_index=? AND team_id=?""",
                        (room["code"], room["question_index"], team["id"])).fetchone():
            return _error("Đội đã gửi đáp án cho câu hỏi này.", 409)
        elapsed_ms = max(0, round((now - room["starts_at"]) * 1000))
        is_correct = int(choice == ARENA_QUESTIONS[room["question_index"]]["answer"])
        conn.execute("""INSERT INTO arena_answers
                        (room_code,question_index,team_id,choice,received_at,elapsed_ms,correct)
                        VALUES(?,?,?,?,?,?,?)""",
                     (room["code"], room["question_index"], team["id"], choice, now, elapsed_ms, is_correct))
        # Không cho biết đúng/sai trước khi hết giờ.
        return jsonify({"ok": True, "received": True, "elapsed_ms": elapsed_ms})


def _ranking(conn, room):
    teams = [dict(r) for r in conn.execute(
        "SELECT id, name FROM arena_teams WHERE room_code=? ORDER BY joined_at,id", (room["code"],))]
    all_answers = [dict(r) for r in conn.execute(
        "SELECT team_id,question_index,choice,elapsed_ms,correct FROM arena_answers WHERE room_code=?",
        (room["code"],))]
    by_team = {t["id"]: [] for t in teams}
    by_current = {}
    for ans in all_answers:
        by_team[ans["team_id"]].append(ans)
        if ans["question_index"] == room["question_index"]:
            by_current[ans["team_id"]] = ans

    totals = []
    for t in teams:
        correct_answers = [a for a in by_team[t["id"]] if a["correct"]]
        totals.append({
            "name": t["name"], "correct": len(correct_answers),
            "time_ms": sum(a["elapsed_ms"] for a in correct_answers),
            "answered": len(by_team[t["id"]]), "_id": t["id"],
        })
    totals.sort(key=lambda r: (-r["correct"], r["time_ms"], r["_id"]))
    for i, row in enumerate(totals, 1):
        row["rank"] = i
        row.pop("_id")

    round_rows = []
    for t in teams:
        ans = by_current.get(t["id"])
        status = "timeout" if not ans else ("correct" if ans["correct"] else "wrong")
        round_rows.append({
            "name": t["name"], "status": status,
            "elapsed_ms": ans["elapsed_ms"] if ans else None,
            "choice": ans["choice"] if ans else None, "_id": t["id"],
        })
    # Trả lời ĐÚNG xếp trên. Nếu cùng đúng, ít thời gian hơn xếp trên.
    # Sai và hết giờ đều không có điểm, vị trí dưới dùng thứ tự tham gia.
    round_rows.sort(key=lambda r: (0 if r["status"] == "correct" else 1,
                                   r["elapsed_ms"] if r["status"] == "correct" else 0,
                                   r["_id"]))
    for i, row in enumerate(round_rows, 1):
        row["rank"] = i
        row.pop("_id")
    return totals, round_rows


@arena_bp.get("/api/dau-truong/<code>/trang-thai")
def arena_state(code):
    with arena_db() as conn:
        room = refresh_room(conn, arena_get_room(conn, code))
        if room is None:
            return _error("Không tìm thấy phòng hoặc phòng đã hết hạn.", 404)
        teacher_requested = bool(request.headers.get("X-Arena-Teacher"))
        team_requested = bool(request.headers.get("X-Arena-Team"))
        teacher = teacher_requested and _teacher_authorized(room)
        team = _team_for_token(conn, room) if team_requested else None
        if (teacher_requested and not teacher) or (team_requested and team is None):
            return _error("Phiên đăng nhập không hợp lệ.", 403)
        authorized = bool(teacher or team)
        nteams = conn.execute("SELECT COUNT(*) FROM arena_teams WHERE room_code=?", (room["code"],)).fetchone()[0]
        answered_count = conn.execute("""SELECT COUNT(*) FROM arena_answers WHERE room_code=?
                           AND question_index=?""", (room["code"], room["question_index"])).fetchone()[0]
        response = {
            "ok": True, "code": room["code"], "phase": room["phase"],
            "server_now_ms": round(time.time() * 1000),
            "question_number": room["question_index"] + 1,
            "total_questions": len(ARENA_QUESTIONS),
            "starts_at_ms": round(room["starts_at"] * 1000) if room["starts_at"] else None,
            "ends_at_ms": round(room["ends_at"] * 1000) if room["ends_at"] else None,
            "question_seconds": QUESTION_SECONDS,
            "ready_seconds": READY_SECONDS,
            "team_count": nteams,
            "max_teams": MAX_TEAMS,
            "answered_count": answered_count if authorized else None,
            "teacher": bool(teacher),
            "my_team": team["name"] if team else None,
        }
        if authorized and room["phase"] == "waiting":
            response["teams"] = [r["name"] for r in conn.execute(
                "SELECT name FROM arena_teams WHERE room_code=? ORDER BY joined_at,id", (room["code"],))]

        if authorized and room["question_index"] >= 0:
            q = ARENA_QUESTIONS[room["question_index"]]
            if room["phase"] in ("answering", "review", "finished"):
                response["question"] = {
                    "number": q["number"], "difficulty": q["difficulty"],
                    "blocks": q["blocks"], "options": q["options"],
                }
            if team:
                ans = conn.execute("""SELECT choice,elapsed_ms,correct FROM arena_answers WHERE
                                     room_code=? AND team_id=? AND question_index=?""",
                                   (room["code"], team["id"], room["question_index"])).fetchone()
                response["my_answer"] = ans["choice"] if ans is not None else None
                response["my_elapsed_ms"] = ans["elapsed_ms"] if ans is not None else None
        if authorized and room["phase"] in ("review", "finished"):
            q = ARENA_QUESTIONS[room["question_index"]]
            correct_count = conn.execute("""SELECT COUNT(*) FROM arena_answers WHERE room_code=?
                                        AND question_index=? AND correct=1""",
                                         (room["code"], room["question_index"])).fetchone()[0]
            choice_counts = [0, 0, 0, 0]
            for row in conn.execute("""SELECT choice,COUNT(*) n FROM arena_answers
                                    WHERE room_code=? AND question_index=? GROUP BY choice""",
                                    (room["code"], room["question_index"])):
                choice_counts[row["choice"]] = row["n"]
            response["review"] = {
                "answer": q["answer"], "explanation": q["explanation"],
                "correct": correct_count,
                "wrong": answered_count - correct_count,
                "timeout": max(0, nteams - answered_count),
                "choice_counts": choice_counts,
            }
            response["leaderboard"], response["round_ranking"] = _ranking(conn, room)
        return jsonify(response)


app.register_blueprint(arena_bp)

@app.get("/health")
def health():
    return jsonify({"ok": True, "games": 5, "questions": len(QUESTIONS), "escape_questions": len(ESCAPE_QUESTIONS), "arena_questions": len(ARENA_QUESTIONS)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)

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

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config["JSON_AS_ASCII"] = False

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


@app.get("/health")
def health():
    return jsonify({"ok": True, "games": 2, "questions": len(QUESTIONS)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)

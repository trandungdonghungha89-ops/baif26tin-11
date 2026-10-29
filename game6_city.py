"""Game 6: Bien doi giai cuu thanh pho so.

Self-contained Flask blueprint. Existing games are not modified by this module.
Source questions: the user's Tin hoc 10 30-question Word document.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import secrets
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path

import qrcode
from flask import Blueprint, Response, jsonify, render_template, request, send_file, url_for

city_bp = Blueprint("city_game", __name__)
CITY_BASE_DIR = Path(__file__).resolve().parent
CITY_DB_PATH = os.environ.get("GAME_DB", str(CITY_BASE_DIR / "game.db"))
CITY_QUESTIONS = json.loads((CITY_BASE_DIR / "game6_questions.json").read_text(encoding="utf-8"))
STATIONS = [
    {"name": "Trung tâm dữ liệu", "lesson": "Bài 1 · Thông tin và xử lí thông tin", "icon": "🗄️", "color": "blue"},
    {"name": "Nhà máy thông minh", "lesson": "Bài 2 · Thiết bị thông minh và tin học", "icon": "🏭", "color": "green"},
    {"name": "Thư viện mã hóa", "lesson": "Bài 3 · Kiểu dữ liệu và dữ liệu văn bản", "icon": "📚", "color": "purple"},
    {"name": "Cỗ máy nhị phân", "lesson": "Bài 4 · Hệ nhị phân và dữ liệu số nguyên", "icon": "⚙️", "color": "orange"},
    {"name": "Lõi điều khiển", "lesson": "Bài 5 · Dữ liệu lôgic", "icon": "💠", "color": "gold"},
]
TIMES_DEFAULT = {"NB": 20, "TH": 25, "VD thấp": 40, "VD cao": 60}
POINTS = {"NB": 100, "TH": 150, "VD thấp": 200, "VD cao": 300}
ICON_CHOICES = ["🚀", "⚡", "🛡️", "🤖", "🌟", "💎", "🔥", "🧠", "🛰️", "🎯", "🧩", "💻"]
ROOM_SECONDS = 24 * 3600
READY_SECONDS = 5
MIN_TEAMS = 2
MAX_TEAMS = 12
ANSWER_LABELS = "ABCD"

assert len(CITY_QUESTIONS) == 30
for expected_number, question in enumerate(CITY_QUESTIONS, 1):
    assert question["number"] == expected_number
    assert question["lesson"] == (expected_number - 1) // 6 + 1
    assert question["level"] in POINTS and len(question["options"]) == 4
    assert 0 <= question["answer"] <= 3


@contextmanager
def city_db():
    conn = sqlite3.connect(CITY_DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=20000")
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_city_db():
    with city_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS city_rooms (
            code TEXT PRIMARY KEY,
            teacher_hash TEXT NOT NULL,
            created_at REAL NOT NULL,
            phase TEXT NOT NULL DEFAULT 'lobby',
            question_index INTEGER NOT NULL DEFAULT -1,
            attempt INTEGER NOT NULL DEFAULT 0,
            starts_at REAL,
            ends_at REAL,
            durations_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS city_teams (
            id TEXT PRIMARY KEY,
            room_code TEXT NOT NULL,
            name TEXT NOT NULL,
            name_key TEXT NOT NULL,
            icon TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            joined_at REAL NOT NULL,
            UNIQUE(room_code, name_key)
        );
        CREATE TABLE IF NOT EXISTS city_answers (
            room_code TEXT NOT NULL,
            question_index INTEGER NOT NULL,
            attempt INTEGER NOT NULL,
            team_id TEXT NOT NULL,
            choice INTEGER NOT NULL,
            used_boost INTEGER NOT NULL,
            correct INTEGER NOT NULL,
            points INTEGER NOT NULL,
            received_at REAL NOT NULL,
            PRIMARY KEY(room_code,question_index,attempt,team_id)
        );
        CREATE INDEX IF NOT EXISTS idx_city_teams_room ON city_teams(room_code);
        CREATE INDEX IF NOT EXISTS idx_city_answers_room ON city_answers(room_code,question_index);
        """)


init_city_db()


def city_fail(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


def city_json_body():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def city_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def get_room(conn, code: str):
    if not code or len(code) != 6:
        return None
    return conn.execute(
        "SELECT * FROM city_rooms WHERE code=? AND created_at>?",
        (code.upper(), time.time() - ROOM_SECONDS)
    ).fetchone()


def refresh_room(conn, room):
    """The server clock alone opens and ends answer windows."""
    if room is None:
        return None
    now = time.time()
    if room["phase"] == "countdown" and now >= room["starts_at"]:
        conn.execute("UPDATE city_rooms SET phase='answering' WHERE code=? AND phase='countdown'", (room["code"],))
    if room["phase"] in ("countdown", "answering") and now >= room["ends_at"]:
        conn.execute(
            "UPDATE city_rooms SET phase='review' WHERE code=? AND phase IN ('countdown','answering')",
            (room["code"],)
        )
    return conn.execute("SELECT * FROM city_rooms WHERE code=?", (room["code"],)).fetchone()


def valid_teacher(room):
    token = request.headers.get("X-City-Teacher", "")
    return bool(room and token and secrets.compare_digest(city_hash(token), room["teacher_hash"]))


def team_from_request(conn, room):
    token = request.headers.get("X-City-Team", "")
    if not token or not room:
        return None
    return conn.execute(
        "SELECT * FROM city_teams WHERE room_code=? AND token_hash=?",
        (room["code"], city_hash(token))
    ).fetchone()


def require_teacher(conn, code):
    room = refresh_room(conn, get_room(conn, code))
    if room is None:
        return None, city_fail("Mã phòng không tồn tại hoặc đã hết hạn.", 404)
    if not valid_teacher(room):
        return None, city_fail("Không có quyền điều khiển của giáo viên.", 403)
    return room, None


def public_url(endpoint, **kwargs):
    relative = url_for(endpoint, **kwargs)
    base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if base and (base.startswith("https://") or base.startswith("http://")):
        return base + relative
    url = url_for(endpoint, _external=True, **kwargs)
    if request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https":
        url = url.replace("http://", "https://", 1)
    return url


def get_teams(conn, code):
    return [dict(r) for r in conn.execute(
        "SELECT id,name,icon,joined_at FROM city_teams WHERE room_code=? ORDER BY joined_at,id", (code,)
    )]


def get_answers(conn, code):
    return [dict(r) for r in conn.execute(
        "SELECT room_code,question_index,attempt,team_id,choice,used_boost,correct,points,received_at "
        "FROM city_answers WHERE room_code=?", (code,)
    )]


def all_ranked(teams, answers):
    # The shared leaderboard uses score, advanced questions and correct count;
    # connection latency and join order are NOT tie-breakers.
    result = []
    for t in teams:
        a = [x for x in answers if x["team_id"] == t["id"]]
        correct = [x for x in a if x["correct"]]
        result.append({
            "name": t["name"], "icon": t["icon"],
            "points": sum(x["points"] for x in a),
            "correct": len(correct),
            "high_correct": sum(CITY_QUESTIONS[x["question_index"]]["level"] == "VD cao" for x in correct),
            "bonus": sum(x["points"] - POINTS[CITY_QUESTIONS[x["question_index"]]["level"]] for x in correct),
            "used_boost": any(x["used_boost"] for x in a)
        })
    result.sort(key=lambda x: (-x["points"], -x["high_correct"], -x["correct"], x["name"].casefold()))
    last_key = None
    last_rank = 0
    for index, item in enumerate(result, 1):
        key = (item["points"], item["high_correct"], item["correct"])
        if key != last_key:
            last_rank = index
            last_key = key
        item["rank"] = last_rank
    return result


def response_state(conn, room, teacher, team):
    code = room["code"]
    teams = get_teams(conn, code)
    answers = get_answers(conn, code)
    now = time.time()
    qidx = room["question_index"]
    phase = room["phase"]
    current_complete = phase not in ("countdown", "answering")
    # Only finalized answers count toward points/energy.
    completed = [a for a in answers if a["question_index"] < qidx or (
        a["question_index"] == qidx and current_complete and qidx >= 0
    )]
    current = [a for a in answers if qidx >= 0 and a["question_index"] == qidx and a["attempt"] == room["attempt"]]
    team_count = len(teams)
    energy = sum(a["correct"] for a in completed)
    target = math.ceil(team_count * len(CITY_QUESTIONS) * 0.6)
    completed_question_count = max(0, qidx + (1 if current_complete and qidx >= 0 else 0))
    stations = []
    for i, station in enumerate(STATIONS):
        start = i * 6
        done = max(0, min(6, completed_question_count - start))
        correct = sum(a["correct"] for a in completed if start <= a["question_index"] < start + 6)
        capacity = done * team_count
        ratio = correct / capacity if capacity else 0
        state = "locked" if done == 0 else ("active" if done < 6 else "done")
        if state == "done":
            state = "stable" if ratio >= 0.60 else ("recovering" if ratio >= 0.40 else "weak")
        stations.append({**station, "done": done, "correct": correct, "capacity": capacity,
                         "ratio": round(ratio * 100), "state": state})
    result = {
        "ok": True, "code": code, "phase": phase, "question_index": qidx,
        "question_number": qidx + 1 if qidx >= 0 else 0,
        "total_questions": len(CITY_QUESTIONS),
        "attempt": room["attempt"],
        "server_now_ms": round(now * 1000),
        "starts_at_ms": round(room["starts_at"] * 1000) if room["starts_at"] else None,
        "ends_at_ms": round(room["ends_at"] * 1000) if room["ends_at"] else None,
        "ready_seconds": READY_SECONDS,
        "question_seconds": json.loads(room["durations_json"]).get(CITY_QUESTIONS[qidx]["level"]) if qidx >= 0 else None,
        "team_count": team_count, "max_teams": MAX_TEAMS,
        "energy": energy, "target_energy": target, "max_energy": team_count * len(CITY_QUESTIONS),
        "reached_goal": bool(target and energy >= target),
        "finished_questions": completed_question_count,
        "stations": stations,
        "answered_count": len(current) if phase in ("countdown", "answering", "review") else None,
        "is_secret": qidx >= 27 and phase not in ("awards", "finished"),
        "teacher": bool(teacher),
    }
    if phase == "lobby":
        result["teams"] = [{"name": t["name"], "icon": t["icon"]} for t in teams]
    if phase in ("answering", "review") and qidx >= 0:
        q = CITY_QUESTIONS[qidx]
        result["question"] = {"number": q["number"], "lesson": q["lesson"], "level": q["level"],
                              "prompt": q["prompt"], "options": q["options"], "points": POINTS[q["level"]]}
    if phase == "review" and qidx >= 0:
        q = CITY_QUESTIONS[qidx]
        counts = [0, 0, 0, 0]
        for record in current:
            counts[record["choice"]] += 1
        result["review"] = {
            "answer": q["answer"], "answer_text": q["options"][q["answer"]],
            "explanation": q["explanation"], "choice_counts": counts,
            "correct": sum(a["correct"] for a in current),
            "wrong": len(current) - sum(a["correct"] for a in current),
            "timeout": max(0, team_count - len(current)),
        }
    show_scores = phase in ("station", "awards", "finished") or (phase == "review" and qidx == 26)
    if show_scores:
        result["leaderboard"] = all_ranked(teams, completed)
    if team:
        own_finished = [a for a in completed if a["team_id"] == team["id"]]
        own_all = [a for a in answers if a["team_id"] == team["id"]]
        own_current = next((a for a in current if a["team_id"] == team["id"]), None)
        result["my_team"] = {
            "name": team["name"], "icon": team["icon"],
            "points": sum(a["points"] for a in own_finished),
            "correct": sum(a["correct"] for a in own_finished),
            "boost_available": not any(a["used_boost"] for a in own_all),
            "my_answer": own_current["choice"] if own_current else None,
            "my_boost": bool(own_current["used_boost"]) if own_current else False,
            "my_earned": own_current["points"] if own_current and phase == "review" else None,
            "my_correct": bool(own_current["correct"]) if own_current and phase == "review" else None,
        }
        # No live opponent points/ranks in a team state; only published checkpoint rankings.
    return result


@city_bp.after_request
def city_headers(response):
    if request.path.startswith(("/api/thanh-pho/", "/thanh-pho-so/qr/")):
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@city_bp.get("/thanh-pho-so")
def city_teacher_page():
    return render_template("city_teacher.html")


@city_bp.get("/thanh-pho-so/tham-gia")
def city_student_page():
    return render_template("city_student.html")


@city_bp.post("/api/thanh-pho/phong")
def city_create_room():
    body = city_json_body()
    raw_durations = body.get("durations", {})
    if not isinstance(raw_durations, dict):
        return city_fail("Cấu hình thời gian không hợp lệ.")
    durations = dict(TIMES_DEFAULT)
    for level in TIMES_DEFAULT:
        if level in raw_durations:
            v = raw_durations[level]
            if isinstance(v, bool) or not isinstance(v, int) or not (10 <= v <= 180):
                return city_fail(f"Thời gian mức {level} phải trong khoảng 10–180 giây.")
            durations[level] = v
    token = secrets.token_urlsafe(32)
    code = None
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    with city_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        expired = [x["code"] for x in conn.execute(
            "SELECT code FROM city_rooms WHERE created_at<?", (time.time() - ROOM_SECONDS,)
        )]
        for old in expired:
            conn.execute("DELETE FROM city_answers WHERE room_code=?", (old,))
            conn.execute("DELETE FROM city_teams WHERE room_code=?", (old,))
            conn.execute("DELETE FROM city_rooms WHERE code=?", (old,))
        for _ in range(60):
            candidate = "".join(secrets.choice(alphabet) for _ in range(6))
            if not conn.execute("SELECT 1 FROM city_rooms WHERE code=?", (candidate,)).fetchone():
                code = candidate
                break
        if not code:
            return city_fail("Không tạo được mã phòng, vui lòng thử lại.", 503)
        conn.execute("INSERT INTO city_rooms(code,teacher_hash,created_at,durations_json) VALUES(?,?,?,?)",
                     (code, city_hash(token), time.time(), json.dumps(durations, ensure_ascii=False)))
    return jsonify({"ok": True, "code": code, "teacher_token": token,
                    "join_url": public_url("city_game.city_student_page", room=code),
                    "qr_url": public_url("city_game.city_qr", code=code),
                    "durations": durations})


@city_bp.get("/thanh-pho-so/qr/<code>.png")
def city_qr(code):
    with city_db() as conn:
        if not get_room(conn, code):
            return city_fail("Không tìm thấy phòng.", 404)
    target = public_url("city_game.city_student_page", room=code.upper())
    img = qrcode.make(target)
    stream = io.BytesIO()
    img.save(stream, format="PNG")
    stream.seek(0)
    return send_file(stream, mimetype="image/png", max_age=0)


@city_bp.post("/api/thanh-pho/<code>/tham-gia")
def city_join(code):
    body = city_json_body()
    raw_name = body.get("name", "")
    name = " ".join(str(raw_name).split())
    if not (2 <= len(name) <= 36) or any(unicodedata.category(c).startswith("C") for c in name):
        return city_fail("Tên đội cần từ 2 đến 36 kí tự hợp lệ.")
    name_key = unicodedata.normalize("NFKC", name).casefold()
    icon = body.get("icon", "🚀")
    if icon not in ICON_CHOICES:
        return city_fail("Biểu tượng đội không hợp lệ.")
    with city_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        room = refresh_room(conn, get_room(conn, code))
        if room is None:
            return city_fail("Mã phòng không tồn tại hoặc đã hết hạn.", 404)
        if room["phase"] != "lobby":
            return city_fail("Trận đấu đã bắt đầu. Không thể thêm đội mới.", 409)
        count = conn.execute("SELECT COUNT(*) FROM city_teams WHERE room_code=?", (room["code"],)).fetchone()[0]
        if count >= MAX_TEAMS:
            return city_fail("Phòng đã đủ 12 đội.", 409)
        if conn.execute("SELECT 1 FROM city_teams WHERE room_code=? AND name_key=?", (room["code"], name_key)).fetchone():
            return city_fail("Tên đội đã có người sử dụng.", 409)
        team_id = secrets.token_urlsafe(18)
        token = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO city_teams(id,room_code,name,name_key,icon,token_hash,joined_at) VALUES(?,?,?,?,?,?,?)",
                     (team_id, room["code"], name, name_key, icon, city_hash(token), time.time()))
    return jsonify({"ok": True, "code": code.upper(), "team_token": token, "name": name, "icon": icon})


@city_bp.get("/api/thanh-pho/<code>/trang-thai")
def city_state(code):
    with city_db() as conn:
        room = refresh_room(conn, get_room(conn, code))
        if room is None:
            return city_fail("Phòng không tồn tại hoặc đã hết hạn.", 404)
        teacher = valid_teacher(room)
        team = team_from_request(conn, room)
        if not (teacher or team):
            return city_fail("Phiên đăng nhập không hợp lệ.", 403)
        return jsonify(response_state(conn, room, teacher, team))


def next_question(conn, room):
    idx = room["question_index"] + 1
    if idx >= 30:
        raise ValueError("Không còn câu hỏi mới.")
    q = CITY_QUESTIONS[idx]
    duration = json.loads(room["durations_json"])[q["level"]]
    starts_at = time.time() + READY_SECONDS
    ends_at = starts_at + duration
    conn.execute("UPDATE city_rooms SET phase='countdown',question_index=?,attempt=0,starts_at=?,ends_at=? WHERE code=?",
                 (idx, starts_at, ends_at, room["code"]))
    return idx


@city_bp.post("/api/thanh-pho/<code>/dieu-khien")
def city_control(code):
    body = city_json_body()
    action = body.get("action")
    with city_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        room, error = require_teacher(conn, code)
        if error:
            return error
        phase = room["phase"]
        idx = room["question_index"]
        if action == "begin":
            if phase != "lobby":
                return city_fail("Trò chơi đã được bắt đầu.", 409)
            count = conn.execute("SELECT COUNT(*) FROM city_teams WHERE room_code=?", (room["code"],)).fetchone()[0]
            if count < MIN_TEAMS:
                return city_fail("Cần tối thiểu 2 đội để bắt đầu.", 409)
            conn.execute("UPDATE city_rooms SET phase='intro' WHERE code=?", (room["code"],))
        elif action == "advance":
            if phase in ("intro", "station", "finale_intro"):
                next_question(conn, room)
            elif phase == "review":
                if idx in (5, 11, 17, 23):
                    conn.execute("UPDATE city_rooms SET phase='station' WHERE code=?", (room["code"],))
                elif idx == 26:
                    conn.execute("UPDATE city_rooms SET phase='finale_intro' WHERE code=?", (room["code"],))
                elif idx == 29:
                    conn.execute("UPDATE city_rooms SET phase='sealed' WHERE code=?", (room["code"],))
                else:
                    next_question(conn, room)
            else:
                return city_fail("Chưa thể chuyển bước ở trạng thái này.", 409)
        elif action == "replay":
            if phase not in ("countdown", "answering", "review") or idx < 0:
                return city_fail("Chỉ được làm lại câu hiện tại khi câu đang diễn ra hoặc ở màn hình đáp án.", 409)
            # Roll back *all* submissions for the current question, including boosts;
            # scores and energy are derived from accepted answers, so no duplicate refunds.
            conn.execute("DELETE FROM city_answers WHERE room_code=? AND question_index=?", (room["code"], idx))
            start = time.time() + READY_SECONDS
            duration = json.loads(room["durations_json"])[CITY_QUESTIONS[idx]["level"]]
            conn.execute("UPDATE city_rooms SET phase='countdown',attempt=attempt+1,starts_at=?,ends_at=? WHERE code=?",
                         (start, start + duration, room["code"]))
        elif action == "reveal":
            if phase != "sealed":
                return city_fail("Chỉ được công bố chung cuộc sau khi kết thúc 30 câu.", 409)
            conn.execute("UPDATE city_rooms SET phase='awards' WHERE code=?", (room["code"],))
        elif action == "finish":
            if phase != "awards":
                return city_fail("Chỉ kết thúc sau khi công bố kết quả.", 409)
            conn.execute("UPDATE city_rooms SET phase='finished' WHERE code=?", (room["code"],))
        else:
            return city_fail("Thao tác điều khiển không hợp lệ.")
    return jsonify({"ok": True})


@city_bp.post("/api/thanh-pho/<code>/tra-loi")
def city_answer(code):
    body = city_json_body()
    choice = body.get("choice")
    idx = body.get("question_index")
    attempt = body.get("attempt")
    boost = body.get("boost", False)
    if isinstance(choice, bool) or not isinstance(choice, int) or choice not in range(4):
        return city_fail("Đáp án phải là A, B, C hoặc D.")
    if isinstance(idx, bool) or not isinstance(idx, int) or isinstance(attempt, bool) or not isinstance(attempt, int):
        return city_fail("Định danh lượt câu hỏi không hợp lệ.")
    if type(boost) is not bool:
        return city_fail("Thông tin thẻ nhân đôi không hợp lệ.")
    with city_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        room = refresh_room(conn, get_room(conn, code))
        if room is None:
            return city_fail("Không tìm thấy phòng.", 404)
        team = team_from_request(conn, room)
        if team is None:
            return city_fail("Thiết bị chưa đăng ký hợp lệ vào phòng.", 403)
        if idx != room["question_index"] or attempt != room["attempt"]:
            return city_fail("Lượt câu hỏi đã thay đổi, vui lòng tải trạng thái mới.", 409)
        prior = conn.execute(
            "SELECT 1 FROM city_answers WHERE room_code=? AND question_index=? AND attempt=? AND team_id=?",
            (room["code"], idx, attempt, team["id"])
        ).fetchone()
        if prior:
            return jsonify({"ok": True, "already_received": True})
        current = time.time()
        if room["phase"] != "answering" or current < room["starts_at"] or current >= room["ends_at"]:
            return city_fail("Câu hỏi chưa mở hoặc đã hết thời gian.", 409)
        if boost and conn.execute("SELECT 1 FROM city_answers WHERE room_code=? AND team_id=? AND used_boost=1 LIMIT 1",
                                  (room["code"], team["id"])).fetchone():
            return city_fail("Thẻ SIÊU NĂNG LƯỢNG đã được sử dụng.", 409)
        q = CITY_QUESTIONS[idx]
        correct = int(choice == q["answer"])
        points = correct * POINTS[q["level"]] * (2 if boost else 1)
        conn.execute("INSERT INTO city_answers(room_code,question_index,attempt,team_id,choice,used_boost,correct,points,received_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?)",
                     (room["code"], idx, attempt, team["id"], choice, int(boost), correct, points, current))
    # Deliberately do not disclose correct/points before time expires.
    return jsonify({"ok": True, "received": True})


def csv_cell(text):
    value = str(text)
    # Prevent spreadsheet formula injection from team-controlled names.
    return "'" + value if value and value.lstrip().startswith(("=", "+", "-", "@")) else value


@city_bp.get("/api/thanh-pho/<code>/bao-cao.csv")
def city_export(code):
    with city_db() as conn:
        room, err = require_teacher(conn, code)
        if err:
            return err
        if room["phase"] not in ("awards", "finished"):
            return city_fail("Báo cáo chỉ được xuất sau khi công bố kết quả chung cuộc.", 409)
        teams = get_teams(conn, room["code"])
        answers = get_answers(conn, room["code"])
        ranks = all_ranked(teams, answers)
        by_name = {r["name"]: r for r in ranks}
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(["Hạng", "Tên đội", "Biểu tượng", "Tổng điểm", "Số câu đúng", "Đúng VD cao",
                         "Điểm thưởng x2", "Đã dùng thẻ x2", *[f"Câu {n}" for n in range(1, 31)]])
        for t in teams:
            r = by_name[t["name"]]
            per_q = {a["question_index"]: a for a in answers if a["team_id"] == t["id"]}
            writer.writerow([r["rank"], csv_cell(t["name"]), t["icon"], r["points"], r["correct"],
                             r["high_correct"], r["bonus"], "Có" if r["used_boost"] else "Không",
                             *[(ANSWER_LABELS[per_q[n]["choice"]] + (" ✓" if per_q[n]["correct"] else " ✗") +
                                (" ×2" if per_q[n]["used_boost"] else "")) if n in per_q else "Không trả lời"
                               for n in range(30)]])
        data = "\ufeff" + buffer.getvalue()
    response = Response(data, mimetype="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = f'attachment; filename="game6_{code.upper()}_ket_qua.csv"'
    response.headers["Cache-Control"] = "no-store, private"
    return response

from __future__ import annotations

import io
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any

import qrcode
from flask import Blueprint, Response, jsonify, render_template, request, send_file, url_for

BASE_DIR = Path(__file__).resolve().parent
with open(BASE_DIR / "code_race_questions.json", "r", encoding="utf-8") as f:
    CODE_RACE_QUESTIONS = json.load(f)

code_race_bp = Blueprint("code_race", __name__)
RACE_LOCK = threading.RLock()
RACE_ROOMS: dict[str, dict[str, Any]] = {}
MAX_STUDENTS = 50
ROOM_TTL = 6 * 60 * 60

PIT_STOPS = {
    2: {
        "title": "PIT STOP 1 · NỀN TẢNG",
        "items": ["Phép toán đơn giản: 1 đơn vị thời gian", "Phép toán tích cực: thực hiện nhiều nhất, chi phối thời gian", "Big-O: đánh giá và phân loại độ phức tạp thời gian"],
        "badge": "🏅 Hiểu nền tảng Big-O",
    },
    5: {
        "title": "PIT STOP 2 · PHÂN TÍCH CHƯƠNG TRÌNH",
        "items": ["Một vòng lặp n lần với công việc cố định → O(n)", "if ... else: lấy thời gian lớn nhất trong các nhánh", "T(n) = 7n + 20 → O(n)"],
        "badge": "🏅 Làm chủ cấu trúc chương trình",
    },
    7: {
        "title": "PIT STOP 3 · TĂNG TỐC",
        "items": ["Hai vòng lặp n × n → O(n²)", "Khi n lớn, số hạng tăng nhanh nhất sẽ chi phối độ phức tạp"],
        "badge": "🏅 Phân tích số hạng chi phối",
    },
}

FINAL_KNOWLEDGE = [
    "Phép toán đơn giản và đơn vị thời gian",
    "Phép toán tích cực",
    "Ý nghĩa của Big-O",
    "Một vòng lặp → O(n)",
    "Rẽ nhánh if ... else lấy nhánh lớn nhất",
    "Bỏ hệ số và hằng số khi xét Big-O",
    "Hai vòng lặp lồng nhau → O(n²)",
    "Số hạng tăng nhanh nhất chi phối",
    "n × (n/2) vẫn là O(n²)",
    "Hai phần nối tiếp: giữ bậc tăng lớn hơn",
]


def _now() -> float:
    return time.time()


def _cleanup() -> None:
    cutoff = _now() - ROOM_TTL
    stale = [code for code, room in RACE_ROOMS.items() if room.get("created_at", 0) < cutoff]
    for code in stale:
        RACE_ROOMS.pop(code, None)


def _new_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        if code not in RACE_ROOMS:
            return code


def _room(code: str | None):
    return RACE_ROOMS.get((code or "").strip().upper())


def _teacher_ok(room, token: str | None) -> bool:
    return bool(room and token and secrets.compare_digest(str(token), room.get("teacher_token", "")))


def _ranked(room):
    people = list(room["students"].items())
    people.sort(key=lambda kv: (-kv[1]["distance"], -kv[1]["correct"], kv[1]["correct_time_ms"], kv[1]["joined_at"]))
    result = []
    for pos, (sid, stu) in enumerate(people, 1):
        result.append({
            "sid": sid,
            "name": stu["name"],
            "distance": stu["distance"],
            "correct": stu["correct"],
            "streak": stu["streak"],
            "max_streak": stu["max_streak"],
            "position": pos,
            "last_gain": stu.get("last_gain", 0),
            "turbo": bool(stu.get("last_turbo", False)),
        })
    return result


def _close_question(room) -> None:
    if room["phase"] != "question":
        return
    qidx = room["qidx"]
    q = CODE_RACE_QUESTIONS[qidx]
    eligible = list(room["question_students"])
    for sid in eligible:
        stu = room["students"].get(sid)
        if not stu:
            continue
        if sid not in room["answers"]:
            stu["streak"] = 0
            stu["last_gain"] = 0
            stu["last_turbo"] = False
    correct = sum(1 for a in room["answers"].values() if a["choice"] == q["answer"])
    answered = len(room["answers"])
    total = len(eligible)
    wrong = max(0, answered - correct)
    no_answer = max(0, total - answered)
    rate = round(correct * 100 / total, 1) if total else 0
    room["review_stats"] = {
        "eligible": total,
        "answered": answered,
        "correct": correct,
        "wrong": wrong,
        "no_answer": no_answer,
        "rate": rate,
    }
    if qidx in PIT_STOPS:
        badge = PIT_STOPS[qidx]["badge"]
        if rate >= 75:
            room["class_badges"].append(badge)
    room["phase"] = "review"
    room["ended_at"] = _now()


def _refresh(room) -> None:
    if room and room["phase"] == "question" and room.get("ends_at") and _now() >= room["ends_at"]:
        _close_question(room)


def _public_state(room, sid: str | None = None, teacher: bool = False):
    _refresh(room)
    ranking = _ranked(room)
    rank_map = {r["sid"]: r for r in ranking}
    qidx = room["qidx"]
    q = CODE_RACE_QUESTIONS[qidx] if 0 <= qidx < len(CODE_RACE_QUESTIONS) else None
    data: dict[str, Any] = {
        "ok": True,
        "code": room["code"],
        "phase": room["phase"],
        "qidx": qidx,
        "total_questions": len(CODE_RACE_QUESTIONS),
        "joined": len(room["students"]),
        "max_students": MAX_STUDENTS,
        "class_badges": room["class_badges"],
        "top10": ranking[:10],
    }
    if q:
        data["question"] = {
            "difficulty": q["difficulty"],
            "zone": q["zone"],
            "seconds": q["seconds"],
            "prompt": q["prompt"],
            "choices": q["choices"],
        }
    if room["phase"] == "question":
        data["remain"] = max(0, int(room["ends_at"] - _now() + 0.999))
        data["eligible"] = len(room["question_students"])
        data["answered"] = len(room["answers"])
    else:
        data["remain"] = 0
    if room["phase"] == "review" and q:
        data["answer"] = q["answer"]
        data["answer_text"] = q["choices"][q["answer"]]
        data["explain"] = q["explain"]
        data["review_stats"] = room.get("review_stats", {})
        data["pit_stop"] = PIT_STOPS.get(qidx)
    if room["phase"] == "finished":
        all_correct = [s["correct"] for s in room["students"].values()]
        total_attempts = len(room["students"]) * len(CODE_RACE_QUESTIONS)
        total_correct = sum(all_correct)
        data["finish_stats"] = {
            "students": len(room["students"]),
            "class_accuracy": round(total_correct * 100 / total_attempts, 1) if total_attempts else 0,
            "best_streak": max([s["max_streak"] for s in room["students"].values()] or [0]),
            "knowledge": FINAL_KNOWLEDGE,
        }
    if sid:
        stu = room["students"].get(sid)
        if stu:
            mine = rank_map.get(sid, {})
            data["student"] = {
                "name": stu["name"],
                "distance": stu["distance"],
                "correct": stu["correct"],
                "streak": stu["streak"],
                "max_streak": stu["max_streak"],
                "position": mine.get("position"),
                "last_gain": stu.get("last_gain", 0),
                "last_turbo": bool(stu.get("last_turbo", False)),
            }
            ans = room["answers"].get(sid)
            if ans:
                data["my_answer"] = ans["choice"]
                data["my_correct"] = bool(ans["choice"] == q["answer"]) if q else False
    if teacher:
        data["students"] = [
            {"name": r["name"], "distance": r["distance"], "position": r["position"]}
            for r in ranking
        ]
    return data


@code_race_bp.get("/code-race")
def teacher_page():
    return render_template("code_race_teacher.html")


@code_race_bp.get("/code-race/join")
def student_page():
    return render_template("code_race_student.html")


@code_race_bp.post("/api/code-race/create")
def create_room():
    with RACE_LOCK:
        _cleanup()
        code = _new_code()
        token = secrets.token_urlsafe(24)
        RACE_ROOMS[code] = {
            "code": code,
            "teacher_token": token,
            "created_at": _now(),
            "phase": "lobby",
            "qidx": 0,
            "students": {},
            "answers": {},
            "question_students": [],
            "started_at": None,
            "ends_at": None,
            "review_stats": {},
            "class_badges": [],
        }
    return jsonify({
        "ok": True,
        "code": code,
        "teacher_token": token,
        "join_url": url_for("code_race.student_page", room=code, _external=True),
        "qr_url": url_for("code_race.qr_png", code=code, _external=True),
        "max_students": MAX_STUDENTS,
    })


@code_race_bp.post("/api/code-race/join")
def join_room():
    body = request.get_json(silent=True) or {}
    code = str(body.get("room") or "").strip().upper()
    name = " ".join(str(body.get("name") or "").split())[:50]
    with RACE_LOCK:
        room = _room(code)
        if not room:
            return jsonify({"ok": False, "error": "Không tìm thấy phòng"}), 404
        if room["phase"] != "lobby":
            return jsonify({"ok": False, "error": "Cuộc đua đã bắt đầu. Bạn không thể vào muộn."}), 409
        if len(room["students"]) >= MAX_STUDENTS:
            return jsonify({"ok": False, "error": "Phòng đã đủ 50 học sinh."}), 409
        if len(name) < 2:
            return jsonify({"ok": False, "error": "Hãy nhập họ tên."}), 400
        if any(s["name"].casefold() == name.casefold() for s in room["students"].values()):
            return jsonify({"ok": False, "error": "Tên này đã có trong phòng. Hãy thêm số thứ tự hoặc lớp."}), 409
        sid = secrets.token_urlsafe(12)
        room["students"][sid] = {
            "name": name,
            "distance": 0,
            "correct": 0,
            "streak": 0,
            "max_streak": 0,
            "correct_time_ms": 0,
            "joined_at": _now(),
            "last_gain": 0,
            "last_turbo": False,
        }
    return jsonify({"ok": True, "sid": sid})


@code_race_bp.get("/api/code-race/state")
def state():
    code = request.args.get("room")
    sid = request.args.get("sid")
    teacher = request.args.get("teacher") == "1"
    token = request.args.get("teacher_token")
    with RACE_LOCK:
        room = _room(code)
        if not room:
            return jsonify({"ok": False, "error": "Không tìm thấy phòng"}), 404
        if teacher and not _teacher_ok(room, token):
            return jsonify({"ok": False, "error": "Không có quyền giáo viên"}), 403
        return jsonify(_public_state(room, sid=sid, teacher=teacher))


@code_race_bp.post("/api/code-race/start")
def start_next():
    body = request.get_json(silent=True) or {}
    with RACE_LOCK:
        room = _room(body.get("room"))
        if not _teacher_ok(room, body.get("teacher_token")):
            return jsonify({"ok": False, "error": "Không có quyền giáo viên"}), 403
        _refresh(room)
        if room["phase"] not in ("lobby", "review"):
            return jsonify({"ok": False, "error": "Chưa thể chuyển câu lúc này"}), 409
        if room["phase"] == "lobby" and not room["students"]:
            return jsonify({"ok": False, "error": "Chưa có học sinh trong phòng"}), 409
        if room["phase"] == "review":
            room["qidx"] += 1
        if room["qidx"] >= len(CODE_RACE_QUESTIONS):
            room["phase"] = "finished"
            return jsonify({"ok": True, "finished": True})
        q = CODE_RACE_QUESTIONS[room["qidx"]]
        room["answers"] = {}
        room["review_stats"] = {}
        room["question_students"] = list(room["students"].keys())
        for stu in room["students"].values():
            stu["last_gain"] = 0
            stu["last_turbo"] = False
        room["phase"] = "question"
        room["started_at"] = _now()
        room["ends_at"] = room["started_at"] + int(q["seconds"])
    return jsonify({"ok": True, "qidx": room["qidx"]})


@code_race_bp.post("/api/code-race/answer")
def answer():
    body = request.get_json(silent=True) or {}
    with RACE_LOCK:
        room = _room(body.get("room"))
        if not room:
            return jsonify({"ok": False, "error": "Không tìm thấy phòng"}), 404
        _refresh(room)
        if room["phase"] != "question":
            return jsonify({"ok": False, "error": "Câu hỏi đã kết thúc"}), 409
        sid = str(body.get("sid") or "")
        if sid not in room["students"] or sid not in room["question_students"]:
            return jsonify({"ok": False, "error": "Không xác định được người chơi"}), 403
        if sid in room["answers"]:
            return jsonify({"ok": False, "error": "Bạn đã trả lời câu này"}), 409
        try:
            choice = int(body.get("answer"))
        except Exception:
            return jsonify({"ok": False, "error": "Đáp án không hợp lệ"}), 400
        if choice not in (0, 1, 2, 3):
            return jsonify({"ok": False, "error": "Đáp án không hợp lệ"}), 400
        q = CODE_RACE_QUESTIONS[room["qidx"]]
        elapsed_ms = max(0, int((_now() - room["started_at"]) * 1000))
        correct = choice == q["answer"]
        stu = room["students"][sid]
        gain = 0
        turbo = False
        if correct:
            stu["correct"] += 1
            stu["streak"] += 1
            stu["max_streak"] = max(stu["max_streak"], stu["streak"])
            stu["correct_time_ms"] += elapsed_ms
            gain = 100
            if stu["streak"] % 3 == 0:
                gain += 50
                turbo = True
            stu["distance"] += gain
        else:
            stu["streak"] = 0
        stu["last_gain"] = gain
        stu["last_turbo"] = turbo
        room["answers"][sid] = {"choice": choice, "elapsed_ms": elapsed_ms}
    return jsonify({"ok": True, "correct": correct, "gain": gain, "turbo": turbo})


@code_race_bp.post("/api/code-race/finish-question")
def finish_question():
    body = request.get_json(silent=True) or {}
    with RACE_LOCK:
        room = _room(body.get("room"))
        if not _teacher_ok(room, body.get("teacher_token")):
            return jsonify({"ok": False, "error": "Không có quyền giáo viên"}), 403
        if room["phase"] == "question":
            _close_question(room)
    return jsonify({"ok": True})


@code_race_bp.get("/code-race/qr/<code>.png")
def qr_png(code: str):
    with RACE_LOCK:
        room = _room(code)
        if not room:
            return Response("Không tìm thấy phòng", status=404)
        target = url_for("code_race.student_page", room=room["code"], _external=True)
    img = qrcode.make(target)
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return send_file(bio, mimetype="image/png", max_age=30)

from flask import Flask, jsonify, render_template, request, send_file
import io, os, secrets, sqlite3, string, time
import qrcode

app = Flask(__name__)
DB_PATH = os.environ.get('GAME_DB', os.path.join(os.path.dirname(__file__), 'game.db'))
ROUND_SECONDS = 120


def now_ms():
    return int(time.time() * 1000)


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def init_db():
    with db() as c:
        c.executescript('''
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
        ''')


init_db()


def random_code():
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    for _ in range(30):
        code = ''.join(secrets.choice(alphabet) for _ in range(6))
        with db() as c:
            if not c.execute('SELECT 1 FROM rooms WHERE code=?', (code,)).fetchone():
                return code
    raise RuntimeError('Could not create room code')


def get_room(code):
    code = (code or '').strip().upper()
    with db() as c:
        row = c.execute('SELECT * FROM rooms WHERE code=?', (code,)).fetchone()
    return dict(row) if row else None


def normalized_room(room):
    if not room:
        return None
    room = dict(room)
    if room['status'] == 'playing' and room.get('ends_at') and now_ms() >= room['ends_at']:
        with db() as c:
            c.execute("UPDATE rooms SET status='finished' WHERE code=? AND status='playing'", (room['code'],))
        room['status'] = 'finished'
    return room


def require_teacher(room, key):
    return room and secrets.compare_digest(room['teacher_key'], key or '')


@app.get('/')
def index():
    return render_template('index.html')


@app.post('/api/rooms')
def create_room():
    code = random_code()
    key = secrets.token_urlsafe(24)
    with db() as c:
        c.execute('INSERT INTO rooms(code,teacher_key,created_at,status) VALUES(?,?,?,?)',
                  (code, key, now_ms(), 'lobby'))
    return jsonify({'ok': True, 'code': code, 'teacher_key': key, 'round_seconds': ROUND_SECONDS})


@app.get('/api/rooms/<code>/status')
def room_status(code):
    room = normalized_room(get_room(code))
    if not room:
        return jsonify({'ok': False, 'error': 'Không tìm thấy phòng'}), 404
    with db() as c:
        participants = c.execute('SELECT COUNT(*) n FROM participants WHERE room_code=?', (room['code'],)).fetchone()['n']
        completed = c.execute('SELECT COUNT(*) n FROM results WHERE room_code=? AND completed=1', (room['code'],)).fetchone()['n']
    remaining = 0
    if room['status'] == 'playing' and room.get('ends_at'):
        remaining = max(0, room['ends_at'] - now_ms())
    return jsonify({'ok': True, 'code': room['code'], 'status': room['status'],
                    'started_at': room.get('started_at'), 'ends_at': room.get('ends_at'),
                    'remaining_ms': remaining, 'participants': participants, 'completed': completed,
                    'round_seconds': ROUND_SECONDS})


@app.post('/api/rooms/<code>/join')
def join_room(code):
    room = normalized_room(get_room(code))
    if not room:
        return jsonify({'ok': False, 'error': 'Mã phòng không đúng'}), 404
    data = request.get_json(silent=True) or {}
    name = ' '.join(str(data.get('name', '')).split())[:50]
    if len(name) < 2:
        return jsonify({'ok': False, 'error': 'Hãy nhập họ tên'}), 400
    pid = secrets.token_urlsafe(18)
    ts = now_ms()
    with db() as c:
        old = c.execute('SELECT id FROM participants WHERE room_code=? AND name=?', (room['code'], name)).fetchone()
        if old:
            pid = old['id']
            c.execute('UPDATE participants SET last_seen=? WHERE id=?', (ts, pid))
        else:
            c.execute('INSERT INTO participants(id,room_code,name,joined_at,last_seen) VALUES(?,?,?,?,?)',
                      (pid, room['code'], name, ts, ts))
    return jsonify({'ok': True, 'participant_id': pid, 'name': name, 'status': room['status']})


@app.post('/api/rooms/<code>/start')
def start_room(code):
    room = get_room(code)
    data = request.get_json(silent=True) or {}
    if not require_teacher(room, data.get('teacher_key')):
        return jsonify({'ok': False, 'error': 'Không có quyền giáo viên'}), 403
    start = now_ms()
    end = start + ROUND_SECONDS * 1000
    with db() as c:
        c.execute("UPDATE rooms SET status='playing', started_at=?, ends_at=? WHERE code=?", (start, end, room['code']))
        c.execute('DELETE FROM results WHERE room_code=?', (room['code'],))
    return jsonify({'ok': True, 'started_at': start, 'ends_at': end, 'round_seconds': ROUND_SECONDS})


@app.post('/api/rooms/<code>/finish')
def finish_room(code):
    room = get_room(code)
    data = request.get_json(silent=True) or {}
    if not require_teacher(room, data.get('teacher_key')):
        return jsonify({'ok': False, 'error': 'Không có quyền giáo viên'}), 403
    with db() as c:
        c.execute("UPDATE rooms SET status='finished' WHERE code=?", (room['code'],))
    return jsonify({'ok': True})


@app.post('/api/rooms/<code>/reset')
def reset_room(code):
    room = get_room(code)
    data = request.get_json(silent=True) or {}
    if not require_teacher(room, data.get('teacher_key')):
        return jsonify({'ok': False, 'error': 'Không có quyền giáo viên'}), 403
    with db() as c:
        c.execute("UPDATE rooms SET status='lobby', started_at=NULL, ends_at=NULL WHERE code=?", (room['code'],))
        c.execute('DELETE FROM results WHERE room_code=?', (room['code'],))
    return jsonify({'ok': True})


@app.post('/api/rooms/<code>/submit')
def submit_result(code):
    room = normalized_room(get_room(code))
    data = request.get_json(silent=True) or {}
    pid = str(data.get('participant_id', ''))
    if not room or room['status'] not in ('playing', 'finished'):
        return jsonify({'ok': False, 'error': 'Phòng chưa bắt đầu hoặc không tồn tại'}), 400
    with db() as c:
        p = c.execute('SELECT * FROM participants WHERE id=? AND room_code=?', (pid, room['code'])).fetchone()
        if not p:
            return jsonify({'ok': False, 'error': 'Không xác định được học sinh'}), 403
        completed_at = now_ms()
        in_time = bool(room.get('ends_at')) and completed_at <= room['ends_at']
        elapsed = None
        if room.get('started_at'):
            elapsed = max(0, completed_at - room['started_at'])
        c.execute('''INSERT INTO results(participant_id,room_code,name,completed,completed_at,elapsed_ms)
                     VALUES(?,?,?,?,?,?)
                     ON CONFLICT(participant_id) DO UPDATE SET completed=excluded.completed,
                     completed_at=excluded.completed_at, elapsed_ms=excluded.elapsed_ms''',
                  (pid, room['code'], p['name'], 1 if in_time else 0, completed_at, elapsed))
    return jsonify({'ok': True, 'accepted': in_time, 'elapsed_ms': elapsed})


@app.get('/api/rooms/<code>/results')
def results(code):
    room = normalized_room(get_room(code))
    key = request.args.get('teacher_key', '')
    if not require_teacher(room, key):
        return jsonify({'ok': False, 'error': 'Không có quyền giáo viên'}), 403
    with db() as c:
        people = c.execute('SELECT id,name,joined_at,last_seen FROM participants WHERE room_code=? ORDER BY joined_at', (room['code'],)).fetchall()
        rows = c.execute('SELECT participant_id,name,completed,completed_at,elapsed_ms FROM results WHERE room_code=? ORDER BY completed DESC, elapsed_ms ASC', (room['code'],)).fetchall()
    result_map = {r['participant_id']: dict(r) for r in rows}
    merged = []
    for p in people:
        r = result_map.get(p['id'])
        merged.append({'id': p['id'], 'name': p['name'], 'completed': bool(r and r['completed']),
                       'elapsed_ms': r['elapsed_ms'] if r else None})
    merged.sort(key=lambda x: (0 if x['completed'] else 1, x['elapsed_ms'] if x['elapsed_ms'] is not None else 10**15, x['name']))
    return jsonify({'ok': True, 'status': room['status'], 'participants': len(people),
                    'completed': sum(1 for x in merged if x['completed']), 'results': merged,
                    'ends_at': room.get('ends_at'), 'started_at': room.get('started_at')})


@app.get('/qr/<code>.png')
def qr(code):
    room = get_room(code)
    if not room:
        return 'Room not found', 404
    base = request.url_root.rstrip('/')
    url = f'{base}/?room={room["code"]}&role=student'
    img = qrcode.make(url)
    bio = io.BytesIO()
    img.save(bio, format='PNG')
    bio.seek(0)
    return send_file(bio, mimetype='image/png', max_age=30)


@app.get('/health')
def health():
    return jsonify({'ok': True})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '5000'))
    app.run(host='0.0.0.0', port=port, debug=False)

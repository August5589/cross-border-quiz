#!/usr/bin/env python3
"""
跨境理论题刷题系统
Flask + SQLite single-file app.
"""

import json
import os
import sqlite3
import random
import urllib.request
import urllib.error
from functools import wraps
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, session, jsonify, g
from werkzeug.security import generate_password_hash, check_password_hash


def hash_password(password):
    return generate_password_hash(password, method='pbkdf2:sha256')

app = Flask(__name__)
app.secret_key = 'cross-border-theory-2026-secret-key-change-in-production'

# ── Database connection ────────────────────────────────────

QUESTIONS_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cleaned_questions.json')
CRAM_FILE_A = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'question_database.json')
CRAM_FILE_B = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'supplementary_questions.json')

# Turso (cloud SQLite) via HTTP API — zero native deps, pure Python
TURSO_URL = os.environ.get('TURSO_DATABASE_URL', '')
TURSO_TOKEN = os.environ.get('TURSO_AUTH_TOKEN', '')
USE_TURSO = bool(TURSO_URL and TURSO_TOKEN)

# Local SQLite fallback
DATA_DIR = os.environ.get('RENDER_DISK_PATH', os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)
DATABASE = os.path.join(DATA_DIR, 'quiz.db')

if USE_TURSO:
    _host = TURSO_URL.split("://", 1)[-1]
    TURSO_PIPELINE = f"https://{_host}/v2/pipeline"
else:
    TURSO_PIPELINE = None


class _Row:
    """Tuple wrapper that supports dict-like column-name access (like sqlite3.Row)."""
    __slots__ = ('_cols', '_vals', '_idx')

    def __init__(self, columns, values):
        self._cols = columns
        self._vals = values
        self._idx = {c: i for i, c in enumerate(columns)}

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._vals[key]
        return self._vals[self._idx[key]]

    def keys(self):
        return self._cols

    def __iter__(self):
        return iter(self._cols)

    def __len__(self):
        return len(self._vals)


class _TursoHTTPCursor:
    """Cursor backed by a pre-fetched Turso HTTP API result."""
    def __init__(self, result):
        self._rows = []
        self._cols = []
        self._idx = 0

        if result is not None:
            self._cols = [c["name"] for c in result.get("cols", [])]
            raw_rows = result.get("rows", [])
            self._rows = [
                tuple(self._extract(cell) for cell in row)
                for row in raw_rows
            ]
        self.description = tuple((c,) for c in self._cols)

    @staticmethod
    def _extract(cell):
        if cell is None:
            return None
        if not isinstance(cell, dict):
            return cell
        v = cell.get("value")
        if v is None:
            return None
        t = cell.get("type")
        if t == "integer":
            try:
                return int(v)
            except (ValueError, TypeError):
                return v
        if t == "float":
            try:
                return float(v)
            except (ValueError, TypeError):
                return v
        return v  # text, blob, or unknown

    def _wrap(self, row):
        if row is None:
            return None
        return _Row(self._cols, row)

    def fetchone(self):
        if self._idx >= len(self._rows):
            return None
        row = self._rows[self._idx]
        self._idx += 1
        return self._wrap(row)

    def fetchall(self):
        result = [self._wrap(r) for r in self._rows[self._idx:]]
        self._idx = len(self._rows)
        return result

    def fetchmany(self, size=None):
        if size is None:
            return self.fetchall()
        end = min(self._idx + size, len(self._rows))
        result = [self._wrap(r) for r in self._rows[self._idx:end]]
        self._idx = end
        return result


class _TursoHTTPConnection:
    """Database connection backed by Turso HTTP API (urllib, zero deps)."""
    def __init__(self, pipeline_url, token):
        self._url = pipeline_url
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _to_arg(v):
        """Convert a Python value to a Turso typed-arg dict."""
        if v is None:
            return {"type": "null"}
        if isinstance(v, bool):
            return {"type": "integer", "value": "1" if v else "0"}
        if isinstance(v, int):
            return {"type": "integer", "value": str(v)}
        if isinstance(v, float):
            return {"type": "float", "value": str(v)}
        if isinstance(v, bytes):
            import base64
            return {"type": "blob", "value": base64.b64encode(v).decode()}
        return {"type": "text", "value": str(v)}

    def execute(self, sql, params=None):
        if params is None:
            params = ()
        elif not isinstance(params, (list, tuple)):
            params = (params,)

        args = [self._to_arg(p) for p in params]
        body = json.dumps({
            "requests": [
                {"type": "execute", "stmt": {"sql": sql, "args": args}}
            ]
        }).encode("utf-8")

        req = urllib.request.Request(self._url, data=body, headers=self._headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            raise RuntimeError(f"Turso HTTP {e.code}: {body[:500]}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Turso unreachable: {e.reason}")

        results = data.get("results", [])
        if not results:
            return _TursoHTTPCursor(None)

        r = results[0]
        rtype = r.get("type", "")
        if rtype != "ok":
            raise RuntimeError(f"Turso error: {r}")

        resp_obj = r.get("response", {})
        if resp_obj.get("type") == "execute":
            return _TursoHTTPCursor(resp_obj.get("result", {}))
        return _TursoHTTPCursor(None)

    def executemany(self, sql, params_list):
        """Execute the same SQL with multiple param sets in a single pipeline request."""
        requests = []
        for params in params_list:
            args = [self._to_arg(p) for p in params]
            requests.append({"type": "execute", "stmt": {"sql": sql, "args": args}})

        body = json.dumps({"requests": requests}).encode("utf-8")
        req = urllib.request.Request(self._url, data=body, headers=self._headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            ebody = e.read().decode("utf-8", errors="replace") if e.fp else ""
            raise RuntimeError(f"Turso HTTP {e.code}: {ebody[:500]}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"Turso unreachable: {e.reason}")

        for r in data.get("results", []):
            if r.get("type") != "ok":
                raise RuntimeError(f"Turso error in batch: {r}")
        return _TursoHTTPCursor(None)

    def commit(self):
        pass  # each execute() is auto-committed via HTTP

    def close(self):
        pass


def connect_db():
    """Return a database connection (Turso HTTP API or local SQLite)."""
    if USE_TURSO:
        return _TursoHTTPConnection(TURSO_PIPELINE, TURSO_TOKEN)
    else:
        conn = sqlite3.connect(DATABASE)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn


PER_PAGE = 5
EXAM_SIZE = 100
CRAM_PER_PAGE = 20


# ── Cram (极速备考) question loader ────────────────────────

def _load_cram_questions():
    """Load + merge cram question files, mapping answers to text."""
    questions = []

    for fpath in (CRAM_FILE_A, CRAM_FILE_B):
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        questions.extend(data)

    result = []
    for i, q in enumerate(questions):
        qtype = q.get('type', '')
        answer = q.get('answer', '').strip().upper()
        options = q.get('options', [])

        # Map answer letter(s) → actual text
        answer_text = ''
        if '判断' in qtype:
            answer_text = '对' if answer in ('A', '√', '对', '正确') else '错'
        elif '多选' in qtype:
            # Multi-choice: "ABCD" → map each letter to option text
            parts = []
            for ch in answer:
                idx = ord(ch) - ord('A') if 'A' <= ch <= 'Z' else -1
                if 0 <= idx < len(options):
                    opt = str(options[idx]).strip()
                    if len(opt) > 2 and opt[1] in ('.', '、', '．') and opt[0].isascii() and opt[0].isalpha():
                        opt = opt[2:].strip()
                    parts.append(opt)
            answer_text = '；'.join(parts) if parts else answer
        else:
            # Single choice or unknown — map first letter
            first = answer[0] if answer else ''
            idx = ord(first) - ord('A') if first and 'A' <= first <= 'Z' else -1
            if 0 <= idx < len(options):
                opt = str(options[idx]).strip()
                if len(opt) > 2 and opt[1] in ('.', '、', '．') and opt[0].isascii() and opt[0].isalpha():
                    opt = opt[2:].strip()
                answer_text = opt
            else:
                answer_text = answer

        result.append({
            'num': q.get('number', i + 1),
            'type': qtype,
            'question': q.get('question', ''),
            'answer': answer_text,
        })

    return result


CRAM_QUESTIONS = _load_cram_questions()
CRAM_TOTAL_PAGES = max(1, (len(CRAM_QUESTIONS) + CRAM_PER_PAGE - 1) // CRAM_PER_PAGE)


# ── Database ──────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        g.db = connect_db()
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = connect_db()
    statements = [
        '''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''',
        '''CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            qtype TEXT NOT NULL,
            question_text TEXT NOT NULL,
            options TEXT NOT NULL,
            answer TEXT NOT NULL,
            original_num INTEGER
        )''',
        '''CREATE TABLE IF NOT EXISTS user_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            user_answer TEXT NOT NULL,
            is_correct INTEGER NOT NULL DEFAULT 0,
            mode TEXT DEFAULT 'practice',
            answered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (question_id) REFERENCES questions(id)
        )''',
        '''CREATE TABLE IF NOT EXISTS wrong_questions (
            user_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, question_id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (question_id) REFERENCES questions(id)
        )''',
    ]
    for stmt in statements:
        db.execute(stmt)
    db.execute('CREATE INDEX IF NOT EXISTS idx_user_answers_user ON user_answers(user_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_user_answers_question ON user_answers(question_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_wrong_questions_user ON wrong_questions(user_id)')
    db.commit()
    db.close()


def import_questions():
    """Import questions from cleaned JSON into SQLite.
    If the question count in DB differs from the JSON, clear and re-import.
    """
    db = connect_db()

    # Count questions in JSON
    with open(QUESTIONS_JSON, 'r', encoding='utf-8') as f:
        data = json.load(f)

    json_total = sum(len(data.get(cat, [])) for cat in ('singles', 'multis', 'judges', 'others'))

    db_count = db.execute('SELECT COUNT(*) FROM questions').fetchone()[0]

    if db_count == json_total and db_count > 0:
        db.close()
        return db_count  # Already up to date

    # Count mismatch — clear and re-import
    if db_count > 0:
        print(f"  DB has {db_count} questions, JSON has {json_total}. Re-importing...")
        db.execute('DELETE FROM wrong_questions')
        db.execute('DELETE FROM user_answers')
        db.execute('DELETE FROM questions')
        db.commit()

    imported = 0
    BATCH = 50 if USE_TURSO else 1

    for category, qs in [('singles', data['singles']), ('multis', data['multis']),
                          ('judges', data['judges']), ('others', data.get('others', []))]:
        batch_params = []
        for q in qs:
            batch_params.append((
                q['type'], q['question_text'],
                json.dumps(q['options'], ensure_ascii=False),
                q['answer'], q.get('original_num')
            ))
            if len(batch_params) >= BATCH:
                db.executemany(
                    'INSERT INTO questions (qtype, question_text, options, answer, original_num) VALUES (?, ?, ?, ?, ?)',
                    batch_params
                )
                imported += len(batch_params)
                print(f"  Imported {imported} questions...")
                batch_params = []

        if batch_params:
            db.executemany(
                'INSERT INTO questions (qtype, question_text, options, answer, original_num) VALUES (?, ?, ?, ?, ?)',
                batch_params
            )
            imported += len(batch_params)

    db.commit()
    db.close()
    return imported


def create_admin_user():
    """Create default admin account if not exists."""
    db = connect_db()
    exists = db.execute('SELECT id FROM users WHERE username = ?', ('admin',)).fetchone()
    if not exists:
        db.execute(
            'INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, 1)',
            ('admin', hash_password('admin123'))
        )
        db.commit()
    db.close()


# ── Auth helpers ──────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login_page'))
        if not session.get('is_admin'):
            return redirect(url_for('practice'))
        return f(*args, **kwargs)
    return decorated


def get_current_user():
    if 'user_id' not in session:
        return None
    db = get_db()
    return db.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()


# ── Routes: Auth ──────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()

        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['is_admin'] = bool(user['is_admin'])
            return redirect(url_for('practice'))

        return render_template('login.html', error='用户名或密码错误')

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))


def parse_question(q):
    """Convert a question row to a dict with parsed options."""
    d = dict(q)
    d['options'] = json.loads(d['options'])
    return d


# ── Routes: Practice ─────────────────────────────────────

@app.route('/')
@login_required
def index():
    return redirect(url_for('practice'))


@app.route('/practice')
@login_required
def practice():
    db = get_db()
    user = get_current_user()

    qtype = request.args.get('type', 'all')
    page = request.args.get('page', 1, type=int)

    # Build query
    if qtype == 'all':
        query = 'SELECT * FROM questions ORDER BY id'
        count_query = 'SELECT COUNT(*) FROM questions'
        params = []
    else:
        type_map = {'single': 'single', 'multi': 'multi', 'judge': 'judge'}
        db_type = type_map.get(qtype, qtype)
        query = 'SELECT * FROM questions WHERE qtype = ? ORDER BY id'
        count_query = 'SELECT COUNT(*) FROM questions WHERE qtype = ?'
        params = [db_type]

    total = db.execute(count_query, params).fetchone()[0]
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * PER_PAGE

    questions = [parse_question(q) for q in
                 db.execute(query + ' LIMIT ? OFFSET ?', params + [PER_PAGE, offset]).fetchall()]

    # Check which questions the user has already answered correctly
    answered_ids = set()
    if questions:
        q_ids = [q['id'] for q in questions]
        placeholders = ','.join('?' * len(q_ids))
        answered = db.execute(
            f'SELECT DISTINCT question_id FROM user_answers WHERE user_id = ? AND question_id IN ({placeholders})',
            [user['id']] + q_ids
        ).fetchall()
        answered_ids = {a['question_id'] for a in answered}

    # Count wrong questions for sidebar
    wrong_count = db.execute(
        'SELECT COUNT(*) FROM wrong_questions WHERE user_id = ?', (user['id'],)
    ).fetchone()[0]

    # Type counts for filter
    type_counts = {}
    for t in ['single', 'multi', 'judge']:
        type_counts[t] = db.execute('SELECT COUNT(*) FROM questions WHERE qtype = ?', (t,)).fetchone()[0]

    return render_template('practice.html',
                           questions=questions, page=page, total_pages=total_pages,
                           qtype=qtype, answered_ids=answered_ids,
                           wrong_count=wrong_count, type_counts=type_counts,
                           user=user)


@app.route('/check', methods=['POST'])
@login_required
def check_answer():
    """Check a single answer via AJAX."""
    db = get_db()
    user_id = session['user_id']
    question_id = request.json.get('question_id')
    user_answer = request.json.get('answer', '').strip().upper()

    question = db.execute('SELECT * FROM questions WHERE id = ?', (question_id,)).fetchone()
    if not question:
        return jsonify({'error': 'Question not found'}), 404

    correct_answer = question['answer'].strip().upper()

    # Determine correctness
    if question['qtype'] == 'multi':
        user_sorted = ''.join(sorted(user_answer))
        correct_sorted = ''.join(sorted(correct_answer))
        is_correct = user_sorted == correct_sorted
    else:
        is_correct = user_answer == correct_answer

    # Record answer
    db.execute(
        'INSERT INTO user_answers (user_id, question_id, user_answer, is_correct, mode) VALUES (?, ?, ?, ?, ?)',
        (user_id, question_id, user_answer, 1 if is_correct else 0, 'practice')
    )

    # Update wrong_questions
    if is_correct:
        db.execute('DELETE FROM wrong_questions WHERE user_id = ? AND question_id = ?',
                   (user_id, question_id))
    else:
        db.execute(
            'INSERT OR IGNORE INTO wrong_questions (user_id, question_id) VALUES (?, ?)',
            (user_id, question_id)
        )

    db.commit()

    wrong_count = db.execute(
        'SELECT COUNT(*) FROM wrong_questions WHERE user_id = ?', (user_id,)
    ).fetchone()[0]

    return jsonify({
        'is_correct': is_correct,
        'correct_answer': question['answer'],
        'wrong_count': wrong_count,
    })


# ── Routes: Wrong Questions ──────────────────────────────

@app.route('/wrong')
@login_required
def wrong_questions():
    db = get_db()
    user = get_current_user()
    user_id = user['id']

    page = request.args.get('page', 1, type=int)

    count = db.execute('SELECT COUNT(*) FROM wrong_questions WHERE user_id = ?', (user_id,)).fetchone()[0]
    total_pages = max(1, (count + PER_PAGE - 1) // PER_PAGE)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * PER_PAGE

    rows = [parse_question(r) for r in db.execute(
        '''SELECT q.* FROM questions q
           INNER JOIN wrong_questions w ON q.id = w.question_id
           WHERE w.user_id = ?
           ORDER BY w.created_at DESC
           LIMIT ? OFFSET ?''',
        (user_id, PER_PAGE, offset)
    ).fetchall()]

    wrong_count = count
    type_counts = {}
    for t in ['single', 'multi', 'judge']:
        type_counts[t] = db.execute('SELECT COUNT(*) FROM questions WHERE qtype = ?', (t,)).fetchone()[0]

    return render_template('wrong.html',
                           questions=rows, page=page, total_pages=total_pages,
                           wrong_count=wrong_count, type_counts=type_counts,
                           user=user)


@app.route('/wrong/check', methods=['POST'])
@login_required
def wrong_check_answer():
    """Check a wrong-question answer. If correct, remove from wrong list."""
    db = get_db()
    user_id = session['user_id']
    question_id = request.json.get('question_id')
    user_answer = request.json.get('answer', '').strip().upper()

    question = db.execute('SELECT * FROM questions WHERE id = ?', (question_id,)).fetchone()
    if not question:
        return jsonify({'error': 'Question not found'}), 404

    correct_answer = question['answer'].strip().upper()

    if question['qtype'] == 'multi':
        is_correct = ''.join(sorted(user_answer)) == ''.join(sorted(correct_answer))
    else:
        is_correct = user_answer == correct_answer

    db.execute(
        'INSERT INTO user_answers (user_id, question_id, user_answer, is_correct, mode) VALUES (?, ?, ?, ?, ?)',
        (user_id, question_id, user_answer, 1 if is_correct else 0, 'practice')
    )

    if is_correct:
        db.execute('DELETE FROM wrong_questions WHERE user_id = ? AND question_id = ?',
                   (user_id, question_id))
    else:
        db.execute(
            'INSERT OR IGNORE INTO wrong_questions (user_id, question_id) VALUES (?, ?)',
            (user_id, question_id)
        )

    db.commit()

    wrong_count = db.execute(
        'SELECT COUNT(*) FROM wrong_questions WHERE user_id = ?', (user_id,)
    ).fetchone()[0]

    return jsonify({
        'is_correct': is_correct,
        'correct_answer': question['answer'],
        'wrong_count': wrong_count,
        'removed': is_correct,
    })


# ── Routes: Cram (极速备考) ──────────────────────────────

@app.route('/cram')
@login_required
def cram():
    user = get_current_user()
    db = get_db()
    page = request.args.get('page', 1, type=int)
    page = max(1, min(page, CRAM_TOTAL_PAGES))
    start = (page - 1) * CRAM_PER_PAGE
    end = start + CRAM_PER_PAGE
    questions = CRAM_QUESTIONS[start:end]

    wrong_count = db.execute(
        'SELECT COUNT(*) FROM wrong_questions WHERE user_id = ?', (user['id'],)
    ).fetchone()[0]
    type_counts = {}
    for t in ['single', 'multi', 'judge']:
        type_counts[t] = db.execute('SELECT COUNT(*) FROM questions WHERE qtype = ?', (t,)).fetchone()[0]

    return render_template('cram.html',
                           questions=questions, page=page,
                           total_pages=CRAM_TOTAL_PAGES,
                           total_questions=len(CRAM_QUESTIONS),
                           wrong_count=wrong_count, type_counts=type_counts,
                           user=user)


# ── Routes: Exam ──────────────────────────────────────────

@app.route('/exam')
@login_required
def exam_page():
    user = get_current_user()
    db = get_db()
    wrong_count = db.execute(
        'SELECT COUNT(*) FROM wrong_questions WHERE user_id = ?', (user['id'],)
    ).fetchone()[0]
    type_counts = {}
    for t in ['single', 'multi', 'judge']:
        type_counts[t] = db.execute('SELECT COUNT(*) FROM questions WHERE qtype = ?', (t,)).fetchone()[0]
    return render_template('exam.html', user=user, wrong_count=wrong_count, type_counts=type_counts)


@app.route('/exam/start', methods=['POST'])
@login_required
def exam_start():
    """Generate 100 random questions for the exam."""
    db = get_db()
    user_id = session['user_id']

    # Get all question IDs
    all_ids = [row[0] for row in db.execute('SELECT id FROM questions').fetchall()]

    if len(all_ids) < EXAM_SIZE:
        exam_ids = all_ids
    else:
        exam_ids = random.sample(all_ids, EXAM_SIZE)

    questions = db.execute(
        f'SELECT * FROM questions WHERE id IN ({",".join("?" * len(exam_ids))})',
        exam_ids
    ).fetchall()

    # Add question number for display
    questions_list = []
    for i, q in enumerate(questions):
        qd = dict(q)
        qd['options'] = json.loads(q['options'])
        qd['exam_num'] = i + 1
        questions_list.append(qd)

    return jsonify({
        'questions': questions_list,
        'total': len(questions_list),
    })


@app.route('/exam/submit', methods=['POST'])
@login_required
def exam_submit():
    """Submit exam answers and get score."""
    db = get_db()
    user_id = session['user_id']
    data = request.json
    answers = data.get('answers', {})  # {question_id: user_answer}
    elapsed_seconds = data.get('elapsed_seconds', 0)

    if not answers:
        return jsonify({'error': 'No answers submitted'}), 400

    question_ids = list(answers.keys())
    placeholders = ','.join('?' * len(question_ids))
    questions = db.execute(
        f'SELECT * FROM questions WHERE id IN ({placeholders})', question_ids
    ).fetchall()

    correct_count = 0
    results = []

    for q in questions:
        user_answer = answers.get(str(q['id']), '').strip().upper()
        correct = q['answer'].strip().upper()

        if q['qtype'] == 'multi':
            is_correct = ''.join(sorted(user_answer)) == ''.join(sorted(correct))
        else:
            is_correct = user_answer == correct

        if is_correct:
            correct_count += 1

        # Record answer
        db.execute(
            'INSERT INTO user_answers (user_id, question_id, user_answer, is_correct, mode) VALUES (?, ?, ?, ?, ?)',
            (user_id, q['id'], user_answer, 1 if is_correct else 0, 'exam')
        )

        # Update wrong_questions
        if is_correct:
            db.execute('DELETE FROM wrong_questions WHERE user_id = ? AND question_id = ?',
                       (user_id, q['id']))
        else:
            db.execute(
                'INSERT OR IGNORE INTO wrong_questions (user_id, question_id) VALUES (?, ?)',
                (user_id, q['id'])
            )

        results.append({
            'question_id': q['id'],
            'question_text': q['question_text'],
            'options': json.loads(q['options']),
            'qtype': q['qtype'],
            'user_answer': user_answer,
            'correct_answer': q['answer'],
            'is_correct': is_correct,
        })

    db.commit()

    total = len(questions)
    score = round(correct_count / total * 100, 1) if total > 0 else 0

    return jsonify({
        'score': score,
        'correct_count': correct_count,
        'total': total,
        'elapsed_seconds': elapsed_seconds,
        'results': results,
    })


# ── Routes: Admin ─────────────────────────────────────────

@app.route('/admin')
@admin_required
def admin_panel():
    db = get_db()
    user = get_current_user()

    # Get all non-admin users
    users = db.execute('SELECT id, username, is_admin, created_at FROM users ORDER BY is_admin DESC, id').fetchall()

    user_stats = []
    for u in users:
        # Total unique questions answered
        total_done = db.execute(
            'SELECT COUNT(DISTINCT question_id) FROM user_answers WHERE user_id = ?', (u['id'],)
        ).fetchone()[0]

        # Total correct
        total_correct = db.execute(
            'SELECT COUNT(*) FROM user_answers WHERE user_id = ? AND is_correct = 1', (u['id'],)
        ).fetchone()[0]

        # Total answers (for rate calculation)
        total_answers = db.execute(
            'SELECT COUNT(*) FROM user_answers WHERE user_id = ?', (u['id'],)
        ).fetchone()[0]

        overall_rate = round(total_correct / total_answers * 100, 1) if total_answers > 0 else 0

        # By type
        type_stats = {}
        for t in ['single', 'multi', 'judge']:
            type_correct = db.execute(
                '''SELECT COUNT(*) FROM user_answers ua
                   INNER JOIN questions q ON ua.question_id = q.id
                   WHERE ua.user_id = ? AND ua.is_correct = 1 AND q.qtype = ?''',
                (u['id'], t)
            ).fetchone()[0]
            type_total = db.execute(
                '''SELECT COUNT(*) FROM user_answers ua
                   INNER JOIN questions q ON ua.question_id = q.id
                   WHERE ua.user_id = ? AND q.qtype = ?''',
                (u['id'], t)
            ).fetchone()[0]
            type_rate = round(type_correct / type_total * 100, 1) if type_total > 0 else 0
            type_stats[t] = {'correct': type_correct, 'total': type_total, 'rate': type_rate}

        wrong_count = db.execute(
            'SELECT COUNT(*) FROM wrong_questions WHERE user_id = ?', (u['id'],)
        ).fetchone()[0]

        user_stats.append({
            'id': u['id'],
            'username': u['username'],
            'is_admin': u['is_admin'],
            'created_at': u['created_at'],
            'total_done': total_done,
            'total_answers': total_answers,
            'total_correct': total_correct,
            'overall_rate': overall_rate,
            'type_stats': type_stats,
            'wrong_count': wrong_count,
        })

    wrong_count = db.execute(
        'SELECT COUNT(*) FROM wrong_questions WHERE user_id = ?', (user['id'],)
    ).fetchone()[0]
    type_counts = {}
    for t in ['single', 'multi', 'judge']:
        type_counts[t] = db.execute('SELECT COUNT(*) FROM questions WHERE qtype = ?', (t,)).fetchone()[0]

    return render_template('admin.html', user=user, user_stats=user_stats,
                           wrong_count=wrong_count, type_counts=type_counts)


@app.route('/admin/create-user', methods=['POST'])
@admin_required
def create_user():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()

    if not username or not password:
        return jsonify({'error': '用户名和密码不能为空'}), 400
    if len(password) < 4:
        return jsonify({'error': '密码至少4位'}), 400

    db = get_db()
    existing = db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
    if existing:
        return jsonify({'error': '用户名已存在'}), 400

    db.execute(
        'INSERT INTO users (username, password_hash) VALUES (?, ?)',
        (username, hash_password(password))
    )
    db.commit()

    return jsonify({'success': True, 'username': username})


@app.route('/admin/delete-user/<int:user_id>', methods=['POST'])
@admin_required
def delete_user(user_id):
    """Delete a user and all their data. Admin cannot delete themselves."""
    if user_id == session['user_id']:
        return jsonify({'error': '不能删除自己的账号'}), 400

    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if not user:
        return jsonify({'error': '用户不存在'}), 404
    if user['is_admin']:
        return jsonify({'error': '不能删除管理员账号'}), 400

    # Cascade delete all user data
    db.execute('DELETE FROM wrong_questions WHERE user_id = ?', (user_id,))
    db.execute('DELETE FROM user_answers WHERE user_id = ?', (user_id,))
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()

    return jsonify({'success': True, 'username': user['username']})


# ── Main ──────────────────────────────────────────────────

if __name__ == '__main__':
    import sys

    print("Initializing database...")
    init_db()

    print("Importing questions...")
    count = import_questions()
    print(f"  {count} questions in database")

    create_admin_user()
    print("Admin user ready (admin / admin123)")

    port = int(os.environ.get('PORT', 8080))
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'

    if debug:
        print(f"\n[DEV] Starting Flask dev server at http://localhost:{port}")
        app.run(debug=True, host='0.0.0.0', port=port)
    else:
        from waitress import serve
        print(f"\n[PROD] Starting Waitress server at http://0.0.0.0:{port}")
        serve(app, host='0.0.0.0', port=port)

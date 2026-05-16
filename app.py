#!/usr/bin/env python3
"""
跨境理论题刷题系统
Flask + SQLite single-file app.
"""

import json
import os
import sqlite3
import random
from functools import wraps
from datetime import datetime

from flask import Flask, render_template, request, redirect, url_for, session, jsonify, g
from werkzeug.security import generate_password_hash, check_password_hash


def hash_password(password):
    return generate_password_hash(password, method='pbkdf2:sha256')

app = Flask(__name__)
app.secret_key = 'cross-border-theory-2026-secret-key-change-in-production'

DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'quiz.db')
QUESTIONS_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'cleaned_questions.json')

PER_PAGE = 5
EXAM_SIZE = 100


# ── Database ──────────────────────────────────────────────

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DATABASE)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            qtype TEXT NOT NULL,
            question_text TEXT NOT NULL,
            options TEXT NOT NULL,
            answer TEXT NOT NULL,
            original_num INTEGER
        );

        CREATE TABLE IF NOT EXISTS user_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            user_answer TEXT NOT NULL,
            is_correct INTEGER NOT NULL DEFAULT 0,
            mode TEXT DEFAULT 'practice',
            answered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (question_id) REFERENCES questions(id)
        );

        CREATE TABLE IF NOT EXISTS wrong_questions (
            user_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, question_id),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (question_id) REFERENCES questions(id)
        );

        CREATE INDEX IF NOT EXISTS idx_user_answers_user ON user_answers(user_id);
        CREATE INDEX IF NOT EXISTS idx_user_answers_question ON user_answers(question_id);
        CREATE INDEX IF NOT EXISTS idx_wrong_questions_user ON wrong_questions(user_id);
    ''')
    db.commit()
    db.close()


def import_questions():
    """Import questions from cleaned JSON into SQLite."""
    db = sqlite3.connect(DATABASE)
    count = db.execute('SELECT COUNT(*) FROM questions').fetchone()[0]
    if count > 0:
        db.close()
        return count  # Already imported

    with open(QUESTIONS_JSON, 'r', encoding='utf-8') as f:
        data = json.load(f)

    imported = 0
    for category, qs in [('singles', data['singles']), ('multis', data['multis']),
                          ('judges', data['judges']), ('others', data.get('others', []))]:
        for q in qs:
            db.execute(
                'INSERT INTO questions (qtype, question_text, options, answer, original_num) VALUES (?, ?, ?, ?, ?)',
                (q['type'], q['question_text'], json.dumps(q['options'], ensure_ascii=False),
                 q['answer'], q.get('original_num'))
            )
            imported += 1

    db.commit()
    db.close()
    return imported


def create_admin_user():
    """Create default admin account if not exists."""
    db = sqlite3.connect(DATABASE)
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

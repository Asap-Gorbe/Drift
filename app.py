import os
from flask import Flask, render_template, request, redirect, url_for, session
import bcrypt
import psycopg2
from flask_socketio import SocketIO, send, join_room, leave_room
from psycopg2 import pool
from contextlib import contextmanager
from functools import wraps

def safe_handler(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            print(f"Error in {fn.__name__}: {e}")
            send("Something went wrong on the server.", to=request.sid)
    return wrapper
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(24))
socketio = SocketIO(app, async_mode="eventlet")

users = {}  # { socket_sid: username }
user_rooms = {}   # { sid: current_room_name }
DEFAULT_ROOM = "general"
db_pool = pool.ThreadedConnectionPool(
    minconn=1,
    maxconn=10,
    host="localhost",
    database="drift",
    user="postgres",
    password=os.environ.get("DB_PASSWORD", "8811awge")
)
USERNAME_MIN, USERNAME_MAX = 4, 30
PASSWORD_MIN, PASSWORD_MAX = 6, 72   # bcrypt ignores anything past 72 bytes
MESSAGE_MAX = 1000

def validate_username(username):
    if not username or not username.strip():
        return "Username cannot be empty"
    if not (USERNAME_MIN <= len(username) <= USERNAME_MAX):
        return f"Username must be {USERNAME_MIN}-{USERNAME_MAX} characters"
    return None

def validate_password(password):
    if not password:
        return "Password cannot be empty"
    if not (PASSWORD_MIN <= len(password) <= PASSWORD_MAX):
        return f"Password must be {PASSWORD_MIN}-{PASSWORD_MAX} characters"
    return None

@contextmanager
def get_cursor(commit=False):
    conn = db_pool.getconn()
    try:
        cur = conn.cursor()
        yield cur
        if commit:
            conn.commit()
        cur.close()
    except Exception:
        conn.rollback()
        raise
    finally:
        db_pool.putconn(conn)
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())
def register_user(username, password):
    with get_cursor(commit=True) as cur:
        cur.execute("SELECT id FROM users WHERE username = %s;", (username,))
        if cur.fetchone():
            return False
        cur.execute("INSERT INTO users (username, password) VALUES (%s, %s);",
                    (username, hash_password(password)))
        return True

def login_user(username, password):
    with get_cursor() as cur:
        cur.execute("SELECT id, password FROM users WHERE username = %s;", (username,))
        result = cur.fetchone()
    if not result:
        return False
    user_id, hashed_pw = result
    return check_password(password, hashed_pw)

def get_user_id(username):
    with get_cursor() as cur:
        cur.execute("SELECT id FROM users WHERE username = %s;", (username,))
        result = cur.fetchone()
    return result[0] if result else None

def get_room_id(name):
    with get_cursor() as cur:
        cur.execute("SELECT id FROM rooms WHERE name = %s;", (name,))
        result = cur.fetchone()
    return result[0] if result else None

def get_room_history(room_name):
    with get_cursor() as cur:
        cur.execute("""
            SELECT u.username, m.content
            FROM messages m
            JOIN users u ON m.user_id = u.id
            JOIN rooms r ON m.room_id = r.id
            WHERE r.name = %s
            ORDER BY m.created_at ASC;
        """, (room_name,))
        return cur.fetchall()

def get_or_create_dm_room(id_a, id_b):
    name = dm_room_name(id_a, id_b)
    with get_cursor(commit=True) as cur:
        cur.execute("SELECT id FROM rooms WHERE name = %s;", (name,))
        if not cur.fetchone():
            cur.execute("INSERT INTO rooms (name) VALUES (%s);", (name,))
    return name

@app.route("/register", methods=["POST"])
def register():
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    error = validate_username(username) or validate_password(password)
    if error:
        return error, 400
    try:
        if not register_user(username, password):
            return "Username already exists!", 400
    except Exception as e:
        print(f"Register error: {e}")
        return "Something went wrong, please try again", 500
    return redirect(url_for("login_page"))
@app.route("/login", methods=["POST"])
def login():
    username = request.form.get("username", "")
    password = request.form.get("password", "")
    if not username or not password:
        return "Username and password required", 400
    try:
        ok = login_user(username, password)
    except Exception as e:
        print(f"Login error: {e}")
        return "Something went wrong, please try again", 500
    if not ok:
        return "Invalid credentials!", 401
    session["username"] = username
    return redirect(url_for("index"))
@app.route("/login_page")
def login_page():
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

@app.route("/")
def index():
    if "username" not in session:
        return redirect(url_for("login_page"))
    return render_template("index.html", username=session["username"])

@socketio.on("join")
@safe_handler
def handle_join():
    if "username" not in session:
        return
    username = session["username"]
    users[request.sid] = username
    switch_to_room(DEFAULT_ROOM)

@socketio.on("switch_room")
@safe_handler
def handle_switch_room(room_name):
    if not room_name or not room_name.strip():
        return
    switch_to_room(room_name.strip())

def switch_to_room(room_name):
    username = users.get(request.sid)
    if not username:
        return
    old_room = user_rooms.get(request.sid)
    if old_room:
        leave_room(old_room)
        send(f"{username} left", to=old_room)
    join_room(room_name)
    user_rooms[request.sid] = room_name
    history = get_room_history(room_name)
    for sender, content in history:
        send(f"[{sender}]: {content}", to=request.sid)
    send(f"{username} joined #{room_name}", to=room_name)

@socketio.on("message")
@safe_handler
def handle_message(msg):
    if not msg or not msg.strip():
        return
    if len(msg) > MESSAGE_MAX:
        send(f"Message too long (max {MESSAGE_MAX} characters)", to=request.sid)
        return
    msg = msg.strip()
    username = users.get(request.sid, "Unknown")
    room_name = user_rooms.get(request.sid, DEFAULT_ROOM)
    user_id = get_user_id(username)
    room_id = get_room_id(room_name)
    if user_id and room_id:
        with get_cursor(commit=True) as cur:
            cur.execute("INSERT INTO messages (user_id, content, room_id) VALUES (%s, %s, %s);",
                        (user_id, msg, room_id))
    send(f"[{username}]: {msg}", to=room_name)
@socketio.on("disconnect")
@safe_handler
def handle_disconnect():
    username = users.pop(request.sid, "Unknown")
    room_name = user_rooms.pop(request.sid, None)
    print(f"{username} disconnected")
    if room_name:
        send(f"{username} left the chat", to=room_name)
def dm_room_name(id_a, id_b):
    return f"dm_{min(id_a, id_b)}_{max(id_a, id_b)}"

def register_user(username, password):
    with get_cursor(commit=True) as cur:
        cur.execute("SELECT id FROM users WHERE username = %s;", (username,))
        if cur.fetchone():
            return False
        cur.execute("INSERT INTO users (username, password) VALUES (%s, %s);",
                    (username, hash_password(password)))
        return True

def login_user(username, password):
    with get_cursor() as cur:
        cur.execute("SELECT id, password FROM users WHERE username = %s;", (username,))
        result = cur.fetchone()
    if not result:
        return False
    user_id, hashed_pw = result
    return check_password(password, hashed_pw)

def get_user_id(username):
    with get_cursor() as cur:
        cur.execute("SELECT id FROM users WHERE username = %s;", (username,))
        result = cur.fetchone()
    return result[0] if result else None

def get_room_id(name):
    with get_cursor() as cur:
        cur.execute("SELECT id FROM rooms WHERE name = %s;", (name,))
        result = cur.fetchone()
    return result[0] if result else None

def get_room_history(room_name):
    with get_cursor() as cur:
        cur.execute("""
            SELECT u.username, m.content
            FROM messages m
            JOIN users u ON m.user_id = u.id
            JOIN rooms r ON m.room_id = r.id
            WHERE r.name = %s
            ORDER BY m.created_at ASC;
        """, (room_name,))
        return cur.fetchall()

def get_or_create_dm_room(id_a, id_b):
    name = dm_room_name(id_a, id_b)
    with get_cursor(commit=True) as cur:
        cur.execute("SELECT id FROM rooms WHERE name = %s;", (name,))
        if not cur.fetchone():
            cur.execute("INSERT INTO rooms (name) VALUES (%s);", (name,))
    return name
@socketio.on("start_dm")
@safe_handler
def handle_start_dm(target_username):
    if "username" not in session:
        return
    my_id = get_user_id(session["username"])
    target_id = get_user_id(target_username)
    if not target_id:
        send("That user doesn't exist", to=request.sid)
        return
    if target_id == my_id:
        send("You can't DM yourself", to=request.sid)
        return
    room = get_or_create_dm_room(my_id, target_id)
    switch_to_room(room)
if __name__ == "__main__":
    print("Starting app")
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
import os
from flask import Flask, render_template, request, redirect, url_for, session
import bcrypt
import psycopg2
from flask_socketio import SocketIO, send, join_room, leave_room
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(24))
socketio = SocketIO(app, async_mode="eventlet")

users = {}  # { socket_sid: username }
user_rooms = {}   # { sid: current_room_name }
DEFAULT_ROOM = "general"
def get_connection():
    return psycopg2.connect(
        host="localhost",
        database="drift",
        user="postgres",
        password=os.environ.get("DB_PASSWORD", "8811awge")
    )

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())

def register_user(username, password):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE username = %s;", (username,))
    if cur.fetchone():
        cur.close()
        conn.close()
        return False
    cur.execute("INSERT INTO users (username, password) VALUES (%s, %s);",
                (username, hash_password(password)))
    conn.commit()
    cur.close()
    conn.close()
    return True

def login_user(username, password):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, password FROM users WHERE username = %s;", (username,))
    result = cur.fetchone()
    cur.close()
    conn.close()
    if not result:
        return False
    user_id, hashed_pw = result
    return check_password(password, hashed_pw)
def get_user_id(username):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE username = %s;", (username,))
    result = cur.fetchone()
    cur.close()
    conn.close()
    return result[0] if result else None

def get_room_id(name):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM rooms WHERE name = %s;", (name,))
    result = cur.fetchone()
    cur.close()
    conn.close()
    return result[0] if result else None

def get_room_history(room_name):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT u.username, m.content
        FROM messages m
        JOIN users u ON m.user_id = u.id
        JOIN rooms r ON m.room_id = r.id
        WHERE r.name = %s
        ORDER BY m.created_at ASC;
    """, (room_name,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows
@app.route("/register", methods=["POST"])
def register():
    username = request.form.get("username")
    password = request.form.get("password")
    if not register_user(username, password):
        return "Username already exists!", 400
    return redirect(url_for("login_page"))

@app.route("/login", methods=["POST"])
def login():
    username = request.form.get("username")
    password = request.form.get("password")
    if not login_user(username, password):
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
def handle_join():
    if "username" not in session:
        return
    username = session["username"]
    users[request.sid] = username
    switch_to_room(DEFAULT_ROOM)

@socketio.on("switch_room")
def handle_switch_room(room_name):
    switch_to_room(room_name)

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
def handle_message(msg):
    username = users.get(request.sid, "Unknown")
    room_name = user_rooms.get(request.sid, DEFAULT_ROOM)
    user_id = get_user_id(username)
    room_id = get_room_id(room_name)
    if user_id and room_id:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("INSERT INTO messages (user_id, content, room_id) VALUES (%s, %s, %s);",
                    (user_id, msg, room_id))
        conn.commit()
        cur.close()
        conn.close()
    send(f"[{username}]: {msg}", to=room_name)
@socketio.on("disconnect")
def handle_disconnect():
    username = users.pop(request.sid, "Unknown")
    room_name = user_rooms.pop(request.sid, None)
    print(f"{username} disconnected")
    if room_name:
        send(f"{username} left the chat", to=room_name)
def dm_room_name(id_a, id_b):
    return f"dm_{min(id_a, id_b)}_{max(id_a, id_b)}"

def get_or_create_dm_room(id_a, id_b):
    name = dm_room_name(id_a, id_b)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM rooms WHERE name = %s;", (name,))
    if not cur.fetchone():
        cur.execute("INSERT INTO rooms (name) VALUES (%s);", (name,))
        conn.commit()
    cur.close()
    conn.close()
    return name
@socketio.on("start_dm")
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
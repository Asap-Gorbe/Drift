import os
from flask import Flask, render_template, request, redirect, url_for, session
from flask_socketio import SocketIO, send, emit, join_room, leave_room
import psycopg2
import bcrypt
import secrets
import time
from PIL import Image
app = Flask(__name__)
# Set SECRET_KEY in your environment for production; this fallback is dev-only.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
socketio = SocketIO(app, async_mode="eventlet")

DEFAULT_ROOM = "general"

# In-memory map of live sockets -> who/where they are.
# sid -> {"username", "user_id", "room", "room_id"}
users = {}
UPLOAD_FOLDER = os.path.join("static", "avatars")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2 MB cap

# ---------- Database ----------

def get_connection():
    # TODO: rotate this password and read it from DB_PASSWORD instead.
    return psycopg2.connect(
        host="localhost",
        database="drift",
        user="postgres",
        password=os.environ.get("DB_PASSWORD", "8811awge"),
    )


# ---------- Password helpers ----------

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


# ---------- User helpers ----------

def register_user(username, password):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM users WHERE username = %s;", (username,))
        if cur.fetchone():
            return False
        cur.execute(
            "INSERT INTO users (username, password) VALUES (%s, %s);",
            (username, hash_password(password)),
        )
        conn.commit()
        return True
    finally:
        cur.close()
        conn.close()


def login_user(username, password):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT password FROM users WHERE username = %s;", (username,))
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if not row:
        return False
    return check_password(password, row[0])


def get_user_id(username):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM users WHERE username = %s;", (username,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cur.close()
        conn.close()


def get_username(user_id):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT username FROM users WHERE id = %s;", (user_id,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cur.close()
        conn.close()

def get_avatar(username):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT avatar FROM users WHERE username = %s;", (username,))
        row = cur.fetchone()
        return row[0] if row and row[0] else None
    finally:
        cur.close()
        conn.close()


def all_avatars():
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT username, avatar FROM users WHERE avatar IS NOT NULL;")
        return {r[0]: r[1] for r in cur.fetchall()}
    finally:
        cur.close()
        conn.close()


def set_avatar(user_id, filename):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET avatar = %s WHERE id = %s;", (filename, user_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()
# ---------- Room helpers ----------

def get_room_id(room_name):
    """Return the room's id, creating a (public) room if it doesn't exist."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM rooms WHERE name = %s;", (room_name,))
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute("INSERT INTO rooms (name) VALUES (%s) RETURNING id;", (room_name,))
        room_id = cur.fetchone()[0]
        conn.commit()
        return room_id
    finally:
        cur.close()
        conn.close()


def add_room_member(room_id, user_id):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO room_members (room_id, user_id) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING;",
            (room_id, user_id),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def get_room_history(room_id, limit=50):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT u.username, m.content "
            "FROM messages m JOIN users u ON u.id = m.user_id "
            "WHERE m.room_id = %s "
            "ORDER BY m.created_at ASC "
            "LIMIT %s;",
            (room_id, limit),
        )
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()


def save_message(user_id, room_id, content):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO messages (user_id, room_id, content) VALUES (%s, %s, %s);",
            (user_id, room_id, content),
        )
        conn.commit()
    finally:
        cur.close()
        conn.close()


def list_user_rooms(user_id):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT r.id, r.name, r.is_private "
            "FROM rooms r "
            "JOIN room_members rm ON rm.room_id = r.id "
            "WHERE rm.user_id = %s "
            "ORDER BY r.name;",
            (user_id,),
        )
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()


def dm_room_name(id_a, id_b):
    return f"dm_{min(id_a, id_b)}_{max(id_a, id_b)}"


def get_or_create_dm_room(id_a, id_b):
    """Return (room_name, room_id) for the private 1:1 room between two users."""
    name = dm_room_name(id_a, id_b)
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM rooms WHERE name = %s;", (name,))
        row = cur.fetchone()
        if row:
            return name, row[0]
        cur.execute(
            "INSERT INTO rooms (name, is_private) VALUES (%s, TRUE) RETURNING id;",
            (name,),
        )
        room_id = cur.fetchone()[0]
        conn.commit()
        return name, room_id
    finally:
        cur.close()
        conn.close()
def find_room(name):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, is_private FROM rooms WHERE name = %s;", (name,))
        return cur.fetchone()  # (id, is_private) or None
    finally:
        cur.close()
        conn.close()


def is_room_member(room_id, user_id):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT 1 FROM room_members WHERE room_id = %s AND user_id = %s;",
            (room_id, user_id),
        )
        return cur.fetchone() is not None
    finally:
        cur.close()
        conn.close()


def get_room_by_token(token):
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, name FROM rooms WHERE invite_token = %s;", (token,))
        return cur.fetchone()  # (id, name) or None
    finally:
        cur.close()
        conn.close()


def create_private_room(name):
    """Create a private room with an invite token. Returns (room_id, token) or
    None if the name is already taken."""
    token = secrets.token_urlsafe(16)
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM rooms WHERE name = %s;", (name,))
        if cur.fetchone():
            return None
        cur.execute(
            "INSERT INTO rooms (name, is_private, invite_token) "
            "VALUES (%s, TRUE, %s) RETURNING id;",
            (name, token),
        )
        room_id = cur.fetchone()[0]
        conn.commit()
        return room_id, token
    finally:
        cur.close()
        conn.close()

# ---------- Routes ----------

@app.route("/register", methods=["POST"])
def register():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    if not username or not password:
        return "Username and password are required!", 400
    if len(username) > 50:
        return "Username too long (max 50 characters)!", 400
    if not register_user(username, password):
        return "Username already exists!", 400
    return redirect(url_for("login_page"))


@app.route("/login", methods=["POST"])
def login():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    if not login_user(username, password):
        return "Invalid credentials!", 401
    session["username"] = username
    pending = session.pop("pending_join", None)
    if pending:
        return redirect(url_for("join_private", token=pending))
    return redirect(url_for("index"))

@app.route("/login_page")
def login_page():
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


@app.route("/")
@app.route("/")
def index():
    if "username" not in session:
        return redirect(url_for("login_page"))
    return render_template(
        "index.html",
        username=session["username"],
        initial_room=request.args.get("room", ""),
        avatar=get_avatar(session["username"]),
    )

@app.route("/join/<token>")
def join_private(token):
    if "username" not in session:
        session["pending_join"] = token   # remember it across login
        return redirect(url_for("login_page"))
    room = get_room_by_token(token)
    if not room:
        return "Invalid or expired invite link.", 404
    room_id, room_name = room
    user_id = get_user_id(session["username"])
    if user_id is not None:
        add_room_member(room_id, user_id)
    return redirect(url_for("index", room=room_name))
@app.route("/upload_avatar", methods=["POST"])
def upload_avatar():
    if "username" not in session:
        return redirect(url_for("login_page"))
    file = request.files.get("avatar")
    if not file or file.filename == "":
        return redirect(url_for("index"))
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        return "Unsupported image type.", 400

    # Verify it's actually a valid image, not just a renamed file.
    try:
        Image.open(file.stream).verify()
    except Exception:
        return "That file isn't a valid image.", 400
    file.stream.seek(0)  # verify() consumes the stream, so rewind before saving

    user_id = get_user_id(session["username"])
    old = get_avatar(session["username"])
    filename = f"{user_id}_{int(time.time())}.{ext}"
    file.save(os.path.join(UPLOAD_FOLDER, filename))
    set_avatar(user_id, filename)

    # Remove the previous avatar file so they don't accumulate.
    if old and old != filename:
        old_path = os.path.join(UPLOAD_FOLDER, old)
        if os.path.exists(old_path):
            os.remove(old_path)

    socketio.emit("avatars", all_avatars())
    return redirect(url_for("index"))
# ---------- Socket helpers ----------

def enter_room(sid, room_name):
    """Move a connected socket into a room: leave the old one, join the new one,
    record membership, replay history, and announce the join."""
    info = users.get(sid)
    if not info:
        return

    old_room = info["room"]
    if old_room and old_room != room_name:
        leave_room(old_room)
        send(f"{info['username']} left {old_room}", to=old_room)

    room_id = get_room_id(room_name)
    if info["user_id"] is not None:
        add_room_member(room_id, info["user_id"])

    info["room"] = room_name
    info["room_id"] = room_id
    join_room(room_name)

    # Replay recent history to the joining socket only.
    for hist_user, hist_msg in get_room_history(room_id):
        send(f"[{hist_user}]: {hist_msg}", to=sid)

    send(f"{info['username']} joined {room_name}", to=room_name)


# ---------- Socket handlers ----------

@socketio.on("join")
def handle_join(data=None):
    if "username" not in session:
        return
    username = session["username"]
    users[request.sid] = {
        "username": username,
        "user_id": get_user_id(username),
        "room": None,
        "room_id": None,
    }
    enter_room(request.sid, DEFAULT_ROOM)

@socketio.on("get_avatars")
def handle_get_avatars():
    emit("avatars", all_avatars())

@socketio.on("switch_room")
def handle_switch_room(room_name):
    if request.sid not in users:
        return
    room_name = (room_name or "").strip()
    if not room_name:
        return
    user_id = users[request.sid]["user_id"]
    room = find_room(room_name)
    if room:
        room_id, is_private = room
        if is_private and not is_room_member(room_id, user_id):
            send("That's a private room — you need an invite link to join.", to=request.sid)
            return
    enter_room(request.sid, room_name)

@socketio.on("get_rooms")
def handle_get_rooms():
    if "username" not in session:
        return
    user_id = get_user_id(session["username"])
    if user_id is None:
        return

    payload = []
    for room_id, name, is_private in list_user_rooms(user_id):
        entry = {"id": room_id, "name": name, "is_private": is_private}
        # For DM rooms (dm_<a>_<b>), show the *other* person's name.
        if name.startswith("dm_"):
            try:
                _, a, b = name.split("_")
                other_id = int(b) if int(a) == user_id else int(a)
                other_name = get_username(other_id)
                if other_name:
                    entry["display"] = other_name
            except (ValueError, IndexError):
                pass
        payload.append(entry)
    emit("room_list", payload)


@socketio.on("start_dm")
def handle_start_dm(target_username):
    if request.sid not in users:
        return
    target_username = (target_username or "").strip()
    if not target_username:
        return

    my_id = users[request.sid]["user_id"]
    target_id = get_user_id(target_username)
    if target_id is None:
        send("That user doesn't exist", to=request.sid)
        return
    if target_id == my_id:
        send("You can't DM yourself", to=request.sid)
        return

    dm_name, room_id = get_or_create_dm_room(my_id, target_id)
    # Both people are members so the DM shows up in each of their lists.
    add_room_member(room_id, my_id)
    add_room_member(room_id, target_id)
    enter_room(request.sid, dm_name)


@socketio.on("message")
def handle_message(msg):
    info = users.get(request.sid)
    if not info:
        return
    msg = (msg or "").strip()
    if not msg:
        return
    if len(msg) > 2000:
        msg = msg[:2000]

    if info["user_id"] is not None:
        save_message(info["user_id"], info["room_id"], msg)

    send(f"[{info['username']}]: {msg}", to=info["room"])


@socketio.on("disconnect")
def handle_disconnect():
    info = users.pop(request.sid, None)
    if not info or not info["room"]:
        return
    leave_room(info["room"])
    send(f"{info['username']} left {info['room']}", to=info["room"])

@socketio.on("search")
def handle_search(query):
    if request.sid not in users:
        return
    query = (query or "").strip()
    if not query:
        emit("search_results", {"users": [], "groups": []})
        return

    me = users[request.sid]["username"]
    pattern = f"%{query}%"
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT username FROM users "
            "WHERE username ILIKE %s AND username <> %s "
            "ORDER BY username LIMIT 8;",
            (pattern, me),
        )
        users_found = [r[0] for r in cur.fetchall()]

        cur.execute(
            "SELECT name FROM rooms "
            "WHERE is_private = FALSE AND name ILIKE %s AND LEFT(name, 3) <> 'dm_' "
            "ORDER BY name LIMIT 8;",
            (pattern,),
        )
        groups_found = [r[0] for r in cur.fetchall()]
    finally:
        cur.close()
        conn.close()

    emit("search_results", {"users": users_found, "groups": groups_found})

def create_group(name, is_private):
    """Create a group. Private groups get an invite token; public ones don't.
    Returns (room_id, token) or None if the name is taken."""
    token = secrets.token_urlsafe(16) if is_private else None
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM rooms WHERE name = %s;", (name,))
        if cur.fetchone():
            return None
        cur.execute(
            "INSERT INTO rooms (name, is_private, invite_token) "
            "VALUES (%s, %s, %s) RETURNING id;",
            (name, is_private, token),
        )
        room_id = cur.fetchone()[0]
        conn.commit()
        return room_id, token
    finally:
        cur.close()
        conn.close()

@socketio.on("create_group")
def handle_create_group(data):
    if request.sid not in users:
        return
    data = data or {}
    name = (data.get("name") or "").strip()
    is_private = bool(data.get("is_private"))
    if not name:
        return
    if len(name) > 50:
        send("Room name too long (max 50 characters).", to=request.sid)
        return

    result = create_group(name, is_private)
    if result is None:
        send("A room with that name already exists — pick another.", to=request.sid)
        return

    room_id, token = result
    add_room_member(room_id, users[request.sid]["user_id"])
    enter_room(request.sid, name)
    emit("group_created", {"name": name, "is_private": is_private, "token": token})

if __name__ == "__main__":
    print("Starting app")
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
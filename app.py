__version__ = "2.22"
import logging
import os
from contextlib import contextmanager
from dotenv import load_dotenv
load_dotenv()
from flask import Flask, render_template, request, redirect, url_for, session , jsonify
from flask_socketio import SocketIO, send, emit, join_room, leave_room
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import psycopg2
from psycopg2 import pool as pg_pool
import bcrypt
import secrets
import time
from datetime import datetime
from markupsafe import escape  # XSS: escapes <, >, &, " in user content
from PIL import Image
from flask_wtf.csrf import CSRFProtect
# --- Logging ---
# Single place to control format and level for the whole app.
# INFO shows normal activity; bump to DEBUG locally if needed.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# --- Startup validation ---
# Crash immediately with a clear message instead of failing mid-request
# when a required env var is missing.
if not os.environ.get("DB_PASSWORD"):
    raise RuntimeError("DB_PASSWORD environment variable is not set.")
if not os.environ.get("SECRET_KEY"):
    raise RuntimeError("SECRET_KEY environment variable is not set.")
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")
app.config["SESSION_COOKIE_HTTPONLY"] = True   # JS can't read the cookie (already Flask's default; explicit for clarity)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"  # cookie withheld on cross-site POSTs — the CSRF backstop
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes", "on")
app.config["WTF_CSRF_TIME_LIMIT"] = None   # token lives as long as the session — your chat page stays open for hours
csrf = CSRFProtect(app)
socketio = SocketIO(app, async_mode="eventlet")
limiter = Limiter(get_remote_address, app=app, default_limits=[])

DEFAULT_ROOM = "general"

# In-memory map of live sockets -> who/where they are.
# sid -> {"username", "user_id", "room", "room_id"}
users = {}
UPLOAD_FOLDER = os.path.join("static", "avatars")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2 MB cap
GROUP_UPLOAD_FOLDER = os.path.join("static", "groups")
os.makedirs(GROUP_UPLOAD_FOLDER, exist_ok=True)
# ---------- Database ----------

# --- Connection pool ---
# Opens 2 connections at startup, grows to 10 under load.
# Connections are borrowed and returned rather than opened/closed per query,
# which avoids the overhead and connection limit issues of the old approach.
_pool = pg_pool.ThreadedConnectionPool(
    minconn=2,
    maxconn=10,
    host="localhost",
    database="drift",
    user="postgres",
    password=os.environ["DB_PASSWORD"],
)


@contextmanager
def get_db():
    conn = _pool.getconn()
    try:
        yield conn
    finally:
        _pool.putconn(conn)


# ---------- Password helpers ----------

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


# ---------- User helpers ----------

def register_user(username, password, question, answer):
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT id FROM users WHERE username = %s;", (username,))
            if cur.fetchone():
                return False
            cur.execute(
                "INSERT INTO users (username, password, security_question, security_answer) "
                "VALUES (%s, %s, %s, %s);",
                (username, hash_password(password), question,
                 hash_password(_normalize_answer(answer))),
            )
            conn.commit()
            return True
        finally:
            cur.close()

def login_user(username, password):
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT password FROM users WHERE username = %s;", (username,))
            row = cur.fetchone()
        finally:
            cur.close()
    if not row:
        return False
    return check_password(password, row[0])
def _normalize_answer(answer):
    return (answer or "").strip().lower()


def set_security_question(user_id, question, answer):
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE users SET security_question = %s, security_answer = %s WHERE id = %s;",
                (question, hash_password(_normalize_answer(answer)), user_id),
            )
            conn.commit()
        finally:
            cur.close()


def get_security_question(username):
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT security_question FROM users WHERE username = %s;", (username,))
            row = cur.fetchone()
        finally:
            cur.close()
    return row[0] if row else None


def check_security_answer(username, answer):
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT security_answer FROM users WHERE username = %s;", (username,))
            row = cur.fetchone()
        finally:
            cur.close()
    if not row or not row[0]:
        return False
    return check_password(_normalize_answer(answer), row[0])

def get_user_id(username):
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT id FROM users WHERE username = %s;", (username,))
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            cur.close()


def get_username(user_id):
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT username FROM users WHERE id = %s;", (user_id,))
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            cur.close()


def get_avatar(username):
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT avatar FROM users WHERE username = %s;", (username,))
            row = cur.fetchone()
            return row[0] if row and row[0] else None
        finally:
            cur.close()


def all_avatars():
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT username, avatar FROM users WHERE avatar IS NOT NULL;")
            return {r[0]: r[1] for r in cur.fetchall()}
        finally:
            cur.close()


def set_avatar(user_id, filename):
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute("UPDATE users SET avatar = %s WHERE id = %s;", (filename, user_id))
            conn.commit()
        finally:
            cur.close()


# ---------- Room helpers ----------

def get_room_id(room_name):
    """Return the room's id, creating a (public) room if it doesn't exist."""
    with get_db() as conn:
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


def add_room_member(room_id, user_id):
    with get_db() as conn:
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
def remove_room_member(room_id, user_id):
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "DELETE FROM room_members WHERE room_id = %s AND user_id = %s;",
                (room_id, user_id),
            )
            removed = cur.rowcount > 0
            conn.commit()
            return removed
        finally:
            cur.close()

def get_room_history(room_id, limit=50):
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT m.id, u.username, m.content, m.created_at "
                "FROM messages m JOIN users u ON u.id = m.user_id "
                "WHERE m.room_id = %s "
                "ORDER BY m.created_at ASC "
                "LIMIT %s;",
                (room_id, limit),
            )
            return cur.fetchall()
        finally:
            cur.close()
def delete_message_db(message_id, user_id):
    """Delete a message only if it belongs to user_id.
    Returns True if a row was actually deleted, False otherwise."""
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "DELETE FROM messages WHERE id = %s AND user_id = %s;",
                (message_id, user_id),
            )
            deleted = cur.rowcount > 0
            conn.commit()
            return deleted
        finally:
            cur.close()
def edit_message_db(message_id, user_id, new_content):
    """Update a message's content only if it belongs to user_id.
    Returns True if a row was actually updated, False otherwise."""
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "UPDATE messages SET content = %s WHERE id = %s AND user_id = %s;",
                (new_content, message_id, user_id),
            )
            updated = cur.rowcount > 0
            conn.commit()
            return updated
        finally:
            cur.close()
def save_message(user_id, room_id, content):
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO messages (user_id, room_id, content) VALUES (%s, %s, %s) RETURNING id;",
                (user_id, room_id, content),
            )
            message_id = cur.fetchone()[0]
            conn.commit()
            return message_id
        finally:
            cur.close()


def list_user_rooms(user_id):
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT r.id, r.name, r.is_private, r.photo "
                "FROM rooms r "
                "JOIN room_members rm ON rm.room_id = r.id "
                "WHERE rm.user_id = %s "
                "ORDER BY r.name;",
                (user_id,),
            )
            return cur.fetchall()
        finally:
            cur.close()

def list_room_members(room_id):
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT u.id, u.username FROM room_members rm "
                "JOIN users u ON u.id = rm.user_id "
                "WHERE rm.room_id = %s ORDER BY u.username;",
                (room_id,),
            )
            return cur.fetchall()
        finally:
            cur.close()
def dm_room_name(id_a, id_b):
    return f"dm_{min(id_a, id_b)}_{max(id_a, id_b)}"


def get_or_create_dm_room(id_a, id_b):
    """Return (room_name, room_id) for the private 1:1 room between two users."""
    name = dm_room_name(id_a, id_b)
    with get_db() as conn:
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


def find_room(name):
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT id, is_private FROM rooms WHERE name = %s;", (name,))
            return cur.fetchone()  # (id, is_private) or None
        finally:
            cur.close()


def is_room_member(room_id, user_id):
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT 1 FROM room_members WHERE room_id = %s AND user_id = %s;",
                (room_id, user_id),
            )
            return cur.fetchone() is not None
        finally:
            cur.close()


def get_room_by_token(token):
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT id, name FROM rooms WHERE invite_token = %s;", (token,))
            return cur.fetchone()  # (id, name) or None
        finally:
            cur.close()


def create_private_room(name):
    """Create a private room with an invite token. Returns (room_id, token) or
    None if the name is already taken."""
    token = secrets.token_urlsafe(16)
    with get_db() as conn:
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
def save_uploaded_image(file, folder, prefix):
    """Validate an uploaded image and save it under `folder` with a unique
    name. Returns the filename, or None if no file was provided.
    Raises ValueError if the file exists but isn't a usable image."""
    if not file or file.filename == "":
        return None
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError("Unsupported image type.")
    try:
        Image.open(file.stream).verify()  # confirm it's a real image, not a renamed file
    except Exception:
        raise ValueError("That file isn't a valid image.")
    file.stream.seek(0)  # verify() consumes the stream, so rewind before saving
    filename = f"{prefix}_{int(time.time())}.{ext}"
    file.save(os.path.join(folder, filename))
    return filename
def is_owner(room_id, user_id):
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT owner_id FROM rooms WHERE id = %s;", (room_id,))
            row = cur.fetchone()
            return bool(row) and row[0] == user_id
        finally:
            cur.close()


def get_group(room_id):
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT name, description, photo, is_private, owner_id, invite_token "
                "FROM rooms WHERE id = %s;",
                (room_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "name": row[0], "description": row[1], "photo": row[2],
                "is_private": row[3], "owner_id": row[4], "invite_token": row[5],
            }
        finally:
            cur.close()


def update_group(room_id, name, description, is_private, photo=None):
    """Update editable fields. photo=None keeps the existing photo. Manages the
    invite token: private groups need one, public groups don't have a link."""
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT invite_token FROM rooms WHERE id = %s;", (room_id,))
            row = cur.fetchone()
            if not row:
                return
            token = (row[0] or secrets.token_urlsafe(16)) if is_private else None

            if photo is None:
                cur.execute(
                    "UPDATE rooms SET name=%s, description=%s, is_private=%s, invite_token=%s "
                    "WHERE id=%s;",
                    (name, description, is_private, token, room_id),
                )
            else:
                cur.execute(
                    "UPDATE rooms SET name=%s, description=%s, is_private=%s, invite_token=%s, photo=%s "
                    "WHERE id=%s;",
                    (name, description, is_private, token, photo, room_id),
                )
            conn.commit()
        finally:
            cur.close()


def regenerate_invite(room_id):
    token = secrets.token_urlsafe(16)
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute("UPDATE rooms SET invite_token = %s WHERE id = %s;", (token, room_id))
            conn.commit()
            return token
        finally:
            cur.close()

def delete_room_db(room_id):
    """Delete a room and everything attached to it, children first so we don't
    trip foreign keys."""
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute("DELETE FROM messages WHERE room_id = %s;", (room_id,))
            cur.execute("DELETE FROM room_members WHERE room_id = %s;", (room_id,))
            cur.execute("DELETE FROM rooms WHERE id = %s;", (room_id,))
            conn.commit()
        finally:
            cur.close()
# ---------- Routes ----------

@app.route("/register", methods=["POST"])
@limiter.limit("5 per minute")
def register():
    username = (request.form.get("username") or "").strip()
    if len(username) < 3:
        return render_template("login.html", mode="register",
                               error="Username must be at least 3 characters.", username=username)
    password = request.form.get("password") or ""
    if not username or not password:
        return render_template("login.html", mode="register",
                               error="Username and password are required.", username=username)
    if len(username) > 20:
        return render_template("login.html", mode="register",
                               error="Username too long (max 50 characters).", username=username)
    if len(password) < 6:
        return render_template("login.html", mode="register",
                               error="Password must be at least 6 characters.", username=username)

    question = (request.form.get("security_question") or "").strip()
    answer = (request.form.get("security_answer") or "").strip()
    if not question or not answer:
        return render_template("login.html", mode="register",
                               error="Please choose a security question and answer.", username=username)

    if not register_user(username, password, question, answer):
        return render_template("login.html", mode="register",
                               error="That username is already taken.", username=username)
    return render_template("login.html", mode="login",
                           notice="Account created — please sign in.", username=username)
@app.route("/login", methods=["POST"])
@limiter.limit("10 per minute")
def login():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    if not login_user(username, password):
        return render_template("login.html", mode="login",
                               error="Invalid username or password.", username=username)
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
@app.route("/create_group", methods=["POST"])
@limiter.limit("10 per minute")
def create_group_route():
    if "username" not in session:
        return jsonify({"error": "Not signed in."}), 401

    name = (request.form.get("name") or "").strip()
    description = (request.form.get("description") or "").strip() or None
    is_private = request.form.get("is_private") == "true"

    if not name:
        return jsonify({"error": "Group name is required."}), 400
    if name.lower().startswith("dm_"):
        return jsonify({"error": "Group names cannot start with 'dm_'."}), 400
    if len(name) > 50:
        return jsonify({"error": "Group name too long (max 50 characters)."}), 400

    try:
        photo = save_uploaded_image(request.files.get("photo"), GROUP_UPLOAD_FOLDER, "group")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    owner_id = get_user_id(session["username"])
    result = create_group(name, is_private, owner_id, description, photo)
    if result is None:
        return jsonify({"error": "A room with that name already exists — pick another."}), 409

    room_id, token = result
    add_room_member(room_id, owner_id)

    return jsonify({"name": name, "is_private": is_private, "token": token}), 201
@app.route("/group/<room_name>")
def group_details(room_name):
    if "username" not in session:
        return jsonify({"error": "Not signed in."}), 401
    found = find_room(room_name)
    if not found:
        return jsonify({"error": "Group not found."}), 404
    room_id, _ = found
    user_id = get_user_id(session["username"])
    if not is_room_member(room_id, user_id):
        return jsonify({"error": "Not a member."}), 403

    g = get_group(room_id)
    return jsonify({
        "name": g["name"],
        "description": g["description"],
        "photo": g["photo"],
        "is_private": g["is_private"],
        "is_owner": g["owner_id"] == user_id,
        "owner_id": g["owner_id"],
        "invite_token": g["invite_token"] if g["is_private"] else None,
        "members": [{"id": m[0], "username": m[1]} for m in list_room_members(room_id)],
    })
@app.route("/room/<room_name>/delete", methods=["POST"])
@limiter.limit("20 per minute")
def delete_room(room_name):
    if "username" not in session:
        return jsonify({"error": "Not signed in."}), 401
    found = find_room(room_name)
    if not found:
        return jsonify({"error": "Conversation not found."}), 404
    room_id, _ = found
    user_id = get_user_id(session["username"])

    if room_name.startswith("dm_"):
        # Either participant may delete a DM.
        if not is_room_member(room_id, user_id):
            return jsonify({"error": "You're not part of this conversation."}), 403
    else:
        # Only the owner may delete a group.
        if not is_owner(room_id, user_id):
            return jsonify({"error": "Only the group owner can delete it."}), 403

    g = get_group(room_id)            # grab the photo filename before the row is gone
    delete_room_db(room_id)
    if g and g["photo"]:
        p = os.path.join(GROUP_UPLOAD_FOLDER, g["photo"])
        if os.path.exists(p):
            os.remove(p)

    # Tell everyone currently in the room that it's gone.
    socketio.emit("room_deleted", {"name": room_name}, to=room_name)
    return jsonify({"ok": True})
@app.route("/group/<room_name>/edit", methods=["POST"])
@limiter.limit("20 per minute")
def group_edit(room_name):
    if "username" not in session:
        return jsonify({"error": "Not signed in."}), 401
    found = find_room(room_name)
    if not found:
        return jsonify({"error": "Group not found."}), 404
    room_id, _ = found
    user_id = get_user_id(session["username"])
    if not is_owner(room_id, user_id):
        return jsonify({"error": "Only the group owner can edit it."}), 403

    new_name = (request.form.get("name") or "").strip()
    description = (request.form.get("description") or "").strip() or None
    is_private = request.form.get("is_private") == "true"

    if not new_name:
        return jsonify({"error": "Group name is required."}), 400
    if new_name.lower().startswith("dm_"):
        return jsonify({"error": "Group names cannot start with 'dm_'."}), 400
    if len(new_name) > 50:
        return jsonify({"error": "Group name too long (max 50 characters)."}), 400
    if new_name != room_name and find_room(new_name):
        return jsonify({"error": "That name is already taken."}), 409

    old = get_group(room_id)
    try:
        photo = save_uploaded_image(request.files.get("photo"), GROUP_UPLOAD_FOLDER, "group")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    update_group(room_id, new_name, description, is_private, photo)

    # Clean up the replaced photo file, if any.
    if photo and old["photo"] and old["photo"] != photo:
        old_path = os.path.join(GROUP_UPLOAD_FOLDER, old["photo"])
        if os.path.exists(old_path):
            os.remove(old_path)

    # Tell people currently in the room to refresh (the name may have changed).
    socketio.emit("group_updated", {"old_name": room_name, "new_name": new_name}, to=room_name)

    g = get_group(room_id)
    return jsonify({
        "name": g["name"],
        "is_private": g["is_private"],
        "invite_token": g["invite_token"] if g["is_private"] else None,
    })


@app.route("/group/<room_name>/regenerate_link", methods=["POST"])
@limiter.limit("10 per minute")
def group_regenerate_link(room_name):
    if "username" not in session:
        return jsonify({"error": "Not signed in."}), 401
    found = find_room(room_name)
    if not found:
        return jsonify({"error": "Group not found."}), 404
    room_id, is_private = found
    user_id = get_user_id(session["username"])
    if not is_owner(room_id, user_id):
        return jsonify({"error": "Only the group owner can change the link."}), 403
    if not is_private:
        return jsonify({"error": "Public groups don't have an invite link."}), 400
    token = regenerate_invite(room_id)
    return jsonify({"invite_token": token})

# ---------- Socket helpers ----------

def enter_room(sid, room_name):
    """Set the socket's active room: ensure membership + subscription,
    replay history, and announce only a genuinely new join."""
    info = users.get(sid)
    if not info:
        return

    room_id = get_room_id(room_name)

    # Only announce the first real join, not every view-switch between rooms.
    is_new_member = False
    if info["user_id"] is not None:
        is_new_member = not is_room_member(room_id, info["user_id"])
        add_room_member(room_id, info["user_id"])

    info["room"] = room_name
    info["room_id"] = room_id
    join_room(room_name)  # idempotent; also covers brand-new rooms

    # Tell the client which room is now active (authoritative — fixes DMs,
    # whose room name the client doesn't know). Must come before history so
    # currentRoom is set before those messages arrive.
    emit("active_room", room_name, to=sid)

    for hist_id, hist_user, hist_msg, hist_time in get_room_history(room_id):
        emit("message", {
            "id": hist_id,
            "sender": hist_user,
            "text": hist_msg,
            "time": hist_time.strftime("%H:%M") if hist_time else "",
            "room": room_name,
        }, to=sid)

    if is_new_member:
        send(f"{info['username']} joined {room_name}", to=room_name)


# ---------- Socket handlers ----------

@socketio.on("join")
def handle_join(data=None):
    if "username" not in session:
        return
    username = session["username"]
    user_id = get_user_id(username)
    users[request.sid] = {
        "username": username,
        "user_id": user_id,
        "room": None,
        "room_id": None,
    }
    # Stay subscribed to every conversation the user is in, so messages from
    # rooms they aren't currently viewing still reach the browser.
    if user_id is not None:
        for _id, room_name, _priv, _photo in list_user_rooms(user_id):
            join_room(room_name)

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
    for room_id, name, is_private, photo in list_user_rooms(user_id):
        entry = {"id": room_id, "name": name, "is_private": is_private, "photo": photo}
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
    if not info or not info["room"]:
        return
    msg = (msg or "").strip()
    if not msg:
        return
    if len(msg) > 2000:
        msg = msg[:2000]
    msg = str(escape(msg))  # XSS: neutralise <script>, HTML tags, etc. before save + broadcast

    message_id = None
    if info["user_id"] is not None:
        message_id = save_message(info["user_id"], info["room_id"], msg)

    emit("message", {
        "id": message_id,
        "sender": info["username"],
        "text": msg,
        "time": datetime.now().strftime("%H:%M"),
        "room": info["room"],
    }, to=info["room"])

@socketio.on("delete_message")
def handle_delete_message(data):
    info = users.get(request.sid)
    if not info or not info["room"]:
        return
    data = data or {}
    message_id = data.get("id")
    if message_id is None:
        return
    if delete_message_db(message_id, info["user_id"]):
        # Tell everyone in the room to drop it from their view.
        socketio.emit("message_deleted", {"id": message_id}, to=info["room"])
@socketio.on("edit_message")
def handle_edit_message(data):
    info = users.get(request.sid)
    if not info or not info["room"]:
        return
    data = data or {}
    message_id = data.get("id")
    new_text = (data.get("text") or "").strip()
    if message_id is None or not new_text:
        return
    if len(new_text) > 2000:
        new_text = new_text[:2000]
    new_text = str(escape(new_text))  # XSS: same neutralisation as a new message
    if edit_message_db(message_id, info["user_id"], new_text):
        socketio.emit("message_edited", {"id": message_id, "text": new_text}, to=info["room"])

@socketio.on("leave_group")
def handle_leave_group(data):
    info = users.get(request.sid)
    if not info:
        return
    data = data or {}
    room_name = (data.get("room") or "").strip()
    if not room_name or room_name.startswith("dm_"):
        return  # DMs are deleted, not "left"
    found = find_room(room_name)
    if not found:
        return
    room_id, _is_private = found
    user_id = info["user_id"]
    if is_owner(room_id, user_id):
        send("You own this group — delete it instead of leaving.", to=request.sid)
        return
    if remove_room_member(room_id, user_id):
        leave_room(room_name)                       # stop receiving this room's messages
        emit("left_group", {"name": room_name}, to=request.sid)
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
    with get_db() as conn:
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

    emit("search_results", {"users": users_found, "groups": groups_found})


def create_group(name, is_private, owner_id=None, description=None, photo=None):
    """Create a group. Private groups get an invite token; public ones don't.
    Returns (room_id, token) or None if the name is taken."""
    token = secrets.token_urlsafe(16) if is_private else None
    with get_db() as conn:
        cur = conn.cursor()
        try:
            cur.execute("SELECT 1 FROM rooms WHERE name = %s;", (name,))
            if cur.fetchone():
                return None
            cur.execute(
                "INSERT INTO rooms (name, is_private, invite_token, owner_id, description, photo) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id;",
                (name, is_private, token, owner_id, description, photo),
            )
            room_id = cur.fetchone()[0]
            conn.commit()
            return room_id, token
        finally:
            cur.close()
@socketio.on("create_group")
def handle_create_group(data):
    if request.sid not in users:
        return
    data = data or {}
    name = (data.get("name") or "").strip()
    is_private = bool(data.get("is_private"))
    if not name:
        return
    if name.lower().startswith("dm_"):
        send("Room names cannot start with 'dm_'.", to=request.sid)
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
    log.info("Starting Drift v%s", __version__)
    debug = os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes", "on")
    socketio.run(app, host="0.0.0.0", port=5000, debug=debug)

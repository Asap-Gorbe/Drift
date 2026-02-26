from flask import Flask, render_template
from flask_socketio import SocketIO, send
from flask_socketio import emit
import psycopg2
from flask import request
import bcrypt
from flask import request, redirect, url_for, flash
from flask import session

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, async_mode="eventlet")
users = {}

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def check_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())
@app.route("/register", methods=["POST"])
def register():
    username = request.form.get("username")
    password = request.form.get("password")

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT id FROM users WHERE username = %s;", (username,))
    if cur.fetchone():
        cur.close()
        conn.close()
        return "Username already exists!", 400

    hashed_pw = hash_password(password)
    cur.execute("INSERT INTO users (username, password) VALUES (%s, %s);",
                (username, hashed_pw))
    conn.commit()
    cur.close()
    conn.close()
    return "Registered successfully!", 200
@app.route("/login", methods=["POST"])
def login():
    username = request.form.get("username")
    password = request.form.get("password")


    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT password FROM users WHERE username = %s;", (username,))
    result = cur.fetchone()
    cur.close()
    conn.close()

    if not result:
        return "User not found!", 404

    hashed_pw = result[0]
    if check_password(password, hashed_pw):
        return "Logged in successfully!", 200
    else:
        return "Wrong password!", 401

@app.route("/login_page")
def login_page():
    return render_template("login.html")
@app.route("/")
def index():
    return render_template("index.html")
@socketio.on("join")
def handle_join(username):
    users[request.sid] = username

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT id FROM users WHERE username = %s", (username,))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO users (username, password) VALUES (%s, %s);",
            (username, "temp")
        )
        conn.commit()

    cur.close()
    conn.close()

    print(f"{username} connected")
    send(f"{username} joined", broadcast=True)

@socketio.on("message")
def handle_message(msg):
    username = users.get(request.sid, "Unknown")
    full_message = f"[{username}]: {msg}"
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT id FROM users WHERE username = %s;", (username,))
    result = cur.fetchone()

    if result:
        user_id = result[0]
        cur.execute(
            "INSERT INTO messages (user_id, content) VALUES (%s, %s);",
            (user_id, msg)
        )
        conn.commit()

    cur.close()
    conn.close()

    send(full_message, broadcast=True)
@socketio.on("disconnect")
def handle_disconnect():
    username = users.get(request.sid, "Unknown")
    print(f"{username} disconnected")
    send(f"{username} left the chat", broadcast=True)
    users.pop(request.sid, None)
def get_connection():
    return psycopg2.connect(
        host="localhost",
        database="drift",
        user="postgres",
        password="8811awge"
    )
if __name__ == "__main__":
    print("Starting app")
    socketio.run(app, host="0.0.0.0", port=5000 ,debug=True)

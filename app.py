from flask import Flask, render_template
from flask_socketio import SocketIO, send
from flask_socketio import emit
from flask import request
app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app)
users = {}
@app.route("/")
def index():
    return render_template("index.html")
@socketio.on("join")
def handle_join(username):
    users[request.sid] = username
    print(f"{username} connected")
    send(f"{username} joined", broadcast=True)

@socketio.on("message")
def handle_message(msg):
    username = users.get(request.sid,"UnKnown")
    full_message = f"[{username}]:  {msg}"
    print(full_message)
    send(full_message, broadcast=True)

@socketio.on("disconnect")
def handle_disconnect():
    username = users.get(request.sid, "Unknown")
    print(f"{username} disconnected")
    send(f"{username} left the chat", broadcast=True)
    users.pop(request.sid, None)

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=8888)

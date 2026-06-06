# Drift

A real-time web messenger built with Flask and WebSockets — accounts, public and private group rooms, 1:1 direct messages, group photos, invite links, and live unread tracking.

> Drift is a personal learning project. The "spy on people" line in the repo is a joke — it's an ordinary chat app.

---

## Features

- **Accounts** — register, log in, log out; passwords hashed with bcrypt; login rate-limited.
- **Real-time messaging** — instant delivery over WebSockets, persisted to Postgres, with recent history replayed when you open a conversation. Messages are HTML-escaped, and each shows the sender and a timestamp.
- **Direct messages** — 1:1 chats with any user.
- **Group rooms** — public (discoverable via search) or private (join by invite link).
- **Group management** — create a group with a name, photo, description, and type. The **owner** can later edit all of those, regenerate the private invite link, or delete the group. Members can view details and the link.
- **Delete** — owners delete groups; either participant deletes a DM. Deletion is live for everyone in the conversation.
- **Unread badges** — a per-conversation count in the sidebar, updated in real time even for chats you're not currently viewing.
- **Avatars** — upload a profile picture; group photos and user avatars appear throughout the UI.
- **Search** — find users and public groups.

## Tech stack

- **Backend:** Python, Flask, Flask-SocketIO (eventlet)
- **Database:** PostgreSQL (psycopg2, pooled connections)
- **Auth:** bcrypt + Flask signed-cookie sessions
- **Other:** Flask-Limiter (rate limiting), Pillow (image validation), python-dotenv (config)
- **Frontend:** server-rendered HTML templates + vanilla JS (Socket.IO client), plain CSS

---

## Getting started

### Prerequisites

- Python 3.10+
- PostgreSQL 13+

### 1. Clone and install

```bash
git clone https://github.com/Asap-Gorbe/Drift.git
cd Drift

python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

pip install Flask Flask-SocketIO Flask-Limiter eventlet psycopg2-binary bcrypt python-dotenv Pillow
```

(Optionally freeze these into a `requirements.txt` with `pip freeze > requirements.txt`.)

### 2. Create the database

```bash
createdb drift
```

Then create the tables (the schema the app expects):

```sql
CREATE TABLE users (
    id       SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    avatar   TEXT
);

CREATE TABLE rooms (
    id           SERIAL PRIMARY KEY,
    name         TEXT UNIQUE NOT NULL,
    is_private   BOOLEAN NOT NULL DEFAULT FALSE,
    invite_token TEXT,
    owner_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    description  TEXT,
    photo        TEXT
);

CREATE TABLE messages (
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER REFERENCES users(id),
    room_id    INTEGER REFERENCES rooms(id),
    content    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE room_members (
    room_id INTEGER REFERENCES rooms(id),
    user_id INTEGER REFERENCES users(id),
    PRIMARY KEY (room_id, user_id)
);
```

### 3. Configure environment

Create a `.env` file in the project root:

```
DB_PASSWORD=your-postgres-password
SECRET_KEY=your-long-random-secret
```

Generate a strong `SECRET_KEY` with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Make sure `.env` is listed in `.gitignore` so secrets never get committed.

### 4. Run

```bash
python app.py
```

Open <http://localhost:5000>, register an account, and start a conversation.

---

## Project structure

```
Drift/
├── app.py                 # Flask app: routes, socket handlers, DB access
├── templates/
│   ├── index.html         # main chat UI
│   └── login.html         # sign-in / register
├── static/
│   ├── style.css          # all styling
│   ├── avatars/           # uploaded user avatars
│   ├── groups/            # uploaded group photos
│   └── bloommoon.jpeg     # empty-state image
├── .env                   # secrets (not committed)
└── CLAUDE.md              # internal project notes / context
```

(The `avatars/` and `groups/` folders are created automatically on startup if missing.)

---

## Security notes

This is a learning project and a few hardening steps remain before it should be exposed publicly:

- **`SECRET_KEY`** currently falls back to a default if unset — make it required, like `DB_PASSWORD`, since the session cookie is signed with it.
- **`debug=True`** is enabled in `app.py`; turn it off (or gate it behind an env var) for anything non-local — it exposes an interactive debugger.
- **No CSRF protection** on form POST routes yet.

---

## License

Add a license of your choice (e.g. MIT) before publishing.

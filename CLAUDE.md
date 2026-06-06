# Drift — Project Context (v2.21)

A real-time web messenger: accounts, public/private group rooms, 1:1 DMs, group photos, and live unread tracking. (The "spy on people" line in the repo is a joke — it's an ordinary chat app.)

Repo: https://github.com/Asap-Gorbe/Drift

---

## Stack

- **Backend:** Python + Flask
- **Real-time:** Flask-SocketIO (eventlet), WebSockets
- **Database:** PostgreSQL via psycopg2, using a `ThreadedConnectionPool` (2–10 connections) behind a `get_db()` context manager
- **Auth/crypto:** bcrypt password hashing; Flask `session` (signed cookie) for login state
- **Config:** `python-dotenv` loads `.env`; startup validation raises if `DB_PASSWORD` is missing
- **Rate limiting:** Flask-Limiter on auth and group routes
- **Images:** Pillow validates uploads; files saved under `static/`
- **Safety:** `markupsafe.escape` on message content; logging configured app-wide

## Data model (Postgres)

- **users**: `id`, `username`, `password` (bcrypt hash), `avatar` (filename, nullable)
- **rooms**: `id`, `name`, `is_private`, `invite_token` (private only), `owner_id` → users(id) ON DELETE SET NULL, `description`, `photo` (filename)
- **messages**: `id`, `user_id`, `room_id`, `content`, `created_at`
- **room_members**: `room_id`, `user_id`
- DMs are ordinary rooms named `dm_<lowId>_<highId>`, `is_private = TRUE`.

---

## Real-time model (important)

- On connect, a socket **subscribes to every room the user belongs to** (`handle_join` loops `list_user_rooms` and `join_room`s each), so messages from background conversations still reach the client.
- Each message is a **structured object**: `{ sender, text, time, room }`. System notices (e.g. "X joined") are still plain strings — the client branches on `typeof`.
- The client renders a message only if `data.room === currentRoom`; otherwise it bumps that room's **unread badge** in the sidebar. Opening a room clears its count.
- The server tells the client which room is active via the `active_room` event (authoritative — this is how DMs, whose name the client doesn't know, get `currentRoom` set).
- Timestamps are formatted **server-side as `HH:MM`** (local clock). Fine for local dev; for multi-timezone deploy, switch to sending a UTC timestamp and formatting client-side.

**Socket events**
- client→server: `join`, `get_rooms`, `get_avatars`, `switch_room`, `start_dm`, `search`, and `message` (via `socket.send`)
- server→client: `room_list`, `avatars`, `active_room`, `message`, `group_updated`, `room_deleted`, `search_results`

**HTTP routes**
- `/register`, `/login`, `/login_page`, `/logout`, `/` (index), `/join/<token>` (accept invite)
- `/upload_avatar` (user avatar)
- `/create_group` (multipart: name, description, is_private, optional photo)
- `GET /group/<name>` (details + `is_owner`), `/group/<name>/edit`, `/group/<name>/regenerate_link`
- `/room/<name>/delete`

---

## Features (built)

- **Auth**: register / login / logout, bcrypt, rate-limited.
- **Messaging**: real-time, persisted, history replayed on join, XSS-escaped, structured payloads with timestamps.
- **Rooms**: public and private groups; 1:1 DMs; private rooms joined via invite links (`/join/<token>`).
- **Unread badges**: per-conversation counts in the sidebar, live.
- **Sidebar**: Telegram-style rows — avatar + name. DM rows show the other user's avatar; group rows show the group photo, or a colored initial circle if none; lock icon on private groups; unread badge on the right.
- **User avatars**: upload + validation; shown in messages and on DM sidebar rows.
- **Groups (full lifecycle)**:
  - Create via a modal (name, photo, description, public/private).
  - **Owner-only** editing of name, photo, description, type (click the group name in the header → panel; non-owners see it read-only).
  - Invite links for **private groups only**; **owner-only** regenerate; members can view.
  - Photos stored in `static/groups/`.
- **Delete**: groups deletable by the **owner only**; DMs deletable by **either participant**. Deletes the whole conversation (messages + memberships + room) and fires `room_deleted` so anyone with it open is bounced to the empty state and it leaves their sidebar live.

## Permissions summary

- Edit group settings / regenerate link → **owner only** (enforced server-side via `is_owner`).
- Delete group → **owner only**. Delete DM → **either participant** (`is_room_member`).
- View a private group's invite link → any member.

---

## Security posture

- **Done**: parameterised SQL everywhere; bcrypt; rate limits on auth + group routes; DB password rotated and required at startup (no fallback); image uploads validated with Pillow; message output escaped + rendered via `textContent`.
- **Open / recommended** (last code seen still had these):
  - `SECRET_KEY` reads from env but with a dev fallback (`"dev-secret-change-me"`). Make it **required** like `DB_PASSWORD` — the fallback lets anyone forge a session cookie.
  - `debug=True` is hardcoded in `socketio.run`. Gate it behind an env var before any deployment (it exposes a remote code-execution console).
  - No CSRF protection on POST routes (`/login`, `/register`, `/upload_avatar`, `/create_group`, group edit/delete). Worth adding before going public.

## Known wrinkles

- **Renaming a group**: socket rooms are keyed by *name*, so a rename is handled by broadcasting `group_updated` (clients refresh and re-enter under the new name) rather than a true id-keyed refactor. Works, but it's the soft spot.
- A brand-new DM that *someone else* starts mid-session won't badge until the room list next refreshes.
- A genuine "X joined" notice can appear in whatever pane the recipient has open (rare; only on real first joins).
- **GitHub link caching**: the `blob/...` page serves stale copies and `raw.githubusercontent.com` blocks fetches — review by pasting/uploading the file or via Claude Code, not the GitHub URL.
- **Frontend paste rule** (a real mistake we hit): everything visual goes inside `<div class="app">…</div>`; everything that's code goes between `<script>` and `</script>`. A snippet starting with `<div` belongs in the first; one starting with `function`/`socket.on` belongs in the second.

## How I like to work (please follow)

- **Explain before changing** — what and *why this approach over alternatives*, in plain language, before the code.
- **One logical step at a time**, then wait so I can follow it.
- **Give the changed snippet + where it goes** — don't rewrite whole files.
- Treat me as a reviewer/learner, not a rubber stamp.

---

## Roadmap / next options

- Message-bubble **appearance pass** (parked).
- **Typing indicators**.
- **Edit / delete individual messages** (needs message IDs in the payload).
- **Presence** (who's online / in a room).
- **Deploy hardening**: SECRET_KEY required, gate `debug`, add CSRF.
- Optional polish: group photo in the chat header; avatars in the search dropdown.

## Where we left off

Just finished the **delete-chat** feature and bumped to **v2.21**. The group lifecycle (create / edit / link / delete) and the sidebar/unread system are complete. Natural next step: pick one from the roadmap — the appearance pass or message edit/delete are the most user-visible; the security items are the most important before any real deployment.
# Drift — Project Context (v2.24)

A real-time web messenger: accounts, public/private group rooms, 1:1 DMs, group photos, live unread tracking, **editable/deletable messages**, and **group membership management**. (The "spy on people" line in the repo is a joke — it's an ordinary chat app.)

Repo: https://github.com/Asap-Gorbe/Drift

---

## Stack

- **Backend:** Python + Flask
- **Real-time:** Flask-SocketIO (eventlet), WebSockets
- **Database:** PostgreSQL via psycopg2, using a `ThreadedConnectionPool` (2–10 connections) behind a `get_db()` context manager
- **Auth/crypto:** bcrypt password hashing; Flask `session` (signed cookie) for login state
- **CSRF:** Flask-WTF; hidden `csrf_token` in forms, `X-CSRFToken` header on fetch POSTs
- **Config:** `python-dotenv` loads `.env`; startup validation raises if `DB_PASSWORD` (and `SECRET_KEY`) is missing
- **Rate limiting:** Flask-Limiter on auth and group routes
- **Images:** Pillow validates uploads; files saved under `static/`
- **Safety:** `markupsafe.escape` on message content; logging configured app-wide

## Data model (Postgres)

- **users**: `id`, `username`, `password` (bcrypt hash), `avatar` (filename, nullable), `security_question` (text, nullable), `security_answer` (bcrypt hash of the normalized answer, nullable)
- **rooms**: `id`, `name`, `is_private`, `invite_token` (private only), `owner_id` → users(id) ON DELETE SET NULL, `description`, `photo` (filename)
- **messages**: `id`, `user_id`, `room_id`, `content`, `created_at` — `id` is now surfaced to the client and used as the handle for edit/delete
- **room_members**: `room_id`, `user_id`
- DMs are ordinary rooms named `dm_<lowId>_<highId>`, `is_private = TRUE`.

---

## Real-time model (important)

- On connect, a socket **subscribes to every room the user belongs to** (`handle_join` loops `list_user_rooms` and `join_room`s each), so messages from background conversations still reach the client.
- Each message is a **structured object**: `{ id, sender, text, time, room }`. `id` is the messages-table primary key (null only for the rare unsaved message). System notices (e.g. "X joined") are still plain strings — the client branches on `typeof`.
- The client stamps each rendered message row with `data-message-id`, so edit/delete can target a specific message in the DOM.
- The client renders a message only if `data.room === currentRoom`; otherwise it bumps that room's **unread badge** in the sidebar. Opening a room clears its count.
- The server tells the client which room is active via the `active_room` event (authoritative — this is how DMs, whose name the client doesn't know, get `currentRoom` set).
- Timestamps are formatted **server-side as `HH:MM`** (local clock). Fine for local dev; for multi-timezone deploy, switch to sending a UTC timestamp and formatting client-side.

**Socket events**
- client→server: `join`, `get_rooms`, `get_avatars`, `switch_room`, `start_dm`, `search`, `message` (via `socket.send`), `delete_message`, `edit_message`, `leave_group`
- server→client: `room_list`, `avatars`, `active_room`, `message`, `group_updated`, `room_deleted`, `search_results`, `message_deleted`, `message_edited`, `left_group`

**HTTP routes**
- `/register` (now also captures `security_question` + `security_answer`), `/login`, `/login_page`, `/logout`, `/` (index), `/join/<token>` (accept invite)
- `/upload_avatar` (user avatar)
- `/create_group` (multipart: name, description, is_private, optional photo)
- `GET /group/<name>` (details + `is_owner` + `owner_id` + `members[]`), `/group/<name>/edit`, `/group/<name>/regenerate_link`
- `/room/<name>/delete`
- *(pending — reset flow)* `/reset` (GET + POST)

---

## Features (built)

- **Auth**: register / login / logout, bcrypt, rate-limited, CSRF-protected. Registration captures a security question + answer (answer stored hashed).
- **Messaging**: real-time, persisted, history replayed on join, XSS-escaped, structured payloads with timestamps and **message ids**.
- **Message edit / delete (own messages only)**:
  - Each rendered message carries `data-message-id`; own messages show inline ✎ / × actions.
  - Delete: `delete_message` → SQL delete guarded by `WHERE id = %s AND user_id = %s` → broadcast `message_deleted` → every client drops the row.
  - Edit: `edit_message` → ownership-guarded update, **re-escaped server-side** (same path as a new message, so edits can't smuggle in HTML) → broadcast `message_edited` → clients rewrite the `.message__text`.
- **Rooms**: public and private groups; 1:1 DMs; private rooms joined via invite links (`/join/<token>`).
- **Unread badges**: per-conversation counts in the sidebar, live.
- **Sidebar**: Telegram-style rows — avatar + name. DM rows show the other user's avatar; group rows show the group photo, or a colored initial circle if none; lock icon on private groups; unread badge on the right.
- **User avatars**: upload + validation; shown in messages and on DM sidebar rows.
- **Groups (full lifecycle)**:
  - Create via a modal (name, photo, description, public/private).
  - **Owner-only** editing of name, photo, description, type.
  - Invite links for **private groups only**; **owner-only** regenerate; members can view.
  - **Member list**: the group panel lists members with avatars (aligned, Telegram-style rows). Avatars reuse `avatarEl`; the row is the flex container so img and initial-circle avatars line up identically. A subtitle slot per member is reserved for presence later.
  - **Leave a group**: any non-owner member can leave (`leave_group`); they're unsubscribed and the group leaves their sidebar live. Owners can't leave — they delete instead.
- **Delete**: groups deletable by the **owner only**; DMs deletable by **either participant**. Deletes the whole conversation and fires `room_deleted`.

## Permissions summary

- Edit group settings / regenerate link → **owner only** (enforced server-side via `is_owner`).
- Delete group → **owner only**. Delete DM → **either participant** (`is_room_member`).
- Leave group → **any non-owner member**; owner leaves by deleting the group.
- Edit / delete a message → **its author only** (enforced in SQL).
- View a private group's invite link → any member.

---

## Security posture

- **Done**: parameterised SQL everywhere; bcrypt; rate limits on auth + group routes; DB password + `SECRET_KEY` required at startup (no fallback); `debug` gated behind an env var; **CSRF via Flask-WTF** on POST routes; image uploads validated with Pillow; message output escaped + rendered via `textContent`; **security-question answers normalized + bcrypt-hashed**.
- **Notes / accepted tradeoffs**:
  - The (pending) reset flow reveals whether a username has a security question set, leaking account existence a little — already true of registration ("username taken"); accepted for a friends-scale app.
  - Security questions are weaker than email reset; mitigated by hashing + rate-limiting.

## Deployment / migrations (READ BEFORE PUSHING TO THE VPS)

This release adds two columns. **Run this on every environment's Postgres (including the live VPS) BEFORE the new code runs**, or `/register` will 500:

```sql
ALTER TABLE users ADD COLUMN security_question TEXT;
ALTER TABLE users ADD COLUMN security_answer   TEXT;
```

No backfill needed — existing rows get `NULL` (those users just can't use security-question reset until a settings panel lets them set one).

Files changed this session: `app.py`, `templates/index.html`, `templates/login.html`, `static/style.css`.

Deploy order on the VPS: pull → run the `ALTER TABLE` in psql → restart the systemd service.

## Known wrinkles

- **Renaming a group**: socket rooms are keyed by *name*, so a rename broadcasts `group_updated` (clients refresh and re-enter) rather than a true id-keyed refactor. Works, but it's the soft spot.
- A brand-new DM that *someone else* starts mid-session won't badge until the room list next refreshes.
- **Existing accounts have no security question** (`NULL`) — they can't use the (upcoming) reset flow until there's a logged-in settings panel to set one. New accounts are covered via registration.
- **GitHub link caching**: the `blob/...` page serves stale copies and `raw.githubusercontent.com` blocks fetches — review by pasting/uploading the file or via Claude Code, not the GitHub URL.
- **Frontend paste rule**: everything visual goes inside `<div class="app">…</div>`; everything that's code goes between `<script>` and `</script>`. A snippet starting with `<div` belongs in the first; one starting with `function`/`socket.on` belongs in the second.

## How I like to work (please follow)

- **Explain before changing** — what and *why this approach over alternatives*, in plain language, before the code.
- **One logical step at a time**, then wait so I can follow it.
- **Give the changed snippet + where it goes** — don't rewrite whole files.
- Treat me as a reviewer/learner, not a rubber stamp.

---

## Roadmap / next options

- **Password reset via security question — flow pending.** Groundwork + registration capture are done. Next: **3a** add the `/reset` route + `templates/reset.html` (consumes `get_security_question` / `check_security_answer` / `update_password`), then **3b** add the "Forgot password?" link on the login page.
- **Owner-remove-member (2B) — pending.** The member list is currently display-only; still need the owner's Remove button + a `/group/<name>/remove_member` route that boots the removed user's live socket.
- **Presence** (who's online / in a room) — also lights up the reserved member-list subtitle line.
- **Typing indicators**.
- **Settings panel** (logged-in): set/change security question, change password while logged in.
- Message-bubble **appearance pass** (parked).
- Optional polish: group photo in the chat header; avatars in the search dropdown.

## Where we left off

This session shipped **message edit/delete**, **group leave + member list** (with avatar-aligned rows), and the **account-recovery groundwork** (schema + helpers + registration capture). Committing here, **before** wiring the actual reset flow.

Immediate next steps, in order:
1. **3a** — `/reset` route + `templates/reset.html` (the two-stage username → question → answer + new password flow). `update_password` + the reset route/template were drafted but not yet applied.
2. **3b** — "Forgot password?" link on the login page.
3. **2B** — owner-remove-member (Remove button + route + force-unsubscribe).
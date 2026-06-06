-- Run once to set up the Drift database.
-- psql -U postgres -d drift -f schema.sql

CREATE TABLE IF NOT EXISTS users (
    id       SERIAL PRIMARY KEY,
    username VARCHAR(20)  NOT NULL UNIQUE,
    password TEXT         NOT NULL,
    avatar   TEXT
);

CREATE TABLE IF NOT EXISTS rooms (
    id           SERIAL PRIMARY KEY,
    name         VARCHAR(50)  NOT NULL UNIQUE,
    is_private   BOOLEAN      NOT NULL DEFAULT FALSE,
    invite_token TEXT
);

CREATE TABLE IF NOT EXISTS room_members (
    room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (room_id, user_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    room_id    INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    content    TEXT    NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS messages_room_created ON messages(room_id, created_at);

-- Seed the default public room.
INSERT INTO rooms (name, is_private)
VALUES ('general', FALSE)
ON CONFLICT DO NOTHING;
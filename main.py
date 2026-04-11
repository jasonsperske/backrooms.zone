from fastapi import FastAPI, Query, Header, HTTPException, Depends, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import sqlite3
import random
import os
from contextlib import contextmanager

app = FastAPI(title="Backrooms Zone Door API", docs_url="/api/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = os.environ.get("DB_PATH", "backrooms.db")
REGISTER_KEY = os.environ.get("REGISTER_KEY", "changeme")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                token      TEXT PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS rooms (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                url            TEXT UNIQUE NOT NULL,
                name           TEXT,
                description    TEXT,
                classification TEXT,
                owner_token    TEXT REFERENCES users(token),
                created_at     TEXT NOT NULL DEFAULT (datetime('now')),
                times_entered  INTEGER NOT NULL DEFAULT 0,
                times_exited   INTEGER NOT NULL DEFAULT 0,
                is_featured    INTEGER NOT NULL DEFAULT 0,
                is_active      INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS doors (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                url           TEXT UNIQUE NOT NULL,
                room_id       INTEGER NOT NULL REFERENCES rooms(id),
                game_door_id  TEXT NOT NULL,
                label         TEXT,
                dest_url      TEXT,
                created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                times_entered INTEGER NOT NULL DEFAULT 0,
                times_exited  INTEGER NOT NULL DEFAULT 0,
                UNIQUE (room_id, game_door_id)
            );

            CREATE TABLE IF NOT EXISTS collisions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                exit_door_id     INTEGER REFERENCES doors(id),
                entrance_door_id INTEGER REFERENCES doors(id),
                exit_room_id     INTEGER NOT NULL REFERENCES rooms(id),
                entrance_room_id INTEGER NOT NULL REFERENCES rooms(id),
                user_token       TEXT NOT NULL REFERENCES users(token),
                time_collided    TEXT NOT NULL DEFAULT (datetime('now')),
                times_traversed  INTEGER NOT NULL DEFAULT 1
            );

            CREATE UNIQUE INDEX IF NOT EXISTS uq_collision_door
                ON collisions(user_token, exit_door_id) WHERE exit_door_id IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS uq_collision_nodoor
                ON collisions(user_token, exit_room_id) WHERE exit_door_id IS NULL;
        """)


init_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def require_register_key(x_api_key: Optional[str] = Header(default=None)):
    if x_api_key != REGISTER_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing X-API-Key header")


def ensure_user(token: str, conn: sqlite3.Connection):
    conn.execute("INSERT OR IGNORE INTO users (token) VALUES (?)", (token,))


def _lookup_door(room_id: int, game_door_id: str, conn: sqlite3.Connection) -> Optional[int]:
    """Return a door's id if it exists in the registry, otherwise None."""
    row = conn.execute(
        "SELECT id FROM doors WHERE room_id = ? AND game_door_id = ?",
        (room_id, game_door_id),
    ).fetchone()
    return row["id"] if row else None


def _pick_fresh_destination(
    token: str,
    exclude_room_id: int,
    conn: sqlite3.Connection,
    max_tries: int = 5,
) -> tuple:
    """
    Find a destination (entrance_room_id, entrance_door_id, redirect_url) for a new collision.

    Strategy:
      - Try up to max_tries random rooms; for each, look for a door that this user
        hasn't collided with yet (not already an entrance_door_id for this user).
      - If a fresh door is found, return it.
      - After max_tries with no fresh door, fall back to the already-collided
        (entrance_room, entrance_door) with the lowest times_traversed.
      - Last resort: any active room other than the source.
    """
    used_entrance_ids = {
        r["entrance_door_id"]
        for r in conn.execute(
            "SELECT entrance_door_id FROM collisions WHERE user_token = ? AND entrance_door_id IS NOT NULL",
            (token,),
        ).fetchall()
    }

    tried_ids = {exclude_room_id}

    for _ in range(max_tries):
        ph = ",".join("?" * len(tried_ids))
        room = conn.execute(
            f"SELECT id, url FROM rooms WHERE is_active = 1 AND id NOT IN ({ph}) ORDER BY RANDOM() LIMIT 1",
            list(tried_ids),
        ).fetchone()
        if not room:
            break
        tried_ids.add(room["id"])

        doors = conn.execute(
            "SELECT id, dest_url FROM doors WHERE room_id = ?", (room["id"],)
        ).fetchall()
        fresh = [d for d in doors if d["id"] not in used_entrance_ids]
        if fresh:
            chosen = random.choice(fresh)
            return room["id"], chosen["id"], chosen["dest_url"] or room["url"]

    # Fallback: already-collided entrance with lowest times_traversed
    row = conn.execute(
        """SELECT c.entrance_room_id, c.entrance_door_id, d.dest_url, r.url AS room_url
           FROM collisions c
           JOIN rooms r ON r.id = c.entrance_room_id
           LEFT JOIN doors d ON d.id = c.entrance_door_id
           WHERE c.user_token = ? AND c.entrance_room_id != ?
           ORDER BY c.times_traversed ASC
           LIMIT 1""",
        (token, exclude_room_id),
    ).fetchone()
    if row:
        return row["entrance_room_id"], row["entrance_door_id"], row["dest_url"] or row["room_url"]

    # Last resort: any active room other than source (enter-only, no door)
    room = conn.execute(
        "SELECT id, url FROM rooms WHERE is_active = 1 AND id != ? ORDER BY RANDOM() LIMIT 1",
        (exclude_room_id,),
    ).fetchone()
    if not room:
        raise HTTPException(status_code=503, detail="No rooms available.")
    return room["id"], None, room["url"]


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class RegisterURLRequest(BaseModel):
    url: str
    name: Optional[str] = None
    description: Optional[str] = None


class SubmitRoomRequest(BaseModel):
    token: str
    url: str
    name: Optional[str] = None
    description: Optional[str] = None
    classification: Optional[str] = None


class DoorResolveRequest(BaseModel):
    token: str
    source: Optional[str] = None
    game_door_id: Optional[str] = None


class RoomDoorRequest(BaseModel):
    token: str
    room_url: str
    game_door_id: str
    label: Optional[str] = None
    dest_url: Optional[str] = None


class RoomDoorUpdateRequest(BaseModel):
    token: str
    game_door_id: Optional[str] = None
    label: Optional[str] = None
    dest_url: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/door")
def door(request: Request):
    """Browser entry point — redirects to go.html with query params intact."""
    qs = request.url.query
    return RedirectResponse(url=f"/go.html?{qs}" if qs else "/go.html", status_code=302)


@app.post("/door/resolve")
def door_resolve(body: DoorResolveRequest):
    """
    Resolves a door transition.

    - Token only (no source/game_door_id): picks a random room, increments its
      times_entered, and returns its URL. No collision is recorded.
    - Token + source + game_door_id: looks up the existing collision; if found,
      increments stats and returns the cached destination. If not, picks a
      fresh destination room+door, records the collision, and returns it.
    """
    with get_db() as conn:
        ensure_user(body.token, conn)

        if body.source is None or body.game_door_id is None:
            room = conn.execute(
                "SELECT id, url FROM rooms WHERE is_active = 1 ORDER BY RANDOM() LIMIT 1"
            ).fetchone()
            if not room:
                raise HTTPException(status_code=503, detail="No rooms available.")
            conn.execute("UPDATE rooms SET times_entered = times_entered + 1 WHERE id = ?", (room["id"],))
            return {"destination": room["url"]}

        source: str = body.source

        source_room = conn.execute(
            "SELECT id FROM rooms WHERE url = ? AND is_active = 1", (source,)
        ).fetchone()
        if not source_room:
            raise HTTPException(status_code=404, detail="Source room not registered.")
        source_room_id: int = source_room["id"]

        exit_door_id = _lookup_door(source_room_id, body.game_door_id, conn)
        if exit_door_id is None:
            raise HTTPException(status_code=404, detail="Door not registered for this room.")

        existing = conn.execute(
            """SELECT c.id, c.entrance_room_id, c.entrance_door_id, c.times_traversed,
                      d.dest_url, r.url AS room_url
               FROM collisions c
               JOIN rooms r ON r.id = c.entrance_room_id
               LEFT JOIN doors d ON d.id = c.entrance_door_id
               WHERE c.user_token = ? AND c.exit_door_id = ?""",
            (body.token, exit_door_id),
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE collisions SET times_traversed = times_traversed + 1 WHERE id = ?",
                (existing["id"],),
            )
            conn.execute("UPDATE rooms SET times_exited  = times_exited  + 1 WHERE id = ?", (source_room_id,))
            conn.execute("UPDATE rooms SET times_entered = times_entered + 1 WHERE id = ?", (existing["entrance_room_id"],))
            conn.execute("UPDATE doors SET times_exited  = times_exited  + 1 WHERE id = ?", (exit_door_id,))
            if existing["entrance_door_id"]:
                conn.execute("UPDATE doors SET times_entered = times_entered + 1 WHERE id = ?", (existing["entrance_door_id"],))
            destination = existing["dest_url"] or existing["room_url"]
        else:
            dest_room_id, entrance_door_id, destination = _pick_fresh_destination(
                body.token, source_room_id, conn
            )
            conn.execute(
                """INSERT INTO collisions
                       (user_token, exit_door_id, entrance_door_id, exit_room_id, entrance_room_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (body.token, exit_door_id, entrance_door_id, source_room_id, dest_room_id),
            )
            conn.execute(
                """INSERT OR IGNORE INTO collisions
                       (user_token, exit_door_id, entrance_door_id, exit_room_id, entrance_room_id)
                   VALUES (?, ?, ?, ?, ?)""",
                (body.token, entrance_door_id, exit_door_id, dest_room_id, source_room_id),
            )
            conn.execute("UPDATE rooms SET times_exited  = times_exited  + 1 WHERE id = ?", (source_room_id,))
            conn.execute("UPDATE rooms SET times_entered = times_entered + 1 WHERE id = ?", (dest_room_id,))
            conn.execute("UPDATE doors SET times_exited  = times_exited  + 1 WHERE id = ?", (exit_door_id,))
            if entrance_door_id:
                conn.execute("UPDATE doors SET times_entered = times_entered + 1 WHERE id = ?", (entrance_door_id,))

    return {"destination": destination}


@app.post("/rooms", status_code=201)
def submit_room(body: SubmitRoomRequest):
    """Public endpoint — any player can submit a room using their token as owner."""
    ALLOWED = {"exploration", "puzzle", "horror", "ambient", "liminal", "other"}
    classification = body.classification.lower() if body.classification else "other"
    if classification not in ALLOWED:
        raise HTTPException(status_code=422, detail=f"classification must be one of: {', '.join(sorted(ALLOWED))}")

    with get_db() as conn:
        ensure_user(body.token, conn)

        existing = conn.execute(
            "SELECT id, is_active FROM rooms WHERE url = ?", (body.url,)
        ).fetchone()

        if existing:
            if existing["is_active"]:
                raise HTTPException(status_code=409, detail="This URL is already registered.")
            conn.execute(
                """UPDATE rooms SET is_active = 1, name = ?, description = ?, classification = ?, owner_token = ?
                   WHERE url = ?""",
                (body.name, body.description, classification, body.token, body.url),
            )
            return {"status": "reactivated", "url": body.url}

        conn.execute(
            "INSERT INTO rooms (url, name, description, classification, owner_token) VALUES (?, ?, ?, ?, ?)",
            (body.url, body.name, body.description, classification, body.token),
        )
    return {"status": "registered", "url": body.url}


@app.post("/register", dependencies=[Depends(require_register_key)], status_code=201)
def register_url(body: RegisterURLRequest):
    """Admin endpoint — register a room by URL. Requires X-API-Key header."""
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id, is_active FROM rooms WHERE url = ?", (body.url,)
        ).fetchone()

        if existing:
            if existing["is_active"]:
                raise HTTPException(status_code=409, detail="URL already registered and active.")
            conn.execute(
                "UPDATE rooms SET is_active = 1, name = ?, description = ? WHERE url = ?",
                (body.name, body.description, body.url),
            )
            return {"status": "reactivated", "url": body.url}

        conn.execute(
            "INSERT INTO rooms (url, name, description) VALUES (?, ?, ?)",
            (body.url, body.name, body.description),
        )
    return {"status": "registered", "url": body.url}


@app.delete("/register")
def deregister_url(
    url: str = Query(...),
    token: Optional[str] = Query(default=None),
    x_api_key: Optional[str] = Header(default=None),
):
    """Deactivate a room. Caller must be the owner or hold the admin API key."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT owner_token FROM rooms WHERE url = ? AND is_active = 1", (url,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="URL not found.")

        if x_api_key != REGISTER_KEY and (not token or token != row["owner_token"]):
            raise HTTPException(status_code=403, detail="You do not own this room.")

        conn.execute("UPDATE rooms SET is_active = 0 WHERE url = ?", (url,))

    return {"status": "deactivated", "url": url}


@app.post("/rooms/featured")
def set_featured(url: str = Query(...), x_api_key: Optional[str] = Header(default=None)):
    """Admin endpoint — mark a room as featured."""
    if x_api_key != REGISTER_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing X-API-Key header")
    with get_db() as conn:
        result = conn.execute(
            "UPDATE rooms SET is_featured = 1 WHERE url = ? AND is_active = 1", (url,)
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Room not found.")
    return {"status": "featured", "url": url}


@app.delete("/rooms/featured")
def unset_featured(url: str = Query(...), x_api_key: Optional[str] = Header(default=None)):
    """Admin endpoint — remove featured status from a room."""
    if x_api_key != REGISTER_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing X-API-Key header")
    with get_db() as conn:
        result = conn.execute(
            "UPDATE rooms SET is_featured = 0 WHERE url = ? AND is_active = 1", (url,)
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Room not found.")
    return {"status": "unfeatured", "url": url}


@app.get("/doors")
def list_rooms_by_owner(token: str = Query(...)):
    """List active rooms owned by the given token."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT url, name, description, classification, created_at,
                      times_entered, times_exited, is_featured
               FROM rooms WHERE is_active = 1 AND owner_token = ? ORDER BY created_at""",
            (token,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/rooms/doors")
def list_room_doors(room_url: str = Query(...)):
    """List all doors defined for a given room URL."""
    with get_db() as conn:
        room = conn.execute("SELECT id FROM rooms WHERE url = ?", (room_url,)).fetchone()
        if not room:
            raise HTTPException(status_code=404, detail="Room not found.")
        rows = conn.execute(
            """SELECT id, url, game_door_id, label, dest_url, created_at,
                      times_entered, times_exited
               FROM doors WHERE room_id = ? ORDER BY id""",
            (room["id"],),
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/rooms/doors", status_code=201)
def add_room_door(body: RoomDoorRequest):
    """Add a door to a room. Caller must be the room's owner."""
    with get_db() as conn:
        room = conn.execute(
            "SELECT id, owner_token FROM rooms WHERE url = ? AND is_active = 1", (body.room_url,)
        ).fetchone()
        if not room:
            raise HTTPException(status_code=404, detail="Room not found.")
        if room["owner_token"] != body.token:
            raise HTTPException(status_code=403, detail="You do not own this room.")

        door_url = body.dest_url or f"{body.room_url}?bz_door={body.game_door_id}"
        try:
            cur = conn.execute(
                "INSERT INTO doors (url, room_id, game_door_id, label, dest_url) VALUES (?, ?, ?, ?, ?)",
                (door_url, room["id"], body.game_door_id, body.label, body.dest_url),
            )
        except Exception:
            raise HTTPException(status_code=409, detail="A door with that game_door_id already exists for this room.")
        door_id = cur.lastrowid

    return {"id": door_id, "url": door_url, "game_door_id": body.game_door_id,
            "room_url": body.room_url, "label": body.label, "dest_url": body.dest_url}


@app.put("/rooms/doors/{door_id}")
def update_room_door(door_id: int, body: RoomDoorUpdateRequest):
    """Update a door's game_door_id, label, or destination. Caller must own the room."""
    with get_db() as conn:
        door = conn.execute(
            """SELECT d.game_door_id, d.label, d.dest_url, r.owner_token
               FROM doors d JOIN rooms r ON r.id = d.room_id
               WHERE d.id = ?""",
            (door_id,),
        ).fetchone()
        if not door:
            raise HTTPException(status_code=404, detail="Door not found.")
        if door["owner_token"] != body.token:
            raise HTTPException(status_code=403, detail="You do not own this room.")

        new_game_door_id = body.game_door_id if body.game_door_id is not None else door["game_door_id"]
        new_label        = body.label        if body.label        is not None else door["label"]
        new_dest_url     = body.dest_url     if body.dest_url     is not None else door["dest_url"]

        conn.execute(
            "UPDATE doors SET game_door_id = ?, label = ?, dest_url = ? WHERE id = ?",
            (new_game_door_id, new_label, new_dest_url, door_id),
        )
    return {"id": door_id, "game_door_id": new_game_door_id, "label": new_label, "dest_url": new_dest_url}


@app.delete("/rooms/doors/{door_id}")
def delete_room_door(door_id: int, token: str = Query(...)):
    """Delete a door. Caller must own the room."""
    with get_db() as conn:
        door = conn.execute(
            "SELECT r.owner_token FROM doors d JOIN rooms r ON r.id = d.room_id WHERE d.id = ?",
            (door_id,),
        ).fetchone()
        if not door:
            raise HTTPException(status_code=404, detail="Door not found.")
        if door["owner_token"] != token:
            raise HTTPException(status_code=403, detail="You do not own this room.")

        conn.execute("DELETE FROM doors WHERE id = ?", (door_id,))
    return {"status": "deleted", "id": door_id}


@app.get("/stats/{token}")
def user_stats(token: str):
    """Return collision history for a player token."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT
                   c.id,
                   er.url  AS exit_room_url,
                   ed.url  AS exit_door_url,
                   nr.url  AS entrance_room_url,
                   nd.url  AS entrance_door_url,
                   c.time_collided,
                   c.times_traversed
               FROM collisions c
               JOIN rooms er ON er.id = c.exit_room_id
               JOIN rooms nr ON nr.id = c.entrance_room_id
               LEFT JOIN doors ed ON ed.id = c.exit_door_id
               LEFT JOIN doors nd ON nd.id = c.entrance_door_id
               WHERE c.user_token = ?
               ORDER BY c.time_collided""",
            (token,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Static files (must come last)
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory="static", html=True), name="static")

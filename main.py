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
from urllib.parse import urlparse, parse_qs

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
# Database helpers
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

            CREATE TABLE IF NOT EXISTS registered_urls (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                url           TEXT UNIQUE NOT NULL,
                name          TEXT,
                description   TEXT,
                room_type     TEXT,
                owner_token   TEXT,
                registered_at TEXT NOT NULL DEFAULT (datetime('now')),
                is_active     INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS door_visits (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_token      TEXT NOT NULL,
                    source_url      TEXT NOT NULL,
                    door_id         TEXT,
                    destination_url TEXT NOT NULL,
                    visited_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (user_token) REFERENCES users(token)
                );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_dv_door
                ON door_visits(user_token, source_url, door_id) WHERE door_id IS NOT NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS uq_dv_nodoor
                ON door_visits(user_token, source_url) WHERE door_id IS NULL;

            CREATE TABLE IF NOT EXISTS room_doors (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                room_url     TEXT NOT NULL,
                game_door_id TEXT NOT NULL,
                label        TEXT,
                dest_url     TEXT,
                created_at   TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (room_url, game_door_id),
                FOREIGN KEY (room_url) REFERENCES registered_urls(url)
            );
        """)


def migrate_db():
    """No-op placeholder for future migrations."""
    pass


init_db()
migrate_db()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def require_register_key(x_api_key: Optional[str] = Header(default=None)):
    if x_api_key != REGISTER_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing X-API-Key header")


def ensure_user(token: str, conn: sqlite3.Connection):
    """Create user record if it doesn't exist yet."""
    conn.execute(
        "INSERT OR IGNORE INTO users (token) VALUES (?)",
        (token,),
    )


def _pick_random_dest(token: str, source: str, conn: sqlite3.Connection) -> str:
    """
    Pick a random unvisited room URL for a first-time door visit.
    Falls back to any room other than source if all rooms have been visited.
    """
    visited_rows = conn.execute(
        "SELECT destination_url FROM door_visits WHERE user_token = ?",
        (token,),
    ).fetchall()
    visited_bases = {r["destination_url"].split("?")[0].rstrip("/") for r in visited_rows}
    visited_bases.add(source.split("?")[0].rstrip("/"))

    exclude = f"url NOT IN ({','.join('?' * len(visited_bases))})"
    candidates = conn.execute(
        f"SELECT url FROM registered_urls WHERE is_active = 1 AND {exclude}",
        list(visited_bases),
    ).fetchall()

    if not candidates:
        candidates = conn.execute(
            "SELECT url FROM registered_urls WHERE is_active = 1 AND url != ?",
            (source.split("?")[0].rstrip("/"),),
        ).fetchall()

    if not candidates:
        raise HTTPException(status_code=503, detail="No registered rooms available.")

    return random.choice(candidates)["url"]


def _resolve_door_dest(room_url: str, token: str, conn: sqlite3.Connection) -> str:
    """
    Given a room URL, return a door's dest_url that this user hasn't already
    mapped as an exit from that room (i.e. no existing door_visits row with
    source_url=room_url for that game_door_id).  Falls back to any door if all
    are already mapped, or the room URL itself if the room has no doors.

    Filtering out already-mapped doors prevents the reverse INSERT from being
    silently dropped by INSERT OR IGNORE, which would leave the user unable to
    walk back to the room they came from.
    """
    # Doors in this room that already have a mapping for this user
    used_door_ids = {
        r["door_id"]
        for r in conn.execute(
            """SELECT door_id FROM door_visits
               WHERE user_token = ? AND source_url = ? AND door_id IS NOT NULL""",
            (token, room_url),
        ).fetchall()
    }

    rows = conn.execute(
        "SELECT game_door_id, dest_url FROM room_doors WHERE room_url = ? AND dest_url IS NOT NULL",
        (room_url,),
    ).fetchall()

    unused = [r for r in rows if r["game_door_id"] not in used_door_ids]
    candidates = unused if unused else list(rows)

    if candidates:
        return random.choice(candidates)["dest_url"]
    return room_url


def _store_association(token: str, source: str, door_id: Optional[str],
                       destination: str, conn: sqlite3.Connection):
    """
    Store the forward association and its reverse so the player can walk back.

    Forward:  (source, door_id)  → destination
    Reverse:  (dest_base, dest_door_id) → source
    """
    conn.execute(
        """INSERT OR IGNORE INTO door_visits (user_token, source_url, door_id, destination_url)
           VALUES (?, ?, ?, ?)""",
        (token, source, door_id, destination),
    )
    # Parse the destination URL to derive the reverse source + door_id
    parsed = urlparse(destination)
    dest_base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
    qs = parse_qs(parsed.query)
    dest_door_id = next(
        (qs[k][0] for k in ("bz_door", "door", "door_id") if k in qs), None
    )
    conn.execute(
        """INSERT OR IGNORE INTO door_visits (user_token, source_url, door_id, destination_url)
           VALUES (?, ?, ?, ?)""",
        (token, dest_base, dest_door_id, source),
    )


# ---------------------------------------------------------------------------
# Models
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
    room_type: Optional[str] = None


class DoorResolveRequest(BaseModel):
    token: str
    source: Optional[str] = None
    door_id: Optional[str] = None


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
    """
    Browser entry point. Forwards all query params to /go.html so the
    client-side page can attach the localStorage token before resolving.
    """
    qs = request.url.query
    return RedirectResponse(url=f"/go.html?{qs}" if qs else "/go.html", status_code=302)


@app.post("/door/resolve")
def door_resolve(body: DoorResolveRequest):
    """
    Resolves a door transition.

    - Stores the player's exit position for this source door (per door_id if provided).
    - If door_id is given, destination is fixed by the room_doors table.
    - Otherwise, destination is randomly assigned (existing behaviour).
    - Returns the player's last-known exit position FROM the destination room
      so the destination game can place them back where they came from.
    """
    with get_db() as conn:
        ensure_user(body.token, conn)

        if body.source is None:
            # No source — entry from the portal, just pick a random room.
            destination = _pick_random_dest(body.token, "", conn)
            return {"destination": destination}

        # Normalize source: strip query string and trailing slash so lookups
        # are consistent regardless of whether the game appends a trailing slash.
        source: str = body.source.split("?")[0].rstrip("/")

        if body.door_id is not None:
            # ---- door-specific resolution ----
            # door_id is the game's own string identifier (e.g. "door-west-0-7").
            # Look it up via game_door_id so room owners can map it to a fixed destination.
            door_row = conn.execute(
                "SELECT dest_url FROM room_doors WHERE room_url = ? AND game_door_id = ?",
                (source, body.door_id),
            ).fetchone()

            configured_dest = door_row["dest_url"] if door_row else None
            source_base = source.split("?")[0].rstrip("/")
            dest_base = configured_dest.split("?")[0].rstrip("/") if configured_dest else None
            is_self_referential = not configured_dest or dest_base == source_base

            if is_self_referential:
                # ---- per-user symlink: random on first visit, sticky thereafter ----
                existing = conn.execute(
                    """SELECT destination_url FROM door_visits
                       WHERE user_token = ? AND source_url = ? AND door_id = ?""",
                    (body.token, source, body.door_id),
                ).fetchone()

                if existing:
                    destination = existing["destination_url"]
                else:
                    room_url = _pick_random_dest(body.token, source, conn)
                    destination = _resolve_door_dest(room_url, body.token, conn)
                    _store_association(body.token, source, body.door_id, destination, conn)
            else:
                # ---- fixed destination configured by room owner ----
                assert configured_dest is not None
                destination = configured_dest
                _store_association(body.token, source, body.door_id, destination, conn)
        else:
            # ---- doorless random resolution (no door_id provided) ----
            existing = conn.execute(
                """SELECT destination_url FROM door_visits
                   WHERE user_token = ? AND source_url = ? AND door_id IS NULL""",
                (body.token, source),
            ).fetchone()

            if existing:
                destination = existing["destination_url"]
            else:
                destination = _pick_random_dest(body.token, source, conn)
                _store_association(body.token, source, None, destination, conn)

    return {"destination": destination}


@app.post("/rooms", status_code=201)
def submit_room(body: SubmitRoomRequest):
    """Public endpoint — any player can submit a room using their token as owner."""
    ALLOWED_TYPES = {"exploration", "puzzle", "horror", "ambient", "liminal", "other"}
    room_type = body.room_type.lower() if body.room_type else "other"
    if room_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=422, detail=f"room_type must be one of: {', '.join(sorted(ALLOWED_TYPES))}")

    with get_db() as conn:
        ensure_user(body.token, conn)

        existing = conn.execute(
            "SELECT id, is_active FROM registered_urls WHERE url = ?",
            (body.url,),
        ).fetchone()

        if existing:
            if existing["is_active"]:
                raise HTTPException(status_code=409, detail="This URL is already registered.")
            conn.execute(
                """UPDATE registered_urls
                   SET is_active = 1, name = ?, description = ?, room_type = ?, owner_token = ?
                   WHERE url = ?""",
                (body.name, body.description, room_type, body.token, body.url),
            )
            return {"status": "reactivated", "url": body.url}

        conn.execute(
            """INSERT INTO registered_urls (url, name, description, room_type, owner_token)
               VALUES (?, ?, ?, ?, ?)""",
            (body.url, body.name, body.description, room_type, body.token),
        )
    return {"status": "registered", "url": body.url}


@app.post("/register", dependencies=[Depends(require_register_key)], status_code=201)
def register_url(body: RegisterURLRequest):
    """Register a new door URL. Requires X-API-Key header."""
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id, is_active FROM registered_urls WHERE url = ?",
            (body.url,),
        ).fetchone()

        if existing:
            if existing["is_active"]:
                raise HTTPException(status_code=409, detail="URL already registered and active.")
            # Re-activate
            conn.execute(
                "UPDATE registered_urls SET is_active = 1, name = ?, description = ? WHERE url = ?",
                (body.name, body.description, body.url),
            )
            return {"status": "reactivated", "url": body.url}

        conn.execute(
            "INSERT INTO registered_urls (url, name, description) VALUES (?, ?, ?)",
            (body.url, body.name, body.description),
        )
    return {"status": "registered", "url": body.url}


@app.delete("/register")
def deregister_url(
    url: str = Query(...),
    token: Optional[str] = Query(default=None),
    x_api_key: Optional[str] = Header(default=None),
):
    """
    Deactivate a registered door URL.
    Allowed if the caller is the room's owner (matching token) or holds the admin API key.
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT owner_token FROM registered_urls WHERE url = ? AND is_active = 1",
            (url,),
        ).fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="URL not found.")

        is_admin = x_api_key == REGISTER_KEY
        is_owner = token and token == row["owner_token"]

        if not is_admin and not is_owner:
            raise HTTPException(status_code=403, detail="You do not own this room.")

        conn.execute("UPDATE registered_urls SET is_active = 0 WHERE url = ?", (url,))

    return {"status": "deactivated", "url": url}


@app.get("/doors")
def list_doors(token: str = Query(...)):
    """List active registered door URLs owned by the given token."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT url, name, description, room_type, registered_at
               FROM registered_urls
               WHERE is_active = 1 AND owner_token = ?
               ORDER BY registered_at""",
            (token,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/rooms/doors")
def list_room_doors(room_url: str = Query(...)):
    """List all doors defined for a given room URL."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, game_door_id, label, dest_url, created_at
               FROM room_doors WHERE room_url = ? ORDER BY id""",
            (room_url,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/rooms/doors", status_code=201)
def add_room_door(body: RoomDoorRequest):
    """Add a door to a room. Caller must be the room's owner."""
    with get_db() as conn:
        room = conn.execute(
            "SELECT owner_token FROM registered_urls WHERE url = ? AND is_active = 1",
            (body.room_url,),
        ).fetchone()
        if not room:
            raise HTTPException(status_code=404, detail="Room not found.")
        if room["owner_token"] != body.token:
            raise HTTPException(status_code=403, detail="You do not own this room.")

        try:
            cur = conn.execute(
                "INSERT INTO room_doors (room_url, game_door_id, label, dest_url) VALUES (?, ?, ?, ?)",
                (body.room_url, body.game_door_id, body.label, body.dest_url),
            )
        except Exception:
            raise HTTPException(status_code=409, detail="A door with that Game Door ID already exists for this room.")
        door_id = cur.lastrowid
    return {"id": door_id, "game_door_id": body.game_door_id, "room_url": body.room_url, "label": body.label, "dest_url": body.dest_url}


@app.put("/rooms/doors/{door_id}")
def update_room_door(door_id: int, body: RoomDoorUpdateRequest):
    """Update a door's game_door_id, label, or destination. Caller must own the room."""
    with get_db() as conn:
        door = conn.execute(
            """SELECT rd.room_url, rd.game_door_id, rd.label, rd.dest_url, ru.owner_token
               FROM room_doors rd
               JOIN registered_urls ru ON ru.url = rd.room_url
               WHERE rd.id = ?""",
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
            "UPDATE room_doors SET game_door_id=?, label=?, dest_url=? WHERE id=?",
            (new_game_door_id, new_label, new_dest_url, door_id),
        )
    return {"id": door_id, "game_door_id": new_game_door_id, "label": new_label, "dest_url": new_dest_url}


@app.delete("/rooms/doors/{door_id}")
def delete_room_door(door_id: int, token: str = Query(...)):
    """Delete a door. Caller must own the room."""
    with get_db() as conn:
        door = conn.execute(
            """SELECT rd.room_url, ru.owner_token
               FROM room_doors rd
               JOIN registered_urls ru ON ru.url = rd.room_url
               WHERE rd.id = ?""",
            (door_id,),
        ).fetchone()
        if not door:
            raise HTTPException(status_code=404, detail="Door not found.")
        if door["owner_token"] != token:
            raise HTTPException(status_code=403, detail="You do not own this room.")

        conn.execute("DELETE FROM room_doors WHERE id = ?", (door_id,))
    return {"status": "deleted", "id": door_id}


@app.get("/stats/{token}")
def user_stats(token: str):
    """Return visit history for a player token."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT source_url, door_id, destination_url, visited_at
               FROM door_visits WHERE user_token = ? ORDER BY visited_at""",
            (token,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Static files (must come last so API routes take priority)
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory="static", html=True), name="static")

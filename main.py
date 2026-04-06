from fastapi import FastAPI, Query, Header, HTTPException, Depends, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, JSONResponse
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
                destination_url TEXT NOT NULL,
                exit_x          REAL,
                exit_y          REAL,
                exit_z          REAL,
                exit_nx         REAL,
                exit_ny         REAL,
                exit_nz         REAL,
                visited_at      TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (user_token, source_url),
                FOREIGN KEY (user_token) REFERENCES users(token)
            );
        """)
        # Non-destructive migrations for existing databases
        migrations = [
            ("registered_urls", "room_type",  "TEXT"),
            ("registered_urls", "owner_token", "TEXT"),
            ("door_visits",     "exit_x",  "REAL"),
            ("door_visits",     "exit_y",  "REAL"),
            ("door_visits",     "exit_z",  "REAL"),
            ("door_visits",     "exit_nx", "REAL"),
            ("door_visits",     "exit_ny", "REAL"),
            ("door_visits",     "exit_nz", "REAL"),
        ]
        for table, col, definition in migrations:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
            except Exception:
                pass


init_db()


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
    source: str
    x:  Optional[float] = None
    y:  Optional[float] = None
    z:  Optional[float] = None
    nx: Optional[float] = None
    ny: Optional[float] = None
    nz: Optional[float] = None


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

    - Stores the player's exit position for this source door.
    - Returns the assigned destination URL.
    - Returns the player's last-known exit position FROM the destination room
      (so the destination game can place them back where they came from).
    """
    with get_db() as conn:
        ensure_user(body.token, conn)

        existing = conn.execute(
            """SELECT destination_url FROM door_visits
               WHERE user_token = ? AND source_url = ?""",
            (body.token, body.source),
        ).fetchone()

        if existing:
            destination = existing["destination_url"]
            # Update exit position for this door use
            conn.execute(
                """UPDATE door_visits
                   SET exit_x=?, exit_y=?, exit_z=?, exit_nx=?, exit_ny=?, exit_nz=?,
                       visited_at=datetime('now')
                   WHERE user_token=? AND source_url=?""",
                (body.x, body.y, body.z, body.nx, body.ny, body.nz,
                 body.token, body.source),
            )
        else:
            # Pick an unvisited destination
            visited = conn.execute(
                "SELECT destination_url FROM door_visits WHERE user_token = ?",
                (body.token,),
            ).fetchall()
            visited_urls = {r["destination_url"] for r in visited} | {body.source}

            candidates = conn.execute(
                "SELECT url FROM registered_urls WHERE is_active = 1 AND url NOT IN ({})".format(
                    ",".join("?" * len(visited_urls))
                ),
                list(visited_urls),
            ).fetchall()

            if not candidates:
                candidates = conn.execute(
                    "SELECT url FROM registered_urls WHERE is_active = 1 AND url != ?",
                    (body.source,),
                ).fetchall()

            if not candidates:
                raise HTTPException(status_code=503, detail="No registered doors available.")

            destination = random.choice(candidates)["url"]
            conn.execute(
                """INSERT INTO door_visits
                   (user_token, source_url, destination_url, exit_x, exit_y, exit_z, exit_nx, exit_ny, exit_nz)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (body.token, body.source, destination,
                 body.x, body.y, body.z, body.nx, body.ny, body.nz),
            )

        # Look up exit position from the destination room (for the return trip)
        reverse = conn.execute(
            """SELECT exit_x, exit_y, exit_z, exit_nx, exit_ny, exit_nz
               FROM door_visits
               WHERE user_token = ? AND source_url = ?""",
            (body.token, destination),
        ).fetchone()

    return_position = None
    if reverse and reverse["exit_x"] is not None:
        return_position = {
            "x":  reverse["exit_x"],  "y":  reverse["exit_y"],  "z":  reverse["exit_z"],
            "nx": reverse["exit_nx"], "ny": reverse["exit_ny"], "nz": reverse["exit_nz"],
        }

    return {"destination": destination, "return_position": return_position}


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


@app.get("/stats/{token}")
def user_stats(token: str):
    """Return visit history for a player token."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT source_url, destination_url, location_data, visited_at
               FROM door_visits WHERE user_token = ? ORDER BY visited_at""",
            (token,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Static files (must come last so API routes take priority)
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory="static", html=True), name="static")

# BACKROOMS.ZONE — Door Registry

> *Best viewed in Netscape Navigator 3.0 at 640×480*

---

## What is this?

We found it on an old backup tape. A ZIP file inside a ZIP file inside a folder called `FINAL_FINAL_v3_USE_THIS`. The timestamp said 1996. Nobody remembers writing it.

**Backrooms Zone** is a door registry for games set in the Backrooms. Developers register their game's exit URLs here. When a player steps through a door, the registry assigns them a destination they haven't visited yet — and remembers it forever. The same door always goes the same place. There's no going back.

At least, that's what the original README.TXT said. We're still piecing things together.

---

## Getting it running again

The original ran on something called "CGI-BIN". We've ported it to Python/FastAPI because the server it ran on is in a landfill somewhere in New Jersey.

### Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip

### Install & run

```bash
# Install dependencies
uv sync

# Start the server
uv run uvicorn main:app --reload
```

Open `http://localhost:8000` in your browser. Try not to use Netscape Navigator.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `DB_PATH` | `backrooms.db` | Path to the SQLite database |
| `REGISTER_KEY` | `changeme` | API key required for the `/register` admin endpoint |

---

## How it works

Each participating game gets a **URL** registered in the registry. When a player walks through a door in-game, the game calls `/door/resolve` with the player's unique token and their current room's URL. The registry returns a destination URL the player hasn't visited yet — and from that point on, that door always leads to the same place.

Players get a **token** stored in `localStorage` — unique to their machine, persistent across sessions. Their entire path through the backrooms is recorded.

### API overview

| Endpoint | Method | Description |
|---|---|---|
| `/door` | GET | Browser entry point — redirects to the door resolution page |
| `/door/resolve` | POST | Resolve a door transition for a player token |
| `/rooms` | POST | Register a new room (public, token-authenticated) |
| `/register` | POST | Register a URL (admin only, requires `X-API-Key`) |
| `/register` | DELETE | Deactivate a URL (owner token or admin key) |
| `/doors` | GET | List active rooms owned by a token |
| `/api/docs` | GET | Auto-generated API docs (FastAPI/Swagger) |

---

## Registering a room

Any game can register its exit door via the public endpoint:

```bash
curl -X POST https://backrooms.zone/rooms \
  -H "Content-Type: application/json" \
  -d '{
    "token": "your-player-token",
    "url": "https://your-game.example.com/",
    "name": "The Yellow Room",
    "description": "Humid. Fluorescent. The hum never stops.",
    "room_type": "liminal"
  }'
```

Valid `room_type` values: `exploration`, `puzzle`, `horror`, `ambient`, `liminal`, `other`

---

## Deployment

The project includes a `render.yaml` for deployment on [Render](https://render.com). Set `DB_PATH` and `REGISTER_KEY` as environment variables in your service config.

---

## Project structure

```
backrooms.zone/
├── main.py          # FastAPI app — all routes and DB logic
├── pyproject.toml   # Dependencies
├── render.yaml      # Render deployment config
├── backrooms.db     # SQLite database (created on first run)
└── static/
    ├── index.html        # The registry homepage (circa 1996)
    ├── go.html           # Door transition page
    └── found-a-room.html # Room submission form
```

---

## Contributing

If you find a door that doesn't lead anywhere, open an issue. If you find a door that leads somewhere it shouldn't, close it quietly and don't tell anyone.

---

*&copy; 1996&ndash;present Backrooms Zone. All doors reserved.*

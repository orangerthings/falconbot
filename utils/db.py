import aiosqlite
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = str(Path(__file__).resolve().parent.parent / "servers.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    server_id           INTEGER PRIMARY KEY,
    reminder_channel_id INTEGER NOT NULL,
    message             TEXT NOT NULL,
    timezone            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gsheets_configs (
    server_id          INTEGER PRIMARY KEY REFERENCES servers(server_id) ON DELETE CASCADE,
    url                TEXT NOT NULL,
    sheet              TEXT NOT NULL,
    attendee_rows      TEXT NOT NULL,
    start_col          TEXT NOT NULL,
    date_row           INTEGER NOT NULL,
    time_row           INTEGER NOT NULL,
    user_col           TEXT NOT NULL,
    user_col_last_name TEXT,
    meeting_name_row   INTEGER
);

CREATE TABLE IF NOT EXISTS reminder_times_before (
    server_id   INTEGER NOT NULL REFERENCES servers(server_id) ON DELETE CASCADE,
    minutes     INTEGER NOT NULL,
    PRIMARY KEY (server_id, minutes)
);

CREATE TABLE IF NOT EXISTS events (
    server_id   INTEGER NOT NULL REFERENCES servers(server_id) ON DELETE CASCADE,
    event_id    TEXT NOT NULL,
    name        TEXT NOT NULL,
    start_time  INTEGER NOT NULL,
    end_time    INTEGER,
    PRIMARY KEY (server_id, event_id)
);

CREATE TABLE IF NOT EXISTS attendees (
    attendee_id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id    INTEGER NOT NULL REFERENCES servers(server_id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    discord_id  INTEGER,
    UNIQUE (server_id, name)
);

CREATE TABLE IF NOT EXISTS attendance (
    server_id    INTEGER NOT NULL,
    event_id    TEXT NOT NULL,
    attendee_id INTEGER NOT NULL REFERENCES attendees(attendee_id) ON DELETE CASCADE,
    status      TEXT NOT NULL,
    PRIMARY KEY (server_id, event_id, attendee_id),
    FOREIGN KEY (server_id, event_id) REFERENCES events(server_id, event_id) ON DELETE CASCADE
);
"""

_db: aiosqlite.Connection | None = None

async def init() -> None:
    global _db
    _db = await aiosqlite.connect(DB_PATH)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA foreign_keys = ON")
    await _db.executescript(SCHEMA)
    async with _db.execute("PRAGMA table_info(events)") as cur:
        event_columns = {row["name"] for row in await cur.fetchall()}
    if "end_time" not in event_columns:
        await _db.execute("ALTER TABLE events ADD COLUMN end_time INTEGER")
    await _db.commit()

async def close() -> None:
    if _db:
        await _db.close()

async def get_server(server_id: int) -> dict | None:
    async with _db.execute("SELECT * FROM servers WHERE server_id = ?", (server_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    async with _db.execute(
        "SELECT minutes FROM reminder_times_before WHERE server_id = ? ORDER BY minutes DESC", (server_id,)
    ) as cur:
        offsets = [r["minutes"] for r in await cur.fetchall()]
    return {**dict(row), "times_before": offsets, "reminder_channel_id": row["reminder_channel_id"]}


async def create_server(server_id: int, reminder_channel_id: int, message: str, timezone: str, times_before: list[int]) -> None:
    try:
        await _db.execute(
            "INSERT INTO servers (server_id, reminder_channel_id, message, timezone) VALUES (?, ?, ?, ?)",
            (server_id, reminder_channel_id, message, timezone),
        )
        await _db.executemany(
            "INSERT INTO reminder_times_before (server_id, minutes) VALUES (?, ?)",
            [(server_id, m) for m in dict.fromkeys(times_before)],
        )
        await _db.commit()
    except Exception:
        await _db.rollback()
        raise

async def update_server_setting(server_id: int, field: str, value) -> bool:
    if field not in {"reminder_channel_id", "message", "timezone"}:
        raise ValueError(f"Unsupported server setting: {field}")
    cur = await _db.execute(f"UPDATE servers SET {field} = ? WHERE server_id = ?", (value, server_id))
    await _db.commit()
    return cur.rowcount > 0

async def get_servers() -> list[dict]:
    async with _db.execute("SELECT * FROM servers") as cur:
        rows = await cur.fetchall()
    return [await get_server(row["server_id"]) for row in rows]

async def get_reminder_times() -> list[aiosqlite.Row]:
    async with _db.execute("SELECT * FROM reminder_times_before") as cur:
        rows = await cur.fetchall()
    return rows

SHEET_CONFIG_REQUIRED = ("url", "sheet", "attendee_rows", "start_col", "date_row", "time_row", "user_col")
SHEET_CONFIG_OPTIONAL = ("user_col_last_name","meeting_name_row")
SHEET_CONFIG_FIELDS = SHEET_CONFIG_REQUIRED + SHEET_CONFIG_OPTIONAL
async def save_gsheet_config(server_id: int, **kwargs) -> None:
    unknown = set(kwargs) - set(SHEET_CONFIG_FIELDS)
    if unknown:
        raise TypeError(f"Unknown gsheet config fields: {sorted(unknown)}")
    missing = [f for f in SHEET_CONFIG_REQUIRED if kwargs.get(f) is None]
    if missing:
        raise TypeError(f"Missing gsheet config fields: {missing}")

    values = {f: kwargs.get(f) for f in SHEET_CONFIG_FIELDS}
    await _db.execute(
        f"""
        INSERT INTO gsheets_configs (server_id, {", ".join(SHEET_CONFIG_FIELDS)}) VALUES (?, {", ".join("?" for _ in SHEET_CONFIG_FIELDS)})
        ON CONFLICT(server_id) DO UPDATE SET {", ".join(f"{f} = excluded.{f}" for f in SHEET_CONFIG_FIELDS)}
        """,
        (server_id, *values.values()),
    )
    await _db.commit()

async def get_sheet_config(server_id: int) -> dict | None:
    async with _db.execute("SELECT * FROM gsheets_configs WHERE server_id = ?", (server_id,)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None

async def save_events(server_id: int, events: dict[str, tuple[str, datetime, datetime]], attendance: list[tuple[str, str, str]]) -> None:
    rows = {(eid, name): status for eid, name, status in attendance}

    async with _db.execute("SELECT event_id FROM events WHERE server_id = ?", (server_id,)) as cur:
        known = {r["event_id"] for r in await cur.fetchall()} | set(events)
    missing = {eid for eid, _ in rows} - known
    if missing:
        raise ValueError(f"Unknown event ids: {sorted(missing)}")

    try:
        await _db.executemany(
            """
            INSERT INTO events (server_id, event_id, name, start_time, end_time) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(server_id, event_id) DO UPDATE SET name = excluded.name, start_time = excluded.start_time, end_time = excluded.end_time
            """,
            [(server_id, eid, name, int(start.timestamp()), int(end.timestamp()))
             for eid, (name, start, end) in events.items()],
        )

        sheet_event_prefix = f"gs-{server_id}-"
        stale_events = known - set(events)
        await _db.executemany(
            "DELETE FROM events WHERE server_id = ? AND event_id = ?",
            [(server_id, eid) for eid in stale_events if eid.startswith(sheet_event_prefix)],
        )

        await _db.executemany(
            "INSERT OR IGNORE INTO attendees (server_id, name) VALUES (?, ?)",
            [(server_id, n) for n in {name for _, name in rows}],
        )
        async with _db.execute(
            "SELECT attendee_id, name FROM attendees WHERE server_id = ?", (server_id,)
        ) as cur:
            ids = {r["name"]: r["attendee_id"] for r in await cur.fetchall()}

        await _db.executemany(
            "DELETE FROM attendance WHERE server_id = ? AND event_id = ?",
            [(server_id, eid) for eid in events],
        )
        await _db.executemany(
            """
            INSERT INTO attendance (server_id, event_id, attendee_id, status)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(server_id, event_id, attendee_id) DO UPDATE SET status = excluded.status
            """,
            [(server_id, eid, ids[name], status) for (eid, name), status in rows.items()],
        )
        await _db.commit()
    except Exception:
        await _db.rollback()
        raise

async def search_events(server_id: int, current: str = "") -> list[aiosqlite.Row]:
    now = int(datetime.now(timezone.utc).timestamp())
    async with _db.execute(
        """
        SELECT event_id, name, start_time FROM events
        WHERE server_id = ? AND instr(lower(name), lower(?)) > 0
        ORDER BY start_time < ?, start_time
        LIMIT 25
        """,
        (server_id, current, now),
    ) as cur:
        return await cur.fetchall()
    
async def search_attendees(server_id: int, current: str = "") -> list[aiosqlite.Row]:
    async with _db.execute(
        """
        SELECT attendee_id, name FROM attendees
        WHERE server_id = ? AND instr(lower(name), lower(?)) > 0
        ORDER BY name
        LIMIT 25
        """,
        (server_id, current),
    ) as cur:
        return await cur.fetchall()

async def get_events(server_id: int) -> list[aiosqlite.Row]:
    async with _db.execute(
        "SELECT * FROM events WHERE server_id = ? ORDER BY start_time", (server_id,)
    ) as cur:
        return await cur.fetchall()

async def unlink_discord(server_id: int, attendee_id: int) -> bool:
    cur = await _db.execute(
        "UPDATE attendees SET discord_id = NULL WHERE server_id = ? AND attendee_id = ?",
        (server_id, attendee_id),
    )
    await _db.commit()
    return cur.rowcount > 0

async def link_discord(server_id: int, attendee_id: int, discord_id: int) -> bool:
    cur = await _db.execute(
        "UPDATE attendees SET discord_id = ? WHERE server_id = ? AND attendee_id = ?",
        (discord_id, server_id, attendee_id),
    )
    await _db.commit()
    return cur.rowcount > 0

async def get_attendees(server_id: int, event_id: str) -> list[aiosqlite.Row]:
    async with _db.execute(
        """
        SELECT a.attendee_id, a.name, a.discord_id, t.status
        FROM attendance t
        JOIN attendees a ON a.attendee_id = t.attendee_id
        WHERE t.server_id = ? AND t.event_id = ?
        """,
        (server_id, event_id),
    ) as cur:
        return await cur.fetchall()

async def get_event_from_field(server_id: int, field: str, value: str) -> aiosqlite.Row | None:
    async with _db.execute(
        f"SELECT * FROM events WHERE server_id = ? AND {field} = ?",
        (server_id, value),
    ) as cur:
        return await cur.fetchone()

async def get_event(server_id: int, event_id: str) -> aiosqlite.Row | None:
    return await get_event_from_field(server_id, "event_id", event_id)

"""SQLite storage for synced Garmin data."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

DAILY_COLUMNS = [
    "steps", "rhr", "stress_avg", "bb_high", "bb_low", "bb_now",
    "sleep_s", "sleep_score", "deep_s", "rem_s",
    "hrv_last", "hrv_weekly", "hrv_low", "hrv_high", "hrv_status",
    "readiness", "readiness_level",
    "acute_load", "chronic_load", "acwr", "training_status",
    "vo2max_run", "vo2max_bike",
]

ACTIVITY_COLUMNS = [
    "start", "date", "sport", "type_key", "name", "duration_s", "distance_m",
    "avg_hr", "max_hr", "avg_speed", "avg_power", "norm_power", "tss",
    "garmin_load", "aerobic_te", "anaerobic_te", "elevation_gain", "calories",
]

DEFAULT_PROFILE = """# Profil zawodnika

Uzupełnij ten plik — trener (Claude) czyta go przy każdej analizie.

## Cel główny
- Start docelowy: (np. 1/2 IM Gdynia, 2027-08-01)
- Cel czasowy: (np. sub 5:00)

## Inne starty w sezonie
-

## Dostępny czas
- Godzin tygodniowo: (np. 8–10)
- Dni bez treningu / ograniczenia: (np. pon wolne, wt max 1 h)

## Aktualne progi (jeśli znasz)
- FTP rower: W
- Tempo progowe bieg: min/km
- CSS pływanie: /100 m
- Tętno max / progowe:

## Mocne i słabe strony, historia kontuzji
-

## Preferencje
- (np. basen tylko wt/czw, długi rower w sobotę, bieg długi w niedzielę)
"""


def data_dir() -> Path:
    base = os.getenv("GARMIN_COACH_HOME")
    path = Path(base).expanduser() if base else Path.home() / ".garmin-coach"
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "garmin.db"


def body_dir() -> Path:
    path = data_dir() / "body"
    path.mkdir(parents=True, exist_ok=True)
    return path


def profile_path() -> Path:
    path = data_dir() / "athlete.md"
    if not path.exists():
        path.write_text(DEFAULT_PROFILE, encoding="utf-8")
    return path


def read_profile() -> str:
    return profile_path().read_text(encoding="utf-8")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS daily (
            date TEXT PRIMARY KEY,
            {", ".join(c + " " + ("TEXT" if c in ("hrv_status", "readiness_level", "training_status") else "REAL") for c in DAILY_COLUMNS)},
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY,
            {", ".join(c + " " + ("TEXT" if c in ("start", "date", "sport", "type_key", "name") else "REAL") for c in ACTIVITY_COLUMNS)},
            raw TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS plan (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            sport TEXT NOT NULL,
            title TEXT,
            duration_min REAL,
            distance_km REAL,
            intensity TEXT,
            description TEXT,
            status TEXT DEFAULT 'planned',
            source TEXT,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_plan_date ON plan(date);
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            meta TEXT,
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread);
        CREATE TABLE IF NOT EXISTS body_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            device TEXT,
            weight_kg REAL,
            body_fat_pct REAL,
            fat_mass_kg REAL,
            skeletal_muscle_kg REAL,
            lean_mass_kg REAL,
            water_l REAL,
            visceral_fat REAL,
            bmr_kcal REAL,
            bmi REAL,
            extra TEXT,
            notes TEXT,
            files TEXT,
            confidence TEXT,
            verified INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS activity_details (
            id INTEGER PRIMARY KEY,
            fetched_at TEXT,
            data TEXT
        );
        CREATE TABLE IF NOT EXISTS garmin_workouts (
            plan_id INTEGER PRIMARY KEY,
            workout_id INTEGER,
            schedule_id INTEGER,
            date TEXT,
            hash TEXT,
            pushed_at TEXT
        );
        """
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(plan)")}
    if "steps" not in cols:  # added after the first release
        conn.execute("ALTER TABLE plan ADD COLUMN steps TEXT")
    if "name_en" not in cols:
        conn.execute("ALTER TABLE plan ADD COLUMN name_en TEXT")
    return conn


def config_path() -> Path:
    return data_dir() / "config.json"


def read_config() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def write_config(values: dict) -> dict:
    cfg = read_config()
    for k, v in values.items():
        if v is None or v == "":
            cfg.pop(k, None)
        else:
            cfg[k] = v
    config_path().write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


def upsert_daily(conn: sqlite3.Connection, date: str, values: dict, updated_at: str) -> None:
    cols = [c for c in DAILY_COLUMNS if c in values]
    if not cols:
        return
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c}=COALESCE(excluded.{c}, daily.{c})" for c in cols)
    conn.execute(
        f"INSERT INTO daily (date, {', '.join(cols)}, updated_at) VALUES (?, {placeholders}, ?) "
        f"ON CONFLICT(date) DO UPDATE SET {updates}, updated_at=excluded.updated_at",
        [date, *[values[c] for c in cols], updated_at],
    )


def upsert_activity(conn: sqlite3.Connection, activity_id: int, values: dict, raw: dict) -> None:
    cols = ACTIVITY_COLUMNS
    conn.execute(
        f"INSERT OR REPLACE INTO activities (id, {', '.join(cols)}, raw) "
        f"VALUES (?, {', '.join('?' for _ in cols)}, ?)",
        [activity_id, *[values.get(c) for c in cols], json.dumps(raw)],
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )

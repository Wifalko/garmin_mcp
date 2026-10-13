"""Body composition from photos of analyser printouts (InBody, Tanita, …).

Photos are kept in ~/.garmin-coach/body/, Claude reads the numbers, one row per
measurement day lands in `body_scans`. The athlete can correct any value in the
dashboard; corrected rows are marked verified.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from garmin_mcp.coach import llm, store

FIELDS = ("weight_kg", "body_fat_pct", "fat_mass_kg", "skeletal_muscle_kg", "lean_mass_kg",
          "water_l", "visceral_fat", "bmr_kcal", "bmi")
LABELS = {
    "weight_kg": "Masa ciała [kg]", "body_fat_pct": "Tkanka tłuszczowa [%]", "fat_mass_kg": "Masa tłuszczu [kg]",
    "skeletal_muscle_kg": "Masa mięśni szkieletowych [kg]", "lean_mass_kg": "Beztłuszczowa masa ciała [kg]",
    "water_l": "Woda [l]", "visceral_fat": "Tłuszcz trzewny [poziom]", "bmr_kcal": "Przemiana materii [kcal]",
    "bmi": "BMI",
}
SEGMENTS = ("left_arm", "right_arm", "trunk", "left_leg", "right_leg")
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".pdf"}
MAX_FILES_PER_CALL = 8

EXTRACT_PROMPT = """To są zdjęcia lub skany wyników analizy składu ciała (np. InBody, Tanita, Withings, wydruk z siłowni).
Jeden pomiar może być na kilku zdjęciach (np. dwie strony wydruku) — połącz je w jeden wpis.
Kilka zdjęć może też dotyczyć różnych dni — wtedy zwróć osobne wpisy.

Zwróć WYŁĄCZNIE JSON w tym kształcie:
{
  "measurements": [
    {
      "date": "YYYY-MM-DD" lub null jeśli data nie jest widoczna,
      "device": "np. InBody 570" lub null,
      "source_files": [numery plików, np. 0, 1],
      "weight_kg": liczba lub null,
      "body_fat_pct": liczba lub null,
      "fat_mass_kg": liczba lub null,
      "skeletal_muscle_kg": liczba lub null (SMM / masa mięśni szkieletowych),
      "lean_mass_kg": liczba lub null (FFM / beztłuszczowa masa ciała),
      "water_l": liczba lub null (całkowita woda w organizmie, TBW),
      "visceral_fat": liczba lub null (poziom tłuszczu trzewnego),
      "bmr_kcal": liczba lub null,
      "bmi": liczba lub null,
      "segmental_lean_kg": {"left_arm": n, "right_arm": n, "trunk": n, "left_leg": n, "right_leg": n} lub null,
      "segmental_fat_kg": {...te same klucze...} lub null,
      "other": {"nazwa parametru z wydruku": wartość, ...} (np. kąt fazowy, ECW/TBW, białko, minerały, wynik InBody),
      "confidence": "high" | "medium" | "low",
      "notes": "krótko: co było nieczytelne lub niepewne"
    }
  ],
  "unreadable": [{"file": numer, "reason": "..."}]
}
Zasady: przepisuj tylko liczby, które faktycznie widzisz — nie zgaduj i nie przeliczaj. Masy w kg, woda w litrach.
Daty z wydruku zamień na format YYYY-MM-DD (polskie wydruki mają zwykle DD.MM.RRRR)."""


def _now() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, str):
        v = v.replace(",", ".").strip()
        m = re.search(r"-?\d+(\.\d+)?", v)
        if not m:
            return None
        v = m.group(0)
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _parse_json(text: str) -> dict:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("Claude nie zwrócił danych w formacie JSON.")
    return json.loads(text[start:end + 1])


def save_upload(name: str, data_b64: str) -> Path:
    ext = Path(name).suffix.lower() or ".jpg"
    if ext not in ALLOWED_EXT:
        raise ValueError(f"Nieobsługiwany typ pliku: {name} (dozwolone: zdjęcia JPG/PNG/WEBP lub PDF).")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(name).stem)[:40] or "skan"
    path = store.body_dir() / f"{dt.datetime.now():%Y%m%d-%H%M%S-%f}_{stem}{ext}"
    path.write_bytes(base64.b64decode(data_b64.split(",", 1)[-1]))
    return path


def _clean_segments(seg: Any) -> dict | None:
    if not isinstance(seg, dict):
        return None
    out = {k: _num(seg.get(k)) for k in SEGMENTS}
    return out if any(v is not None for v in out.values()) else None


def upsert_measurement(m: dict, files: list[str], fallback_date: str | None) -> tuple[str, str]:
    """Insert or merge one measurement. Returns (date, 'added'|'merged')."""
    date = m.get("date") or fallback_date
    try:
        date = dt.date.fromisoformat(str(date)[:10]).isoformat()
    except (TypeError, ValueError):
        raise ValueError("Na zdjęciu nie widać daty pomiaru — podaj ją w polu „Data, jeśli nie ma jej na wydruku”.")
    values = {k: _num(m.get(k)) for k in FIELDS}
    extra = {
        "segmental_lean_kg": _clean_segments(m.get("segmental_lean_kg")),
        "segmental_fat_kg": _clean_segments(m.get("segmental_fat_kg")),
        "other": m.get("other") if isinstance(m.get("other"), dict) and m.get("other") else None,
    }
    extra = {k: v for k, v in extra.items() if v}

    conn = store.connect()
    try:
        row = conn.execute("SELECT * FROM body_scans WHERE date=?", (date,)).fetchone()
        if row:
            merged = {k: (values[k] if values[k] is not None else row[k]) for k in FIELDS}
            old_extra = json.loads(row["extra"]) if row["extra"] else {}
            old_extra.update(extra)
            old_files = json.loads(row["files"]) if row["files"] else []
            conn.execute(
                f"UPDATE body_scans SET {', '.join(f'{k}=?' for k in FIELDS)}, device=COALESCE(?, device), "
                "extra=?, files=?, notes=?, confidence=?, verified=0, updated_at=? WHERE id=?",
                [*merged.values(), m.get("device"), json.dumps(old_extra, ensure_ascii=False),
                 json.dumps(old_files + files), m.get("notes") or row["notes"], m.get("confidence"),
                 _now(), row["id"]])
            status = "merged"
        else:
            conn.execute(
                f"INSERT INTO body_scans (date, device, {', '.join(FIELDS)}, extra, notes, files, confidence, "
                f"created_at, updated_at) VALUES (?, ?, {', '.join('?' for _ in FIELDS)}, ?, ?, ?, ?, ?, ?)",
                [date, m.get("device"), *values.values(), json.dumps(extra, ensure_ascii=False),
                 m.get("notes"), json.dumps(files), m.get("confidence"), _now(), _now()])
            status = "added"
        conn.commit()
    finally:
        conn.close()
    return date, status


def import_photos(uploads: list[dict], fallback_date: str | None = None) -> dict:
    """uploads: [{"name": ..., "data_b64": ...}] -> read by Claude and stored."""
    if not uploads:
        raise ValueError("Nie wybrano żadnych plików.")
    paths = [save_upload(u.get("name") or "skan.jpg", u.get("data_b64") or "") for u in uploads]
    results: list[dict] = []
    unreadable: list[dict] = []
    for i in range(0, len(paths), MAX_FILES_PER_CALL):
        batch = paths[i:i + MAX_FILES_PER_CALL]
        data = _parse_json(llm.read_files(EXTRACT_PROMPT, batch))
        for m in data.get("measurements") or []:
            idx = [j for j in (m.get("source_files") or []) if isinstance(j, int) and 0 <= j < len(batch)]
            files = [batch[j].name for j in idx] or [p.name for p in batch]
            try:
                date, status = upsert_measurement(m, files, fallback_date)
                results.append({"date": date, "status": status, "confidence": m.get("confidence"),
                                "notes": m.get("notes")})
            except ValueError as exc:
                results.append({"date": None, "status": "error", "error": str(exc)})
        for u in data.get("unreadable") or []:
            j = u.get("file")
            name = batch[j].name if isinstance(j, int) and 0 <= j < len(batch) else str(j)
            unreadable.append({"file": name, "reason": u.get("reason")})
    return {"results": results, "unreadable": unreadable, "files": len(paths)}


def list_scans() -> list[dict]:
    conn = store.connect()
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM body_scans ORDER BY date")]
    finally:
        conn.close()
    for r in rows:
        r["extra"] = json.loads(r["extra"]) if r["extra"] else {}
        r["files"] = json.loads(r["files"]) if r["files"] else []
    return rows


def update_scan(scan_id: int, fields: dict) -> dict:
    sets, vals = [], []
    for k in FIELDS:
        if k in fields:
            sets.append(f"{k}=?")
            vals.append(_num(fields[k]))
    if fields.get("date"):
        sets.append("date=?")
        vals.append(dt.date.fromisoformat(str(fields["date"])[:10]).isoformat())
    if "notes" in fields:
        sets.append("notes=?")
        vals.append(fields["notes"] or None)
    if not sets:
        return {"updated": 0}
    conn = store.connect()
    try:
        n = conn.execute(f"UPDATE body_scans SET {', '.join(sets)}, verified=1, updated_at=? WHERE id=?",
                         [*vals, _now(), int(scan_id)]).rowcount
        conn.commit()
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise ValueError("Na ten dzień jest już inny pomiar.") from exc
        raise
    finally:
        conn.close()
    return {"updated": n}


def delete_scan(scan_id: int) -> dict:
    conn = store.connect()
    try:
        n = conn.execute("DELETE FROM body_scans WHERE id=?", (int(scan_id),)).rowcount
        conn.commit()
    finally:
        conn.close()
    return {"deleted": n}  # photos stay in the body/ folder on purpose


def photo_path(scan_id: int, index: int) -> Path | None:
    for r in list_scans():
        if r["id"] == scan_id and 0 <= index < len(r["files"]):
            p = (store.body_dir() / r["files"][index]).resolve()
            if p.parent == store.body_dir().resolve() and p.is_file():
                return p
    return None


# --- analysis ----------------------------------------------------------------

def _at_or_before(rows: list[dict], key: str, date: str) -> dict | None:
    cand = [r for r in rows if r["date"] <= date and r.get(key) is not None]
    return cand[-1] if cand else None


def analyse(rows: list[dict] | None = None) -> dict:
    rows = rows if rows is not None else list_scans()
    if not rows:
        return {"count": 0, "latest": None, "changes": {}, "signals": []}
    latest = rows[-1]
    first = rows[0]
    d_latest = dt.date.fromisoformat(latest["date"])
    four_wk = (d_latest - dt.timedelta(days=28)).isoformat()
    eight_wk = (d_latest - dt.timedelta(days=56)).isoformat()

    changes: dict[str, dict] = {}
    for k in FIELDS:
        cur = _at_or_before(rows, k, latest["date"])
        if not cur:
            continue
        ref4 = _at_or_before(rows, k, four_wk)
        ref_first = next((r for r in rows if r.get(k) is not None), None)
        changes[k] = {
            "current": cur[k], "current_date": cur["date"],
            "vs_4w": round(cur[k] - ref4[k], 2) if ref4 and ref4["date"] != cur["date"] else None,
            "ref_4w_date": ref4["date"] if ref4 else None,
            "vs_first": round(cur[k] - ref_first[k], 2) if ref_first and ref_first["date"] != cur["date"] else None,
            "first_date": ref_first["date"] if ref_first else None,
        }

    signals: list[dict] = []

    def add(level, title, detail):
        signals.append({"level": level, "title": title, "detail": detail})

    # weight loss rate (energy availability)
    w_ref = _at_or_before(rows, "weight_kg", eight_wk) or (first if first.get("weight_kg") else None)
    if latest.get("weight_kg") and w_ref and w_ref["date"] != latest["date"]:
        weeks = max(1.0, (d_latest - dt.date.fromisoformat(w_ref["date"])).days / 7)
        rate = (latest["weight_kg"] - w_ref["weight_kg"]) / w_ref["weight_kg"] * 100 / weeks
        if rate < -0.75:
            add("warn", "Szybki spadek masy ciała",
                f"{rate:.1f}% tygodniowo od {w_ref['date']}. Przy dużej objętości treningu to ryzyko zbyt "
                "małej podaży energii (gorsza regeneracja, odporność, kontuzje). Bezpieczniej ≤ 0,5%/tydz.")
    # muscle trend
    m = changes.get("skeletal_muscle_kg") or changes.get("lean_mass_kg")
    if m and m.get("vs_first") is not None:
        label = "mięśni szkieletowych" if "skeletal_muscle_kg" in changes else "masy beztłuszczowej"
        if m["vs_first"] <= -1.0:
            add("warn", f"Spadek {label}",
                f"{m['vs_first']:+.1f} kg od {m['first_date']}. Może oznaczać za mało białka/energii albo "
                "za mało pracy siłowej przy dużej objętości wytrzymałościowej.")
        elif m["vs_first"] >= 0.5:
            add("ok", f"Przyrost {label}", f"{m['vs_first']:+.1f} kg od {m['first_date']}.")
    # leg asymmetry
    seg_row = next((r for r in reversed(rows) if (r.get("extra") or {}).get("segmental_lean_kg")), None)
    seg = seg_row["extra"]["segmental_lean_kg"] if seg_row else {}
    if seg.get("left_leg") and seg.get("right_leg"):
        l, r = seg["left_leg"], seg["right_leg"]
        asym = abs(l - r) / ((l + r) / 2) * 100
        if asym >= 4:
            add("info", "Asymetria nóg",
                f"Lewa {l:.1f} kg, prawa {r:.1f} kg ({asym:.1f}%, pomiar {seg_row['date']}). Warto dodać pracę jednonóż — "
                "asymetrie zwiększają ryzyko przeciążeń w biegu.")
    vf = latest.get("visceral_fat")
    if vf is not None and vf >= 10:
        add("info", "Podwyższony tłuszcz trzewny", f"Poziom {vf:g} (na wydrukach InBody/Tanita norma zwykle < 10).")

    return {"count": len(rows), "latest": latest, "first_date": first["date"],
            "changes": changes, "signals": signals}


def for_prompt() -> dict | None:
    rows = list_scans()
    if not rows:
        return None
    a = analyse(rows)
    keep = ("date", "device", *FIELDS, "confidence", "verified")
    history = [{k: r.get(k) for k in keep if r.get(k) is not None} for r in rows[-12:]]
    latest_extra = next((r["extra"] for r in reversed(rows) if r.get("extra")), {})
    return {"history_last_12": history, "latest_segmental_and_other": latest_extra,
            "changes": a["changes"], "signals": a["signals"]}

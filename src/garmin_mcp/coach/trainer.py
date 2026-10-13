"""Coach chat: builds the data context, calls Claude, applies plan changes it proposes."""

from __future__ import annotations

import datetime as dt
import json
import re

from garmin_mcp.coach import body, llm, metrics, plan, progress, store, sync

WEEKDAYS = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]
PLAN_BLOCK = re.compile(r"```plan\s*(\{.*?\})\s*```", re.DOTALL)
HISTORY_MESSAGES = 20

INSTRUCTIONS = """Jesteś doświadczonym trenerem triathlonu i prowadzisz tego zawodnika indywidualnie.
Dziś jest {today} ({weekday}).

Jak odpowiadasz:
- Po polsku, konkretnie, jak trener do swojego zawodnika. Najpierw wniosek, potem uzasadnienie liczbami z danych.
- Opierasz się wyłącznie na danych poniżej. Jeśli czegoś brakuje, mówisz czego i co zawodnik może sprawdzić.
- Krótko: zwykle 4–12 zdań lub krótka lista. Markdown: **pogrubienia**, listy "- ", nagłówki "### ".

Analiza treningu (gdy rozmowa dotyczy konkretnej sesji):
- Oceń, czy trening zrealizował swój cel: strefy tętna/mocy, rozkład tempa lub mocy na okrążeniach,
  równomierność, dryf tętna w drugiej połowie, kadencja, w pływaniu SWOLF i tempo na 100 m.
- Porównaj z podobnymi treningami (sekcja porównania) i z sygnałami regeneracji z tamtego dnia.
- Daj 1–3 konkretne rzeczy do poprawy następnym razem, z liczbami (np. "pierwsze 20 min trzymaj ≤ 140 bpm").

Plan treningowy (jest zmienny — dopasowujesz go do formy):
- Rozkład intensywności ok. 80/20, progresja obciążenia do ~10% tygodniowo, co 3–4 tygodnie lżejszy tydzień,
  przed startem docelowym taper. Szanuj dostępny czas, dni i preferencje z profilu zawodnika.
- Gdy HRV/sen/TSB/sygnały pokazują zmęczenie, przesuń akcent i zamień go na tlen lub wolne.
- Każda sesja ma w opisie strukturę: rozgrzewka, część główna z konkretnymi strefami/tętnem/mocą/tempem
  (np. "4×8 min Z4 165–172 bpm, przerwa 3 min Z1"), schłodzenie.

Zmiany w planie zapisujesz blokiem na samym KOŃCU odpowiedzi (dokładnie jeden blok, tylko gdy plan się zmienia):
```plan
{{"upsert": [{{"date": "YYYY-MM-DD", "sport": "swim|bike|run|strength|brick|rest", "title": "krótki tytuł", "duration_min": 60, "distance_km": null, "intensity": "Z2", "description": "rozgrzewka… / część główna… / schłodzenie…"}}], "delete": [12]}}
```
- Zmiana istniejącej sesji: podaj jej "id" z AKTUALNEGO PLANU i tylko zmienione pola. Przeniesienie = nowa "date".
- Nie twórz duplikatów — sprawdź, co już jest zaplanowane na dany dzień. Nie zmieniaj sesji z przeszłości.
- W tekście opisz krótko, co zmieniłeś i dlaczego. Nie przepisuj całego planu do tekstu — zawodnik widzi go w kalendarzu.
- Jeśli profil zawodnika nie ma celu ani dostępnego czasu, zapytaj o to zanim ułożysz plan na dłużej niż tydzień.

Skład ciała (sekcja <sklad_ciala>, jeśli jest — odczytany ze zdjęć wydruków, wartości z confidence "low" traktuj ostrożnie):
- Używaj go do oceny, jakie obciążenia zawodnik znosi: trend masy mięśniowej przy rosnącej objętości,
  tempo zmian masy ciała (spadek > 0,5–0,75%/tydz. przy dużym treningu = ryzyko niedoboru energii i gorszej
  regeneracji), asymetrie segmentowe nóg (ryzyko przeciążeń w biegu → praca jednonóż), nawodnienie.
- Nie zalecaj agresywnej redukcji masy ciała ani restrykcyjnych diet; przy sygnałach niedoboru energii
  (spadek masy + spadek mięśni + słaba regeneracja) priorytetem jest jedzenie wystarczająco dużo i odpoczynek.
- Pomiary bioimpedancją zależą od nawodnienia i pory dnia — oceniaj trendy z kilku pomiarów, nie pojedyncze różnice.
  Nie stawiasz diagnoz medycznych; przy niepokojących wynikach zasugeruj lekarza lub dietetyka sportowego.
"""


def _context(activity_id: int | None) -> str:
    snap = metrics.build_snapshot(days=42)
    compact = metrics.compact_snapshot(snap, 21)
    profile = compact.pop("athlete_profile")
    prog = progress.build_progress()
    for s in prog["sports"]:
        s["weekly"] = [w["value"] for w in s["weekly"]]
    for v in prog["vo2max"].values():
        v.pop("series", None)

    parts = [
        f"<profil_zawodnika>\n{profile}\n</profil_zawodnika>",
        f"<stan_i_historia>\n{json.dumps(compact, ensure_ascii=False, default=str)}\n</stan_i_historia>",
        "<progres>\n" + json.dumps(prog, ensure_ascii=False) + "\n"
        "(run: m/s przy tętnie ref_hr; bike: W przy ref_hr; swim: s/100 m; change_pct > 0 = poprawa)\n</progres>",
        f"<aktualny_plan>\n{json.dumps(plan.plan_for_prompt(), ensure_ascii=False)}\n</aktualny_plan>",
    ]
    bc = body.for_prompt()
    if bc:
        parts.append(f"<sklad_ciala>\n{json.dumps(bc, ensure_ascii=False, default=str)}\n</sklad_ciala>")
    if activity_id:
        parts.append(_activity_context(activity_id))
    return "\n\n".join(parts)


def _activity_context(activity_id: int) -> str:
    conn = store.connect()
    try:
        row = conn.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
        day = conn.execute("SELECT * FROM daily WHERE date=?", (row["date"],)).fetchone() if row else None
    finally:
        conn.close()
    if not row:
        return f"<omawiany_trening>Brak treningu {activity_id} w bazie.</omawiany_trening>"
    act = {k: row[k] for k in row.keys() if k != "raw"}
    try:
        detail = sync.fetch_activity_detail(activity_id)
    except Exception as exc:
        detail = {"error": str(exc)}
    data = {
        "trening": act,
        "szczegoly": detail,
        "porownanie_z_podobnymi": progress.compare_activity(activity_id),
        "regeneracja_tego_dnia": dict(day) if day else None,
    }
    return f"<omawiany_trening>\n{json.dumps(data, ensure_ascii=False, default=str)}\n</omawiany_trening>"


def system_prompt(activity_id: int | None = None) -> str:
    today = dt.date.today()
    head = INSTRUCTIONS.format(today=today.isoformat(), weekday=WEEKDAYS[today.weekday()])
    return f"{head}\n=== DANE ZAWODNIKA ===\n{_context(activity_id)}"


def thread_messages(thread: str, limit: int = 100) -> list[dict]:
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT id, role, content, meta, created_at FROM messages WHERE thread=? ORDER BY id DESC LIMIT ?",
            (thread, limit)).fetchall()
    finally:
        conn.close()
    out = []
    for r in reversed(rows):
        m = dict(r)
        m["meta"] = json.loads(m["meta"]) if m["meta"] else None
        out.append(m)
    return out


def _save(thread: str, role: str, content: str, meta: dict | None = None) -> None:
    conn = store.connect()
    try:
        conn.execute(
            "INSERT INTO messages (thread, role, content, meta, created_at) VALUES (?, ?, ?, ?, ?)",
            (thread, role, content, json.dumps(meta, ensure_ascii=False) if meta else None,
             dt.datetime.now().replace(microsecond=0).isoformat()))
        conn.commit()
    finally:
        conn.close()


def clear_thread(thread: str) -> None:
    conn = store.connect()
    try:
        conn.execute("DELETE FROM messages WHERE thread=?", (thread,))
        conn.commit()
    finally:
        conn.close()


def extract_plan(reply: str) -> tuple[str, dict | None, str | None]:
    """Split the reply into visible text and the plan-ops JSON (if any)."""
    m = PLAN_BLOCK.search(reply)
    if not m:
        return reply.strip(), None, None
    text = (reply[:m.start()] + reply[m.end():]).strip()
    try:
        ops = json.loads(m.group(1))
        return text, ops if isinstance(ops, dict) else None, None
    except ValueError as exc:
        return text, None, f"Nie udało się odczytać zmian planu: {exc}"


def chat(thread: str, message: str, activity_id: int | None = None) -> dict:
    message = message.strip()
    if not message:
        raise ValueError("Pusta wiadomość.")
    _save(thread, "user", message)
    history: list[dict] = []
    for m in thread_messages(thread, HISTORY_MESSAGES):
        if (m["meta"] or {}).get("error"):
            continue
        if history and history[-1]["role"] == m["role"]:
            history[-1]["content"] += "\n\n" + m["content"]
        else:
            history.append({"role": m["role"], "content": m["content"]})
    while history and history[0]["role"] != "user":
        history.pop(0)

    try:
        raw = llm.complete(system_prompt(activity_id), history)
    except llm.LLMError as exc:
        _save(thread, "assistant", f"⚠ {exc}", {"error": True})
        return {"ok": False, "error": str(exc)}

    text, ops, parse_error = extract_plan(raw)
    meta: dict = {}
    if ops:
        meta["plan_changes"] = plan.apply_ops(ops, source="coach")
    if parse_error:
        meta["plan_error"] = parse_error
    _save(thread, "assistant", text or "(pusta odpowiedź)", meta or None)
    return {"ok": True, "reply": text, "meta": meta}

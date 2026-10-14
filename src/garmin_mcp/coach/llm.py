"""Talk to Claude for the coach chat.

Two backends, picked automatically:
- "api":  Anthropic API with your key (set in the dashboard, stored in
          ~/.garmin-coach/config.json, or ANTHROPIC_API_KEY). Paid per use.
- "cli":  the Claude Code CLI (`claude -p`) if installed and logged in — uses
          your Claude subscription, no API key needed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import requests

from garmin_mcp.coach import store

DEFAULT_MODEL = "claude-sonnet-5-5"
API_URL = "https://api.anthropic.com/v1/messages"


class LLMError(RuntimeError):
    pass


def _api_key() -> str | None:
    return store.read_config().get("anthropic_api_key") or os.getenv("ANTHROPIC_API_KEY")


def _claude_cli() -> str | None:
    custom = store.read_config().get("claude_cli_path")
    if custom and os.path.exists(custom):
        return custom
    return shutil.which("claude")


def status() -> dict:
    cfg = store.read_config()
    pref = cfg.get("backend", "auto")
    key, cli = _api_key(), _claude_cli()
    if pref == "api":
        active = "api" if key else None
    elif pref == "cli":
        active = "cli" if cli else None
    else:
        active = "api" if key else ("cli" if cli else None)
    return {
        "backend": active,
        "preference": pref,
        "has_api_key": bool(key),
        "api_key_hint": ("…" + key[-4:]) if key else None,
        "cli_path": cli,
        "model": cfg.get("model") or DEFAULT_MODEL,
    }


def complete(system: str, messages: list[dict], max_tokens: int = 4000) -> str:
    st = status()
    if st["backend"] == "api":
        return _complete_api(system, messages, st["model"], max_tokens)
    if st["backend"] == "cli":
        return _complete_cli(system, messages, st["cli_path"])
    raise LLMError(
        "Trener nie ma połączenia z Claude. Wklej klucz API w ustawieniach trenera "
        "albo zainstaluj Claude Code (polecenie `claude`) i zaloguj się."
    )


MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp", ".gif": "image/gif", ".pdf": "application/pdf"}


def read_files(prompt: str, files: list, max_tokens: int = 6000) -> str:
    """Ask Claude to read images/PDFs (e.g. photos of body-composition printouts)."""
    st = status()
    if st["backend"] == "api":
        import base64

        content: list[dict] = []
        for i, f in enumerate(files):
            media = MEDIA_TYPES.get(f.suffix.lower(), "image/jpeg")
            data = base64.standard_b64encode(f.read_bytes()).decode()
            kind = "document" if media == "application/pdf" else "image"
            content.append({"type": "text", "text": f"Plik #{i}: {f.name}"})
            content.append({"type": kind, "source": {"type": "base64", "media_type": media, "data": data}})
        content.append({"type": "text", "text": prompt})
        return _complete_api("Odczytujesz wyniki pomiarów z obrazów. Zwracasz wyłącznie JSON.",
                             [{"role": "user", "content": content}], st["model"], max_tokens)
    if st["backend"] == "cli":
        listing = "\n".join(f"Plik #{i}: {f}" for i, f in enumerate(files))
        full = (f"Odczytaj narzędziem Read każdy z plików:\n{listing}\n\n{prompt}\n"
                "Odpowiedz wyłącznie JSON-em, bez komentarza.")
        try:
            out = subprocess.run(
                [st["cli_path"], "-p", "--output-format", "text", "--tools", "Read",
                 "--allowedTools", "Read", "--no-session-persistence"],
                input=full, capture_output=True, text=True, encoding="utf-8",
                timeout=900, cwd=str(files[0].parent),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LLMError(f"Nie udało się uruchomić Claude Code: {exc}") from exc
        if out.returncode != 0:
            raise LLMError(_cli_error(out))
        return out.stdout.strip()
    raise LLMError("Brak połączenia z Claude — ustaw klucz API albo Claude Code w zakładce Trener → Ustawienia.")


def _cli_error(out) -> str:
    detail = (out.stderr or out.stdout or "").strip()[:500]
    low = detail.lower()
    if any(k in low for k in ("authenticate", "oauth", "not logged in", "login", "401")):
        return ("Claude Code nie jest zalogowany na tym komputerze albo sesja wygasła. "
                "W cmd uruchom: claude auth login — zaloguj się w przeglądarce i wyślij wiadomość ponownie "
                f"(dashboard nie wymaga restartu). Szczegóły: {detail}")
    return f"Claude Code zakończył się błędem: {detail}"


def _complete_api(system: str, messages: list[dict], model: str, max_tokens: int) -> str:
    try:
        r = requests.post(
            API_URL,
            headers={
                "x-api-key": _api_key(),
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": max_tokens,
                # The data context is re-sent every turn; caching makes follow-ups cheap.
                "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                "messages": messages,
            },
            timeout=300,
        )
    except requests.RequestException as exc:
        raise LLMError(f"Brak połączenia z API Anthropic: {exc}") from exc
    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", r.text)
        except ValueError:
            msg = r.text
        raise LLMError(f"API Anthropic ({r.status_code}): {msg}")
    data = r.json()
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def _complete_cli(system: str, messages: list[dict], cli: str) -> str:
    convo = "\n\n".join(
        f"{'ZAWODNIK' if m['role'] == 'user' else 'TRENER'}: {m['content']}" for m in messages
    )
    prompt = (
        f"{system}\n\n=== ROZMOWA ===\n{convo}\n\n"
        "Odpowiedz jako TRENER na ostatnią wiadomość zawodnika. Nie używaj narzędzi — "
        "wszystkie dane masz powyżej."
    )
    def run(extra: list[str]):
        return subprocess.run(
            [cli, "-p", "--output-format", "text", *extra],
            input=prompt, capture_output=True, text=True, encoding="utf-8",
            timeout=600, cwd=str(store.data_dir()),
        )

    try:
        # No tools, no saved session: the coach only needs to answer from the data given.
        out = run(["--tools", "", "--no-session-persistence"])
        if out.returncode != 0 and "unknown option" in (out.stderr or "").lower():
            out = run([])  # older Claude Code without these flags
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LLMError(f"Nie udało się uruchomić Claude Code: {exc}") from exc
    if out.returncode != 0:
        raise LLMError(_cli_error(out))
    return out.stdout.strip()

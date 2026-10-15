# Trener triathlonowy — dashboard + MCP

Dodatek do tego forka: lokalna baza z danymi z Garmin Connect, dashboard w przeglądarce
odświeżany automatycznie i narzędzia MCP, dzięki którym Claude działa jako trener.

```
Garmin Connect ──(co 15 min)──► ~/.garmin-coach/garmin.db ──► dashboard http://127.0.0.1:8787
                                          │
                                          └──► get_coach_snapshot (MCP) ──► Claude = trener
```

Dane i profil zawodnika leżą w `%USERPROFILE%\.garmin-coach\`, poza repozytorium — nic
osobistego nie trafi do gita.

## Pierwsze uruchomienie (Windows, cmd)

1. Zainstaluj `uv` (zarządza Pythonem i zależnościami), potem zamknij i otwórz cmd od nowa:
   ```
   winget install --id=astral-sh.uv -e
   ```
2. W folderze repo zainstaluj zależności:
   ```
   cd %USERPROFILE%\Documents\GitHub\garmin_mcp
   uv sync
   ```
3. Zaloguj się do Garmina **raz** (email, hasło, ewentualnie kod MFA). Tokeny zapiszą się w
   `%USERPROFILE%\.garminconnect` i wystarczą na kilka miesięcy — hasło nie jest nigdzie zapisywane:
   ```
   uv run garmin-mcp-auth
   ```
4. Uruchom dashboard (albo kliknij dwa razy `start-dashboard.bat`):
   ```
   uv run garmin-dashboard
   ```
   Otworzy się przeglądarka. Pierwsza synchronizacja pobiera ~180 dni treningów i 12 dni
   zdrowia; resztę historii (60 dni) dociąga po 10 dni co 15 minut, żeby nie wpaść w limit Garmina.
   Chcesz całą historię od razu: `uv run garmin-coach-sync --all`.
5. Uzupełnij profil zawodnika — cel, data startu, godziny w tygodniu, progi:
   ```
   notepad %USERPROFILE%\.garmin-coach\athlete.md
   ```

Opcje: `uv run garmin-dashboard --interval 10` (minuty, min. 5), `--port 8790`, `--no-browser`.
Dashboard działa, dopóki otwarte jest okno cmd.

## Trener w Claude Desktop

Otwórz `%APPDATA%\Claude\claude_desktop_config.json` i dodaj serwer (ścieżkę do `uv.exe`
sprawdzisz poleceniem `where uv`):

```json
{
  "mcpServers": {
    "garmin": {
      "command": "C:\\Users\\Michal\\.local\\bin\\uv.exe",
      "args": ["--directory", "C:\\Users\\Michal\\Documents\\GitHub\\garmin_mcp", "run", "garmin-mcp"],
      "env": {
        "GARMIN_ENABLED_TOOLS": "get_coach_snapshot,sync_coach_data,update_athlete_profile,get_training_plan,update_training_plan,get_activities_by_date,get_activity,get_activity_splits,get_activity_hr_in_timezones,get_activity_power_in_timezones,get_power_duration_curve,get_training_readiness,get_training_status,get_training_load_trend,get_recovery_time_remaining,get_hrv_trend,get_sleep_summary,get_vo2max_trend,get_race_predictions,get_lactate_threshold,get_cycling_ftp,get_heart_rate_zones,get_scheduled_workouts,get_calendar_events"
      }
    }
  }
}
```

`GARMIN_ENABLED_TOOLS` zostawia 24 narzędzia (odczyt z Garmina + profil i plan zapisywane lokalnie) zamiast 160 — trener
dostaje mniej szumu, a nic nie zmieni się na Twoim koncie Garmin. Zrestartuj Claude Desktop.

Przykładowe pytania:
- „Jak wyglądam dziś? Mam zaplanowane interwały na rowerze."
- „Podsumuj ostatni tydzień i zaproponuj plan na następny pod mój start."
- „Przeanalizuj wczorajszy bieg — splity, tętno, dryf."
- „Zaktualizuj profil: start przesunięty na 2027-06-14, mam 10 h tygodniowo."

Najważniejsze narzędzie to `get_coach_snapshot`: w jednym wywołaniu daje profil, sygnały
regeneracji, CTL/ATL/TSB łącznie i per dyscyplina, 16 tygodni objętości, zdrowie dzień po dniu
i ostatnie treningi.

## Zakładka „Trener” w dashboardzie

- **Plan treningowy** — kalendarz na 2 tygodnie (strzałkami dalej/wstecz). Sesje dopasowują się
  automatycznie do treningów z Garmina: ✓ zrobione, ✕ opuszczone, „+” trening poza planem.
  Kliknij sesję, żeby zobaczyć jej strukturę, oznaczyć jako pominiętą, usunąć albo zapytać o nią trenera.
- **Progres i regres** — dla każdej dyscypliny: ostatnie 4 tygodnie vs 4 tygodnie wcześniej.
  Bieg: tempo przy stałym tętnie; rower: moc przy stałym tętnie (wymaga pomiaru mocy);
  pływanie: tempo na 100 m. Do tego VO₂max i objętość.
- **Omawianie treningu** — kliknij trening (w tabeli na Pulpicie albo w kalendarzu). Zobaczysz
  okrążenia, czas w strefach i porównanie z podobnymi sesjami, a rozmowa z trenerem dotyczy
  wtedy tego treningu (każdy trening ma własny wątek).
- **Czat** — trener widzi profil, regenerację, obciążenie, progres i plan. Gdy prosisz o plan albo
  zmianę („przesuń długi rower na niedzielę”, „mam kontuzję łydki, bez biegania przez tydzień”),
  sam zapisuje zmiany w kalendarzu. Ten sam plan widzi Claude Desktop (`get_training_plan`,
  `update_training_plan`).

## Treningi na zegarek (Garmin Connect)

Przycisk **„⌚ Na zegarek”** w karcie planu włącza wysyłanie nadchodzących sesji (pływanie, rower,
bieg — na 14 dni do przodu) do Garmin Connect jako treningi **„Coach: …”** (angielskie nazwy), zaplanowane na swój
dzień. Po synchronizacji zegarka trening jest w kalendarzu na zegarku z krokami i celami
(strefa tętna, % FTP / waty, tempo).

- Trener zapisuje do każdej sesji strukturę kroków (`steps`): rozgrzewka, powtórzenia, przerwy,
  schłodzenie z celami. Sesja bez struktury trafia jako jeden krok z czasem i strefą z opisu.
- Zmiana planu = stary trening usuwany z Garmina, nowy wysyłany. Usunięta lub pominięta sesja
  znika z kalendarza Garmin. Treningi z przeszłości zostają.
- Siła i zakładki (brick) na razie nie są wysyłane. Długość basenu dla pływania: 25 m
  (zmienisz w `config.json`: `"pool_length_m": 50`).

## Kalendarz Google

Treningi z planu mogą trafiać do Kalendarza Google jako wydarzenia całodniowe (bez godziny),
w osobnym kalendarzu **„Trening”**. Każda zmiana planu (w dashboardzie, w czacie z trenerem
albo z Claude Desktop) w ciągu kilku sekund dodaje, przesuwa lub usuwa wydarzenia. Po każdej
synchronizacji z Garminem wykonane treningi dostają „✓”. Pominięte sesje i dni odpoczynku
nie trafiają do kalendarza.

Konfiguracja (raz, ok. 5 min) — Trener → Plan treningowy → „📅 Kalendarz Google”:
1. console.cloud.google.com → nowy projekt.
2. APIs & Services → Library → Google Calendar API → Enable.
3. Google Auth Platform: typ External, nazwa aplikacji, Twój e-mail; w Audience **Publish app**
   („In production”) — w trybie testowym Google unieważnia dostęp co 7 dni.
4. Clients → Create client → **Desktop app** → wklej Client ID i Client secret w dashboardzie.
5. „Połącz z Google” → zaloguj się. Ostrzeżenie o niezweryfikowanej aplikacji jest normalne
   (to Twoja własna aplikacja): Zaawansowane → Przejdź do….

Aplikacja prosi tylko o zakres `calendar.app.created`, czyli dostęp wyłącznie do kalendarza,
który sama utworzyła — nie widzi Twoich innych kalendarzy. Klucz i token zapisują się lokalnie
w `config.json`. Nie edytuj treningów ręcznie w kalendarzu „Trening” — zmiany zostaną nadpisane
planem; zmieniaj plan w dashboardzie lub u trenera.

### Połączenie z Claude (Ustawienia w czacie)

Dashboard sam wybiera jedną z dwóch dróg:

1. **Klucz API Anthropic** — wklejasz go w Ustawieniach (zapisuje się tylko w
   `%USERPROFILE%\.garmin-coach\config.json`). Płatne za użycie, osobno od subskrypcji Claude;
   jedna wiadomość to kilkanaście–kilkadziesiąt tysięcy tokenów kontekstu (Twoje dane), więc
   liczy się w centach. Domyślny model: `claude-sonnet-5-5` (do zmiany w Ustawieniach).
2. **Claude Code** — jeśli masz zainstalowane polecenie `claude` i jesteś zalogowany,
   trener użyje Twojej subskrypcji, bez klucza.

Rozmowy i plan trzymane są lokalnie w `garmin.db`.

## Jak liczone są wskaźniki

- **Obciążenie treningu**: Training Load z Garmina; gdy go brak (często pływanie) — TSS z
  pomiaru mocy, potem szacunek z tętna (hrTSS, LTHR ≈ 89% najwyższego tętna z treningów),
  a w ostateczności z czasu. Szacunki są oznaczone gwiazdką.
- **CTL** (forma) = średnia wykładnicza 42 dni, **ATL** (zmęczenie) = 7 dni, **TSB** = CTL − ATL
  z poprzedniego dnia. Skala to skala obciążenia Garmina, nie TSS z TrainingPeaks — patrz na
  trendy i proporcje, nie na progi z książek.
- **Sygnały** (reguły): HRV poniżej normy Garmina (1–2 noce = uwaga, 3+ = alarm), tętno
  spoczynkowe ≥ 5 bpm ponad średnią 7 dni, sen < 6,5 h lub wynik < 60, gotowość < 40,
  ATL/CTL > 1,3 / 1,5, kilometraż biegowy > 130% średniej z 3 tygodni, dyscyplina
  nietrenowana ≥ 8 dni.
- Zawody multisport z Garmina liczą się do obciążenia łącznego, ale nie do CTL per dyscyplina.

## Uwagi

- To nieoficjalne API Garmina (te same endpointy co aplikacja). Może się zepsuć po zmianach
  po stronie Garmina — wtedy `git pull` z repo upstream albo podbicie `garminconnect`.
- Odpowiedź 429 (limit zapytań) wstrzymuje synchronizację na 30 minut automatycznie.
- Nowe dane pojawiają się dopiero po synchronizacji zegarka z aplikacją Garmin Connect.
- Gdy tokeny wygasną, dashboard pokaże komunikat — wtedy ponownie `uv run garmin-mcp-auth`.

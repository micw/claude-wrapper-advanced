# Das Wire-Format (`/wire/v1`)

Das Vokabular, das dieser Wrapper nach außen spricht — für alles, wofür die
OpenAI-Formate kein Feld haben. Die OpenAI-Oberflächen unter `/v1` sind **Konsumenten**
dieses Stroms, nicht umgekehrt.

Belege für jede Feldentscheidung stehen in [MESSUNGEN.md](MESSUNGEN.md); dieses Dokument
ist der Vertrag.

---

## 1. Warum es das gibt

Zwei Gründe, beide aus der Praxis:

1. **Die Abschlussdaten reisten neben dem Strom.** Der Treiber lieferte Tupel und füllte
   nebenher ein `stats`-Dict; jeder Konsument musste wissen, *wann* welches Feld darin
   steht — Usage beim `result`, bei einem Tool-Turn aber aus der assistant-Message,
   Kosten beim Tool-Paar gar nicht. Ein Vertrag per Verabredung. Jetzt steht alles, was
   ein Turn abschließend zu sagen hat, **im** `done`-Ereignis.
2. **Messbares fiel durch.** Kontingent-Zustand, Kosten pro Modell, Cache-Aufteilung nach
   TTL, echte Denk-Tokens, Prozess-Zeiten — die OpenAI-Formate haben dafür keinen Platz,
   also verwarfen beide Oberflächen es still.

---

## 2. `POST /wire/v1/responses`

Nimmt dieselben `messages`, `tools`, `model` und `reasoning_effort` entgegen wie
`/v1/chat/completions` — die Übersetzung nach innen ist identisch, unterschiedlich ist
nur, was herauskommt.

Antwort ist SSE. Jedes Ereignis ist ein JSON-Objekt in `data:`, der Typ steht im Feld
`type`. **Kein `event:`-Feld**, damit der Konsument nur an einer Stelle nachsieht. Bricht
der Client die Verbindung ab, endet der Turn mit ihr.

```
data: {"type":"started","model":"claude-sonnet-5","reused":false}
data: {"type":"text_delta","text":"O"}
data: {"type":"done","stop_reason":"end_turn","text":"OK","usage":{…},"cost":{…},"timing":{…}}
```

### Die Ereignisse

| `type` | Felder | Anmerkung |
|---|---|---|
| `started` | `model`, `reused` | `reused` ist die einzige Stelle, an der die Prozess-Natur nach außen sichtbar wird |
| `text_delta` | `text` | |
| `thinking_progress` | `tokens` | **kein Text** — die CLI redigiert ihn (MESSUNGEN.md §3) |
| `tool_call` | `id`, `name`, `arguments` | `arguments` ist ein JSON-**String**, wie die CLI ihn liefert; höchstens einer pro Turn |
| `limit_status` | `window`, `claim`, `status`, `resets_at`, `overage`, `usage_stale` | nur im Alarmfall, s.u. |
| `done` | `stop_reason`, `text`, `usage`, `cost`, `timing` | sauberes Ende |
| `failed` | `error_type`, `message`, `upstream_status`, `retryable` | getrennt von `done`, weil Fehler und Ende zwei Fälle sind |

### `usage` im `done`

```json
{"input_new": 2, "cache_read": 3219, "cache_write": 5507,
 "cache_write_5m": 0, "cache_write_1h": 5507,
 "input_total": 8728, "output": 6, "thinking": 490, "service_tier": "standard"}
```

**`input_new` enthält die Cache-Treffer NICHT.** Die CLI meldet `input_tokens` als den
nicht gecachten Rest — gemessen 2 Tokens neben 3219 Treffern. Der `codex-api-wrapper`
zählt an dieser Stelle anders (dort schließt `input_tokens` die Treffer ein), deshalb die
abweichenden Namen und die ausgerechnete Summe `input_total` daneben. Wer beide Wrapper
bedient, darf nicht dieselbe Addition bauen.

`thinking` ist die **echte** Zahl aus `message_delta`, wo der Turn eine liefert, sonst die
Summe der Schätzungen (gemessen: 490 echt gegen 450 geschätzt). `cache_write_5m` / `_1h`
sind `None`, wo die CLI die Aufteilung nicht mitschickt — nicht 0, das wäre eine Behauptung.

### `cost` im `done`

```json
{"total_usd": 0.0035, "by_model": {"claude-sonnet-5": {…}, "claude-haiku-4-5-…": {…}}}
```

Zwei Warnungen, die im Feld selbst nicht stehen können:

- **`by_model` enthält Fremdarbeit.** Auf einem Sonnet-Turn steht dort auch Haiku — CLI-
  interne Nebenaufrufe. `total_usd` ist also nicht die Kostenzahl des Modell-Turns; die
  Aufschlüsselung ist der einzige Weg, das zu trennen.
- **Es sind nominale API-Listenpreise und kein Maß für den Abo-Verbrauch.** Kostengleich
  gemessen bewegte Opus das Kontingent, Sonnet nicht (MESSUNGEN.md §5.2).

---

## 3. `GET /wire/v1/usage`

Der Kontingent-Stand des Kontos. **Die einzige Quelle für Füllstände** und für die Frage,
welchem Modell ein Fenster gehört.

```json
{
  "windows": {
    "global/five_hour":        {"used_percent": 34, "window_seconds": 18000,
                                "resets_at": 1787909400, "resets_in_seconds": 5985,
                                "reached": false, "scope": null, "is_active": false},
    "global/seven_day":        {"used_percent": 73, "window_seconds": 604800, "…": "…"},
    "model:fable-5/seven_day": {"used_percent": 1,
                                "scope": {"model": "Fable", "surface": null}, "…": "…"}
  },
  "credits": {"enabled": false, "utilization": null, "…": "…"},
  "fetched_at": 1787903000
}
```

Ein Request an das Backend, gecacht (`USAGE_TTL`, Standard 60 s). `?force=1` umgeht den
Cache — gedacht für den Moment, in dem ein `limit_status` gemeldet hat.

Ein Minutentakt verliert nichts: die Auflösung des Backends beträgt einen Prozentpunkt,
und im Leerlauf bewegt sich der Zähler nicht (MESSUNGEN.md §1).

### Wenn die Quelle nicht antwortet

Die Usage-API des Backends limitiert sich **selbst** — im Betrieb beobachtet: ein `429`,
während dasselbe Konto von anderer Stelle `200` bekam, ohne dass ein Kontingent erschöpft
war. Der Wrapper reagiert darauf so:

- **Sperre nach einem Fehlschlag** (`Retry-After`, sonst `USAGE_FAILURE_BACKOFF`, Standard
  60 s, gedeckelt auf 15 min). Ohne sie löste jeder Consumer-Request einen neuen Versuch
  aus und der Wrapper hielte ein 429 selbst am Leben. **`?force=1` umgeht die Sperre
  nicht** — sonst wäre es genau dieser Umgehungsweg.
- **Der letzte bekannte Stand wird weitergereicht**, markiert mit `stale: true` und
  `stale_reason`. Ein alter Füllstand ist brauchbar, solange klar ist, dass er alt ist.
- **`503` nur, wenn es überhaupt keinen Stand gibt** — also bis zum ersten erfolgreichen
  Abruf nach dem Start.

---

## 4. Die Fensterschlüssel — und warum sie gebaut werden

Turn und Usage-API benennen dieselben Fenster **verschieden**. Kontoweit stimmen sie
überein; beim modell-skopierten Fenster gibt es in den Daten **keine** gemeinsame Kennung
(MESSUNGEN.md §6). Deshalb wird der Schlüssel aus **Dauer + Geltungsbereich** gebildet,
was beide Quellen hergeben:

```
global/<dauer>          kontoweit          z.B. global/seven_day
model:<modell>/<dauer>  nur dieses Modell  z.B. model:fable-5/seven_day
surface:<name>/<dauer>  nur diese Oberfläche
```

Auf der Turn-Seite steuert der Wrapper den Geltungsbereich bei — er weiß, auf welchem
Modell der Turn lief. Das ist keine Heuristik, sondern der Parameter, den er selbst
gesetzt hat.

**Bedingung, unter der das trägt:** ein Modell hat höchstens ein skopiertes Fenster. Heute
erfüllt. Lässt sich ein Claim nicht zuordnen, ist `window` **`null`** und `claim` trägt die
rohe Backend-Kennung — der Konsument lädt dann über `/usage` nach, statt ein falsches
Etikett zu bekommen.

---

## 5. `limit_status`: Alarm, keine Anzeige

Das Ereignis kommt **nur**, wenn etwas anliegt: Warnung, Ablehnung, Guthaben in Benutzung
oder ein Fehlercode. Nicht bei jedem Turn — insbesondere löst `overageStatus` allein nicht
aus, das steht auf einem Konto ohne Guthaben dauerhaft auf `rejected`.

Es trägt **keine Zahlen**. Der Turn kennt keine: das Backend schickt `utilization` nur beim
Warnen und beim 429 (MESSUNGEN.md §4.1). `usage_stale: true` sagt genau das, damit niemand
eine fehlende Zahl als 0 liest.

**Empfohlenes Muster für den Konsumenten:**

```
einmal beim Start        GET /wire/v1/usage        Karte: welche Fenster, wem, wie voll
laufend                  alle ~60 s pollen         Füllstände aktuell halten
bei limit_status         GET /wire/v1/usage?force=1   frischer Stand, sofort
```

Nachladen lohnt außerdem bei einem **unbekannten `window`**: dann kennt der Konsument ein
Fenster noch nicht, und die API erklärt es. Damit heilt sich die Zuordnung selbst, wenn das
Backend ein neues Fenster provisioniert.

---

## 6. Was bewusst fehlt

- **Kein Denktext.** Die CLI redigiert ihn; ein `text`-Feld, das nie Text trägt, wäre eine
  Lüge im Schema.
- **Kein Reasoning-Replay-Item.** Anders als bei der Responses-API gibt es hier nichts, was
  in einen Folge-Turn zurückgespielt werden könnte.
- **Kein `active_group`.** Welches Fenster ein Turn belastet hat, sagt das Backend nicht.
  `limit_status.claim` ist die Wahl des Servers, welches Fenster er für repräsentativ hält
   — gemessen `five_hour` in 18 von 18 Turns, auch bei vollerem Wochenfenster.
- **Keine Füllstände im Turn.** Siehe §5. Sie wären nur über einen Capture-Proxy im
  Auth-Pfad zu haben; die Abwägung und die Entscheidung dagegen stehen in MESSUNGEN.md §7.
- **Höchstens ein `tool_call` pro Turn** — die Decke der CLI, nicht des Formats.

Auch ein Tool-Turn endet nach `tool_call` mit `done` (`stop_reason: "tool_use"`). Seine Usage
stammt aus der abschließenden Assistant-Message; Kosten können dort unbekannt sein, weil die CLI
sie erst in einem `result` meldet, das ein unterbrochener Tool-Turn nicht erreicht.

---

## 7. Stand der Umstellung

Intern ist der Wire-Strom die Quelle: Treiber und Pool liefern Ereignisse, `/wire/v1` gibt
sie unverändert aus.

`cli_driver.drive_turn()` daneben ist ein **Adapter auf die alten Tupel**, damit die beiden
OpenAI-Oberflächen in `main.py` unverändert weiterlaufen. Er entfällt, sobald diese den
Wire-Strom direkt konsumieren; dann verschwindet auch das interne Feld `_raw` am
`tool_call` und die Doppelung zwischen `main._request_json` und `wire_api._body`.

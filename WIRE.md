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
data: {"type":"quota","usage":{"groups":[…]}}
data: {"type":"text_delta","text":"O"}
data: {"type":"done","stop_reason":"end_turn","text":"OK","usage":{…},"cost":{…},"timing":{…}}
```

### Die Ereignisse

| `type` | Felder | Anmerkung |
|---|---|---|
| `started` | `model`, `reused` | `reused` ist die einzige Stelle, an der die Prozess-Natur nach außen sichtbar wird |
| `text_delta` | `text` | |
| `thinking_progress` | `tokens` | **kein Text** — die CLI redigiert ihn (MESSUNGEN.md §3) |
| `tool_call` | `id`, `name`, `arguments` | `id` ist die **Backend-Kennung** (`toolu_…`), `arguments` ein JSON-**String**, wie die CLI ihn liefert; höchstens einer pro Turn |
| `limit_status` | `window`, `claim`, `status`, `resets_at`, `surpassed_threshold`, `overage`, `usage_stale` | nur im Alarmfall, s.u. |
| `quota` | `usage` | vollständiger letzter Kontingent-Snapshot nach frischen Turn-Response-Headern |
| `done` | `stop_reason`, `text`, `usage`, `cost`, `timing` | sauberes Ende — **auch nach einem Tool-Call**, s.u. |
| `failed` | `error_type`, `message`, `upstream_status`, `retryable` | getrennt von `done`, weil Fehler und Ende zwei Fälle sind |

### Jeder Turn endet mit `done` oder `failed` — auch ein Tool-Turn

Ein Tool-Turn schließt mit

```json
{"type":"done","stop_reason":"tool_use","text":"","usage":{…},"cost":{…},"timing":{…}}
```

Das ist nicht kosmetisch: die CLI schickt bei einem Tool-Call **kein** `result`-Ereignis,
die Usage steht dort nur in `message_start` und in der assistant-Message. Ohne dieses
`done` verlöre ein Wire-Konsument die Tokenzahlen jedes Tool-Turns — und in einer
Agentenschleife ist das die Mehrzahl aller Turns.

`cost.total_usd` ist dabei `null`: `total_cost_usd` liefert die CLI nur im `result`, der
Kostenanteil eines Tool-Turns erscheint deshalb erst im nächsten vollen Turn. Kumulativ
korrekt, pro Turn verschoben — bekannt und nicht behebbar, solange die CLI es so meldet.

Der Tupel-Adapter für die OpenAI-Oberflächen unterdrückt dieses `done`: dort ist der
Tool-Call selbst der Abschluss, und ein zusätzliches leeres Ergebnis würde an jedem
Tool-Call eine leere Antwort erzeugen. `tests/test_legacy_adapter.py` nagelt das fest.

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

## 3. `GET /wire/v1/info` — womit spreche ich?

```json
{"service": "claude-wrapper-advanced", "version": "1.7.0"}
```

Mehr nicht, und das ist Absicht:

- **Die Vertragsversion steht im Pfad.** `/wire/v1` sagt bereits, welches Format gilt; ein
  zusätzliches Feld dafür wäre eine zweite Wahrheit über dieselbe Sache.
- **`version` ist die Release-Version** und wandert mit dem Git-Tag. Wer sein Verhalten
  daran festmacht, macht es am Falschen fest — dafür ist die Pfadversion da. Sie taugt für
  Logs, Fehlerberichte und die Frage, ob ein Deployment schon die neue Fassung fährt.
- **Fähigkeiten stehen dort, wo sie gelten:** was die Modelle können, sagt `/models`; wie
  voll die Kontingente sind, sagt `/usage`. Ein dritter Ort, der beides zusammenfasst,
  wäre eine Kopie, die veraltet.

---

## 4. `GET /wire/v1/models` — die Registry, ungeschminkt

```json
{"models": [
  {"id": "opus-5", "name": "Opus 5", "backend_model": "claude-opus-5",
   "context_length": 1000000,
   "input_modalities": ["text", "image"],
   "efforts": {"supported": ["low","medium","high","xhigh","max"], "default": "high"},
   "knowledge_cutoff": null, "aliases": ["opus"]}
]}
```

Der Unterschied zu `/v1/models`: dort werden aus sieben echten Modellen **fünfzehn
Einträge**, weil vier Aliase und vier Effort-Picks (`opus:max`, `sonnet:low`, …) als
Pseudo-Modelle mitlaufen — das braucht ein Model-Picker, der die Effort-Wahl über die
Modellauswahl abbilden muss. Ein Konsument dieser API braucht das Gegenteil: jedes Modell
**einmal**, mit seinen Stufen als Feld und den Aliasen als Liste daran.

`backend_model` ist der Name, den die CLI kennt — und derselbe Schlüssel, unter dem
`done.cost.by_model` abrechnet. Nur damit lässt sich eine Kostenzeile einem Eintrag dieser
Liste zuordnen; ohne ihn bliebe der Haiku-Nebenaufruf ein namenloser Posten.

`input_modalities` gehört zum Modell und kommt aus derselben handgepflegten Registry wie
Kontextfenster und Effort-Stufen. Der Claude-CLI-Pfad bietet keinen dynamischen Modellkatalog,
den der Wrapper unverändert durchreichen könnte; deshalb wird die Fähigkeit hier explizit
geführt, statt vom Consumer aus dem Providernamen erraten zu werden.

`efforts.default` ist der Env-Default, **abgesenkt auf das, was das Modell kennt** — dieselbe
Absenkung, die ein Request erfährt. Ein Modell ohne Stufen (Haiku) hat `supported: []` und
`default: null`, nicht `"high"`. `knowledge_cutoff` ist `null`, wo die CLI für ein Modell
keinen nennt (Opus 5) — wir erfinden dann auch keinen.

---

## 5. `GET /wire/v1/usage`

Der letzte aus offiziellen CLI-Response-Headern beobachtete Kontingent-Stand. Gruppen
tragen ihr eigenes Alter: ein Sonnet-Turn kann global gerade erneuert haben, während der
Fable-Stand älter ist.

```json
{
  "groups": [
    {"id":"global", "upstream_id":null, "scope":null,
     "observed_at":1787903000, "age_seconds":42,
     "windows":[
       {"id":"five_hour", "upstream_id":"5h", "used_percent":34,
        "window_seconds":18000, "resets_at":1787909400},
       {"id":"seven_day", "upstream_id":"7d", "used_percent":73,
        "window_seconds":604800, "resets_at":1788412321}]},
    {"id":"model:fable-5", "upstream_id":"7d_oi",
     "scope":{"family":"fable", "models":["fable-5-1","fable-5"]},
     "observed_at":1787899000,
     "age_seconds":4042,
     "windows":[{"id":"seven_day", "upstream_id":"7d_oi", "used_percent":1,
                 "window_seconds":604800, "resets_at":1788412321}]}
  ]
}
```

Normale Turns aktualisieren passende Gruppen kostenlos. Ein normaler GET startet nur bei
Überschreiten von `USAGE_GLOBAL_MAX_AGE` bzw. `USAGE_FABLE_MAX_AGE` einen minimalen
Haiku-/Fable-CLI-Probe. Kein periodischer Timer läuft im Hintergrund.

`?force=global`, `?force=fable-5` und `?force=all` umgehen die Altersprüfung explizit.
Ein Fable-Probe liefert zugleich `5h`, `7d` und `7d_oi`; `all` braucht deshalb heute nur
diesen einen Probe. Gleichzeitige gleiche Probes werden per Singleflight zusammengefasst.

### Wenn die Quelle nicht antwortet

Schlägt ein altersgesteuerter Probe fehl, bleibt der letzte bekannte Stand erhalten; sein
`age_seconds` macht die Alterung sichtbar. `503` gibt es nur beim Cold Start ohne jeden
globalen Stand. Ein erzwungener Probe meldet seinen Fehler, statt einen erfolgreichen
Reload vorzutäuschen.

---

## 6. Die Fensterschlüssel — und warum sie gebaut werden

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

## 7. `limit_status`: Alarm, keine Anzeige

Das Ereignis kommt **nur**, wenn etwas anliegt: Warnung, Ablehnung, Guthaben in Benutzung
oder ein Fehlercode. Nicht bei jedem Turn — insbesondere löst `overageStatus` allein nicht
aus, das steht auf einem Konto ohne Guthaben dauerhaft auf `rejected`.

Es trägt **keine Zahlen**. Der Turn kennt keine: das Backend schickt `utilization` nur beim
Warnen und beim 429 (MESSUNGEN.md §4.1). `usage_stale: true` sagt genau das, damit niemand
eine fehlende Zahl als 0 liest.

**Empfohlenes Muster für den Konsumenten:**

```
einmal beim Start        GET /wire/v1/usage        Karte: welche Fenster, wem, wie voll
laufend                  quota im Turn-Strom       Snapshot vollständig ersetzen
Reload global            GET /usage?force=global  Alterscache bewusst umgehen
```

Nachladen lohnt außerdem bei einem **unbekannten `window`**: dann kennt der Konsument ein
Fenster noch nicht, und die API erklärt es. Damit heilt sich die Zuordnung selbst, wenn das
Backend ein neues Fenster provisioniert.

---

## 8. Was bewusst fehlt

- **Kein Denktext.** Die CLI redigiert ihn; ein `text`-Feld, das nie Text trägt, wäre eine
  Lüge im Schema.
- **Kein Reasoning-Replay-Item.** Anders als bei der Responses-API gibt es hier nichts, was
  in einen Folge-Turn zurückgespielt werden könnte.
- **Kein `active_group`.** Welches Fenster ein Turn belastet hat, sagt das Backend nicht.
  `limit_status.claim` ist die Wahl des Servers, welches Fenster er für repräsentativ hält
   — gemessen `five_hour` in 18 von 18 Turns, auch bei vollerem Wochenfenster.
- **Kein Status im `quota`-Snapshot.** Der Wrapper liefert Messwert, Reset und Alter;
  Schwellwerte, Farben und Darstellung entscheidet der Konsument.
- **Höchstens ein `tool_call` pro Turn** — die Decke der CLI, nicht des Formats.

Auch ein Tool-Turn endet nach `tool_call` mit `done` (`stop_reason: "tool_use"`). Seine Usage
stammt aus der abschließenden Assistant-Message; Kosten können dort unbekannt sein, weil die CLI
sie erst in einem `result` meldet, das ein unterbrochener Tool-Turn nicht erreicht.

---

## 9. Stand der Umstellung

Intern ist der Wire-Strom die Quelle: Treiber und Pool liefern Ereignisse, `/wire/v1` gibt
sie unverändert aus.

`cli_driver.drive_turn()` daneben ist ein **Adapter auf die alten Tupel**, damit die beiden
OpenAI-Oberflächen in `main.py` unverändert weiterlaufen. Er entfällt, sobald diese den
Wire-Strom direkt konsumieren; dann verschwindet auch das interne Feld `_raw` am
`tool_call` und die Doppelung zwischen `main._request_json` und `wire_api._body`.

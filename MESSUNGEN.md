# Messungen

Was die Claude Code CLI und das Abo-Backend **tatsächlich** liefern — gemessen, nicht
aus Dokumentation abgeschrieben. Jede Aussage hier hat einen Lauf hinter sich, und wo
etwas unentschieden blieb, steht das ausdrücklich dabei.

Der Wrapper ist auf empirisch gefundenem CLI-Verhalten gebaut; `tests/assumptions.py`
prüft die Annahmen, die den Betrieb tragen. Dieses Dokument sammelt die Messungen, aus
denen die **Formatentscheidungen** folgen — vor allem beim Kontingent.

Gemessen mit CLI **2.1.198**, Konto-Tarif `max` (`rateLimitTier: default_claude_max_5x`).

---

## 1. Methodik: warum Verbrauchsmessungen hier schwierig sind

**Die messende Session läuft auf demselben Konto.** Wird der Wrapper aus einer Claude-Code-
Sitzung heraus vermessen, belastet jeder Turn dieser Sitzung dieselben Kontingentfenster
wie die Messung. Bei großem Kontext kostet ein einzelner Agent-Turn rund einen
Prozentpunkt des 5-Stunden-Fensters — mehr als eine ganze Messphase.

Ein erster Messversuch ist genau daran gescheitert: „vier Fable-Turns, `five_hour` +2
Punkte" war wertlos, weil zwischen den Ablesungen auch die Turns der messenden Sitzung
lagen.

**Der Aufbau, der funktioniert:**

1. Das gesamte Protokoll — ablesen, Last erzeugen, ablesen — läuft in **einem einzigen
   Kommando**. Nur solange ein Kommando läuft, ruft die messende Sitzung kein Modell auf.
2. Eine **Kontrollphase** gleicher Länge ohne Last geht voran. Gemessen: über drei Minuten
   Leerlauf bewegt sich kein Zähler. Die Basislinie hält, Differenzmessungen sind möglich.
3. Phasen werden nach **Kosten** geschnitten, nicht nach Turn-Zahl. Modelle unterscheiden
   sich pro Turn um Faktor 1,5 bis 3; sonst liegt eine Phase unter der Auflösung und
   liefert weder Befund noch Gegenbefund.

**Auflösung: ein Prozentpunkt**, in beiden Quellen. Die Header liefern `utilization` als
Bruch mit zwei Nachkommastellen (`0.71`), die Usage-API als Prozentzahl (`72.0`) — dieselbe
Körnung. Faustwert zum Vorausrechnen einer Messung, gültig für Opus und Fable: rund
**$0.14 nominale CLI-Kosten pro Prozentpunkt** des 5-Stunden-Fensters.

---

## 2. Der stream-json-Strom: wer trägt welche Zahlen

Ein Turn mit `--include-partial-messages`, aufgezeichnet:

| Ereignis | Zeitpunkt | Trägt |
|---|---|---|
| `system` / `init` | sofort | Session-Id, Modell, cwd |
| `system` / `status` | vor dem Request | `status: "requesting"` |
| `rate_limit_event` | **vor dem ersten Token** | Kontingent-Zustand (§4) |
| `stream_event` / `message_start` | **vor dem ersten Token** | vollständige **Input**-Usage |
| `stream_event` / `content_block_delta` | laufend | Text bzw. `thinking_delta` |
| `stream_event` / `message_delta` | Ende der Antwort | laufende Output-Usage inkl. **echter Denk-Tokens** |
| `result` | Turn-Ende | Gesamt-Usage, `total_cost_usd`, `modelUsage`, `duration_ms`, `ttft_ms` |

**Die Input-Seite steht vor dem ersten Token fest.** Gemessenes `message_start`:

```json
{"input_tokens": 2, "cache_creation_input_tokens": 5507, "cache_read_input_tokens": 3219,
 "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 5507},
 "output_tokens": 4, "service_tier": "standard"}
```

Das ist Inline-Usage, die der codex-Wrapper strukturell nicht liefern kann — dort meldet
die Responses-API Tokens erst in der Completion.

---

## 3. Denk-Tokens: die echte Zahl liegt im Strom

`thinking_delta` trägt **keinen Text** (die CLI redigiert ihn), nur `estimated_tokens`.
Der Wrapper hat daraus lange eine Summe gebildet. Die **echte** Zahl steht in
`message_delta.usage.output_tokens_details.thinking_tokens` — und **nicht** im
`result`-Ereignis, auch nicht in dessen `usage`.

Gemessen an einem Turn (sonnet, `--effort high`):

| | |
|---|---|
| `estimated_tokens`-Folge | `[50, 200, 200, null]` → Summe **450** |
| `output_tokens_details.thinking_tokens` | **490** |

Die Schätzung ist brauchbar, aber sie ist eine Schätzung. Der Wrapper nimmt seit
[`cli_driver.py`](app/cli_driver.py) die echte Zahl und behält die Summe als Fallback für
Turns, die vor `message_delta` enden (Abbruch, Interrupt bei Tool-Calls).

---

## 4. Kontingente: zwei Quellen, verschiedene Stärken

### 4.1 Was der Turn liefert

`rate_limit_event`, gemessen im Normalfall:

```json
{"status": "allowed", "resetsAt": 1787909400, "rateLimitType": "five_hour",
 "overageStatus": "rejected", "overageDisabledReason": "org_level_disabled",
 "isUsingOverage": false}
```

**Kein `utilization`.** Das Feld erscheint nur, wenn das Backend warnt (`allowed_warning`)
oder ablehnt (429) — im CLI-Binary ist der Warn-Pfad die einzige Stelle, die es setzt. Der
kostenlose Per-Turn-Kanal trägt also **Zustand, keine Füllstände**.

**`rateLimitType` ist nicht `active_group`.** Der Wert kommt aus dem Header
`anthropic-ratelimit-unified-representative-claim`, den der Server setzt. Gemessen nannte
er `five_hour`, während das Wochenfenster bei 72 % stand — in **18 von 18** aufgezeichneten
Turns über vier Modelle. Er sagt also weder „das vollste Fenster" noch „das Fenster, das
dieser Turn belastet hat". Wer ihn als codex' `active_group` behandelt, behauptet etwas,
das die Quelle nicht hergibt.

### 4.2 Was `GET /api/oauth/usage` liefert

Ein eigener Request mit dem OAuth-Token. Antwort (gekürzt, echtes Konto):

```json
{
  "five_hour": { "utilization": 23.0, "resets_at": "2026-08-28T09:29:59.951654+00:00" },
  "seven_day": { "utilization": 72.0, "resets_at": "2026-08-29T17:59:59.951681+00:00" },
  "seven_day_opus": null, "seven_day_sonnet": null, "nimbus_quill": { "utilization": 0.0 },
  "limits": [
    { "kind": "session",       "group": "session", "percent": 23, "scope": null,
      "resets_at": "2026-08-28T09:29:59.951654+00:00", "is_active": false },
    { "kind": "weekly_all",    "group": "weekly",  "percent": 72, "scope": null,
      "resets_at": "2026-08-29T17:59:59.951681+00:00", "is_active": true },
    { "kind": "weekly_scoped", "group": "weekly",  "percent":  1,
      "scope": { "model": { "id": null, "display_name": "Fable" }, "surface": null },
      "resets_at": "2026-08-29T17:59:59.951965+00:00", "is_active": false }
  ],
  "extra_usage": { … }, "spend": { … }
}
```

Beobachtungen, die für das Format zählen:

- **`limits[]` ist die belastbare Achse**, nicht die Top-Level-Schlüssel. Es trägt
  `kind` (`session` / `weekly_all` / `weekly_scoped`), `group` und vor allem `scope`.
- **`scope` hat zwei Dimensionen**: `model` und `surface`. Ein Fenster kann auf eine
  Oberfläche skopiert sein, nicht auf ein Modell. Wer `scope` auf „Modell" flachklopft,
  etikettiert solche Fenster falsch.
- **`scope.model.id` war leer**, gefüllt war nur `display_name` (`"Fable"`). Der Abgleich
  gegen die Modell-Registry läuft also über den Anzeigenamen und braucht Normalisierung.
  Wird `id` künftig gefüllt, hat er Vorrang.
- **Die Top-Level-Schlüssel enthalten rotierende Codenamen**: `tangelo`, `iguana_necktie`,
  `nimbus_quill`, `cinder_cove`, `amber_ladder`, `juniper_tide`, `omelette_promotional`,
  `seven_day_cowork`, `seven_day_omelette`. Fast alle `null`. Eine feste Liste veraltet.
- **`seven_day_sonnet` und `seven_day_opus` sind auf diesem Tarif `null`** — für Sonnet und
  Opus existiert hier kein eigenes Wochenfenster. Die CLI hat Anzeigelogik dafür, die bei
  `max`/`team` greift; auf einem anderen Konto können sie also da sein.
- **`severity`** haben wir nur als `"normal"` gesehen, und die CLI parst das Feld nicht
  einmal. Als „erreicht"-Signal ist `status: "rejected"` aus dem Turn belastbarer.

Ein mit `claude setup-token` erzeugter Token (`sk-ant-oat01-…`) reicht für Inferenz, aber
nicht für diesen Endpunkt. Direkt gemessen antwortet er reproduzierbar mit **403**,
`oauth_scope_insufficient`, benötigter Scope `user:profile`. Ein neuer Setup-Token ändert
daran nichts. Ein Browser-Login kann den Scope liefern, ist für einen unbeaufsichtigten
Dienst wegen regelmäßig nötiger Re-Logins aber kein dauerhaftes Credential.

### 4.3 Einheiten und Fensterlängen

| | Header | Usage-API |
|---|---|---|
| `utilization` | Bruch `0..1` | Prozent `0..100` |
| Reset | Unix-Sekunden | RFC-3339-Zeitstempel |

Fensterlängen sind Konstanten (im CLI verdrahtet): `five_hour` = 18 000 s,
`seven_day` = 604 800 s.

---

## 5. Zuordnung: welche Fenster gelten für welches Modell

### 5.1 Das Header-Set ist modellabhängig

Ursprünglich über einen lokalen Forwarder, seit 2.1.252 direkt per Bun-Preload aufgezeichnet:

| Turn auf | gemeldete Fenster |
|---|---|
| `claude-sonnet-5` | `5h`, `7d` |
| `claude-opus-4-8` | `5h`, `7d` |
| `claude-haiku-4-5` | `5h`, `7d` |
| **`claude-fable-5`** | `5h`, `7d`, **`7d_oi`** |
| **`claude-fable-5-1`** | `5h`, `7d`, **`7d_oi`** |

Fable 5.0 und 5.1 wurden am selben Konto unmittelbar nacheinander mit CLI 2.1.258
vermessen. Beide Antworten trugen `7d_oi` mit identischem Reset und Füllstand. Das ist
damit ein Fenster der **Fable-Familie**, nicht ausschließlich der neuesten Modellversion.
Die Modellregistry pflegt diese Zugehörigkeit explizit; der Usage-Scope nennt beide Modelle.

Das Backend nennt pro Antwort genau die Fenster, die **diesen Request** begrenzen. Ein
skopiertes Fenster erscheint nur auf Turns des betreffenden Modells — dieselbe Lücke, die
der codex-Wrapper dokumentiert: aus einer Sonnet-Antwort ist der Fable-Stand nicht
ablesbar. Vollständig ist nur die Usage-API.

Kein Header trägt einen Modell- oder Scope-Namen. Die vollständige Liste:
`5h-{utilization,reset,status}`, `7d-…`, `7d_oi-…`, `overage-{status,disabled-reason}`,
`representative-claim`, `status`, `reset`, `fallback-percentage`.

### 5.2 Belastung, kostengleich gemessen

Zwei Läufe nach der Methodik aus §1. Kontrolle jeweils still.

**Lauf 1** — gleiche Turn-Zahl (der Fehler: nicht kostengleich):

| Phase | Turns | nominal | `5h` | `7d` | `scoped(Fable)` |
|---|---|---|---|---|---|
| Kontrolle (3 min) | – | – | 25 → 25 | 72 → 72 | 0 → 0 |
| Fable | 4 | $0.277 | 25 → **27** | 72 → **73** | 0 → **1** |
| Sonnet | 4 | $0.076 | 27 → 27 | 73 → 73 | 1 → 1 |

**Lauf 2** — kostengleich, Zielwert $0.28 je Phase:

| Phase | Turns | nominal | Output-Tokens | `5h` | `7d` | `scoped(Fable)` |
|---|---|---|---|---|---|---|
| Kontrolle (2 min) | – | – | – | 28 → 28 | 73 → 73 | 1 → 1 |
| Sonnet | 16 | $0.291 | 14 978 | 28 → **28** | 73 → 73 | 1 → 1 |
| Opus | 10 | $0.284 | 9 168 | 28 → **30** | 73 → 73 | 1 → 1 |

**Belegt:**

1. **Ein Modell-Turn belastet sein modellspezifisches Wochenfenster *und* das kontoweite,
   gleichzeitig.** Die Fable-Phase bewegte `scoped(Fable)`, `seven_day` und `five_hour`.
   Das skopierte Fenster ist ein Sub-Limit im Wochenbudget, kein eigener Topf.
2. **Opus verhält sich wie Fable** — bei gleichem nominalen Einsatz dieselben +2 Punkte.
3. **Ein Modell rührt das skopierte Fenster eines anderen nicht an.** Weder Sonnet noch
   Opus bewegten `scoped(Fable)`.
4. **`total_cost_usd` ist kein Maß für den Abo-Verbrauch.** Kostengleich gemessen bewegte
   Opus +2 Punkte und Sonnet null — bei *mehr* Output-Tokens auf der Sonnet-Seite. Die
   Kostenzahl rechnet API-Listenpreise, das Kontingent gewichtet Modelle anders. Wer
   Abo-Verbrauch schätzen will, darf nicht von `usage.cost` ausgehen.

**Nicht belegt:** ob Sonnet die kontoweiten Fenster in messbarem Umfang belastet. Bei
$0.29 blieb es unter der Auflösung. Strukturell nennen seine Antworten `5h` und `7d` als
regierende Fenster (§5.1); der Wrapper arbeitet mit der Annahme, dass Sonnet dieselben
Fenster belastet wie Opus, nur schwächer.

Ebenfalls offen: ob Sonnet/Opus `seven_day` bewegen. Über beide Phasen ($0.57) blieb es
unverändert; das Wochenfenster ist für diesen Einsatz zu groß.

---

## 6. Zwei Id-Räume — und wo der Join bricht

Der Turn-Kanal und die Usage-API benennen dieselben Fenster **verschieden**:

| Fenster | Turn (`rateLimitType` / Header) | API Top-Level | API `limits[]` | joinbar |
|---|---|---|---|---|
| 5 Stunden | `five_hour` / `5h` | `five_hour` | `kind: session` | ✅ |
| 7 Tage | `seven_day` / `7d` | `seven_day` | `kind: weekly_all` | ✅ |
| Fable | `seven_day_overage_included` / `7d_oi` | **fehlt** | `kind: weekly_scoped`, `scope.model: "Fable"` | ❌ |

Für die kontoweiten Fenster ist der Join zweifach abgesichert: identischer Schlüssel **und**
ein `resets_at`, der zwischen Top-Level und `limits[]` **bis auf die Mikrosekunde**
übereinstimmt.

Für das skopierte Fenster gibt es keine Brücke in den Daten:

- Die API hat dafür **keinen Top-Level-Schlüssel**. Die Codenamen-Slots sind es nicht:
  gemessen stand `nimbus_quill` auf `0.0`, während `weekly_scoped(Fable)` bereits auf `1`
  stand — verschiedene Fenster.
- `resets_at` hilft nicht: das Fable-Fenster setzt **zur selben Sekunde** zurück wie
  `seven_day`.
- Die einzige existierende Verbindung ist eine Labeltabelle im CLI-Binary
  (`seven_day_overage_included → "Fable 5 limit"`) — ein String in einem Bundle, der mit
  jeder CLI-Version wandern kann.

**Was der Wrapper stattdessen hat:** er weiß, welches Modell er gefahren hat. Das ist keine
Heuristik, sondern der Parameter, den er selbst gesetzt hat. Daraus folgt die Regel, die
ohne hartcodiertes Wissen auskommt:

```
claim ist ein kontoweiter Schlüssel   -> global/<claim>       (Id in beiden Quellen dieselbe)
sonst                                 -> model:<M>/<fenster>  (M = Modell dieses Turns)
```

Bedingung, die dabei gilt und im Code steht: **ein Modell hat höchstens ein skopiertes
Fenster.** Heute erfüllt. Wäre sie verletzt, ließen sich zwei skopierte Fenster desselben
Modells aus der Turn-Meldung nicht unterscheiden — dann `null` und Nachladen, nicht raten.

---

## 7. Capture-Proxy und minimale CLI-Probe

### 7.1 Was er liefern würde

Setzt man `ANTHROPIC_BASE_URL` der CLI auf einen lokalen Forwarder, der zu
`api.anthropic.com` weiterreicht, sieht man die Kontingent-Header jeder Antwort — also
**alle** geltenden Fenster mit Füllstand, pro Turn, kostenlos. Gemessen funktioniert das:
die CLI reicht ihre OAuth-Credentials auch an einen Loopback-Endpunkt durch, ein
eingespritzter Token ist nicht nötig.

Zusammen mit der Regel aus §6 wäre daraus eine zur API kompatible Gruppen-Zuordnung
ableitbar: das Header-Set nennt die geltenden Fenster, das Modell des Turns benennt den
Scope, die Ids sind dieselben wie in `rateLimitType`.

### 7.2 Warum ein Proxy vor allen normalen Turns zunächst verworfen wurde

**Der gemessene Gegenwert ist fast null.** Die Auflösung beträgt einen Prozentpunkt (§1),
im Leerlauf bewegt sich nichts (§1), und ein einzelner Turn liegt weit darunter. Ein
Hintergrund-Poll auf `/api/oauth/usage` im Minutentakt verliert gegenüber dem Header-Weg
**keine Information** — er liefert dieselbe Körnung, dazu `scope`, `reached` und die
Fenster, die der Turn gar nicht nennt. Und die eine Frage, für die man den Proxy vermuten
würde — welches Fenster dieser Turn belastet hat — beantwortet auch er nicht (§4.1).

**Der Preis ist eine Kategorie-Änderung.** Der Wrapper ist heute Aufseher eines
Subprozesses; das Modell-Traffic geht an ihm vorbei. Mit Proxy wird er
Man-in-the-Middle im credential-tragenden HTTPS-Pfad:

- Der **OAuth-Token liegt danach im Wrapper** — im Klartext, bei jedem Request. Heute fasst
  er ihn nie an. Ein vergessenes `log.exception(headers)` genügt für ein Credential-Leak.
- **Die Streaming-Arbeit verdoppelt sich**, dazu Request-Bodies bis 32 MiB durch den
  Speicher.
- **Fail-open gibt es nicht.** `ANTHROPIC_BASE_URL` wird beim Spawn gesetzt; ein Umschalten
  auf den direkten Endpunkt bei Störung ist für eine laufende Instanz unmöglich.

**Mit dem Prozess-Pool wird es schlechter.** Der Pool hält CLIs warm, deren Verbindungen
dann auf uns zeigen. Ein Proxy-Neustart (Reload, Worker-Wechsel, Relay-Exception) entwertet
alle warmen Instanzen auf einmal, und der Liveness-Check merkt es nicht: der Prozess lebt,
nur sein Upstream ist tot. Der Ausfall zeigt sich gebündelt beim nächsten `/clear` — als
Fehlerwelle mit Evictions und Neu-Spawns, also genau dem Verhalten, das der Pool verhindern
soll. Dazu: der Messproxy antwortet mit `Connection: close`; produktionsreif müsste er
keep-alive beidseitig korrekt führen, sonst kostet er einen TLS-Handshake pro Turn auf
einem Pfad, den der Pool gerade warm hält.

**Und ein Risiko, auf das wir keinen Einfluss haben:** ein CLI-Update, das OAuth für fremde
Base-URLs sperrt, legt dann nicht eine Kennzahl lahm, sondern **jeden Turn**. Dass Anthropic
Auth-Modi per Flag staffelt, ist bereits sichtbar — `--bare` liest OAuth ausdrücklich nicht.

Die entscheidende Asymmetrie:

```
Poll fällt aus   -> eine Kennzahl ist alt.   Turns laufen weiter.
Proxy fällt aus  -> jeder Turn schlägt fehl.
```

**Entscheidung: der Proxy bleibt Messwerkzeug, kein Bauteil.** Ohne ihn wüssten wir nicht,
dass das Header-Set modellabhängig ist, und hätten die Auflösung falsch eingeschätzt — für
diese Rolle war er richtig. Sollte je eine konkrete Frage ihn wieder brauchen, wäre der
einzig verantwortbare Zuschnitt ein **Kanarien-Bucket**: eine Pool-Gruppe über den Proxy,
der Rest direkt, damit ein Ausfall einen Bucket kostet und nicht den Dienst.

### 7.3 Neubewertung: separater Minimal-Probe über die echte CLI

Der Ausfall von `/api/oauth/usage` mit einem Setup-Token ändert die Abwägung: Statt alle
produktiven Turns durch einen Proxy zu führen, liest ein fail-open **Bun-Preload** die
Response-Header direkt am `fetch` der offiziellen CLI. Normale Turns aktualisieren den
Stand damit ohne Zusatzkosten; eine separate, alterslimitierte CLI führt nur bei alten
Daten einen minimalen echten Probe aus. Die offizielle CLI baut und authentifiziert den
Request, der Wrapper bildet keinen Claude-Code-Request nach.

Reproduzierbare kleinste Konfiguration mit CLI 2.1.198:

```sh
CLAUDE_CODE_OAUTH_TOKEN="$token" claude \
  -p 'Reply only: OK' \
  --model haiku \
  --system-prompt '' \
  --safe-mode \
  --tools '' \
  --output-format json \
  </dev/null
```

Die entscheidende Option ist **`--tools ''`**. Eine manuell gepflegte
`--disallowed-tools`-Liste erfasste nicht alle Builtins und ließ rund 11 000 Tokens
System-/Tool-Kontext stehen. `--tools ''` ist die dokumentierte Komplettabschaltung und
reduzierte denselben Haiku-Probe auf rund 155 normale Input-Tokens. Der verbleibende Block
ist zu klein bzw. nicht markiert für Prompt-Caching: `cache_read_input_tokens` und
`cache_creation_input_tokens` blieben auch über 100 identische Aufrufe beide null. Das ist
für seltene Probes erwünscht — es gibt keinen teuren Kaltstart nach Ablauf eines 5-Minuten-
Caches.

Gemessene Last der finalen Form (`Reply only: OK`, warm/kalt identisch):

| Modell | Aufrufe | Input gesamt | Output gesamt | Cache read/create | nominal pro Aufruf |
|---|---:|---:|---:|---:|---:|
| Haiku | 100 | 15 500 (155/Turn) | 7 913 (Ø 79/Turn) | 0 / 0 | $0.001112 |
| Fable | 100 | 16 500 (165/Turn) | 400 (4/Turn) | 0 / 0 | $0.002429 |

Die Header sind modellabhängig (§5.1): ein Haiku-Probe liefert `5h` und `7d`; für
`7d_oi` braucht es einen Fable-Probe. Der normale CLI-Output reicht **nicht** zum Ablesen:
`rate_limit_event` enthält bei `allowed` keinen Füllstand (§4.1). Der Capture muss daher die
`anthropic-ratelimit-unified-*`-**Response-Header** des echten CLI-Requests lesen.

Bei der Verbrauchsmessung bewegten 100 minimale Fable-Probes den sichtbaren 5h-Stand um
rund zwei Prozentpunkte. Bei Haiku lag die beobachtete Größenordnung bei ein bis drei
Punkten pro 100 Probes, war aber durch nachlaufende Verbuchung vorheriger Messphasen nicht
sauber isoliert. Diese Zahlen sind Kalibrierung, keine Abrechnung: die Header runden auf
einen Prozentpunkt und die Kontingentverbuchung lief sichtbar **zeitverzögert** nach. Ein
Probe darf deshalb nicht aus einem unmittelbar folgenden Read auf seinen Einzelverbrauch
schließen.

Für eine Umsetzung folgen daraus diese Randbedingungen:

1. Probe-Ergebnisse cachen und Probes hart rate-limiten; niemals pro Consumer-Request
   einen Turn auslösen.
2. Haiku für die kontoweiten Fenster verwenden; Fable nur deutlich seltener bzw. wenn der
   skopierte Stand tatsächlich gebraucht wird.
3. Jeder normale Nutzer-Turn aktualisiert über den Preload kostenlos die Header-Fenster,
   die für seine Requests gelten, und verschiebt damit den nächsten Probe.
4. Capture-Ausfall darf nur die Kennzahl altern lassen. Der normale Turn-Pool und die
   Probes bleiben auf direktem Upstream; kein Proxy liegt im credential-tragenden Pfad.
5. Header und Token niemals vollständig loggen. Für die Projektion reichen die bekannten
   `anthropic-ratelimit-unified-*`-Felder.

Geprüfte Sackgassen: `--bare` deaktiviert OAuth und endet mit `Not logged in`;
`--max-budget-usd 0` verhindert den Request vollständig; `count_tokens` akzeptiert den
Setup-Token, liefert aber keine Unified-Rate-Limit-Header; `--strict-mcp-config` hatte ohne
konfigurierte MCP-Server keinen messbaren Effekt.

---

## 8. Was daraus im Wrapper folgt

| Aufgabe | Quelle |
|---|---|
| **Karte** — welche Fenster, welchem Modell, wie voll | Turn-Header-Cache; `GET /wire/v1/usage` probiert bei alten Daten |
| **Alarm** — Limit greift jetzt | `limit_status`-Ereignis im Turn, nur wenn nicht `allowed` |
| **Abrechnung** — Tokens und Kosten pro Modell | `result.modelUsage` |

Das Turn-Ereignis trägt **keine Zahlen**, weil es keine hat. Es nennt Zustand, Fenster und
Reset — und ist damit der Anlass für den Konsumenten, den Stand über die API nachzuladen.
Präzise Attribution unten (Tokens pro Modell), grobe Füllstandsanzeige oben
(Kontingentfenster); die zwei bleiben getrennt, statt eines aus dem anderen zu gewinnen.

---

## 9. Nebenbefunde

- **`total_cost_usd` ist pro Prozess kumulativ** und überlebt `/clear`. Der Pool rechnet
  daraus ein Per-Turn-Delta ([`pool.py`](app/pool.py)); `tests/assumptions.py` prüft die
  Annahme, weil die Subtraktion sonst stumm `0.0` liefern würde.
- **`modelUsage` enthält Fremdarbeit.** Auf einem sonnet-Turn wies es zusätzlich
  `claude-haiku-4-5` mit 522 Input-Tokens aus — CLI-interne Nebenaufrufe. Die Kostenzahl
  eines Turns ist also nicht die Kostenzahl des Modell-Turns.
- **Pro CLI-Turn gehen zwei Requests nach oben**, nicht einer.
- **`--model opus` liefert auf dieser CLI `claude-opus-4-8`**, nicht Opus 5. Alle
  Opus-Messungen hier beziehen sich auf 4.8.
- Der Header `anthropic-ratelimit-unified-fallback-percentage` (gemessen `0.5`) taucht im
  stream-json nirgends auf.

# Meshtastic Routing Simulator

Symulator sieci Meshtastic do badań **routingu i skalowalności** z modelem łącza **Longley–Rice (ITM)** i warstwą RF opartą o **fading** (ITM liczony tylko przy starcie scenariusza).

## Architektura

- **FastAPI** — orchestrator, API REST, WebSocket śladu pakietów
- **React + Leaflet** — kreator scenariusza, heatmapa ITM, podgląd pakietów
- **meshtasticd** (Docker, shardy) — prawdziwy firmware Meshtastic w trybie `-s`
- **RadioGateway** — przechwytuje transmisje SimRadio (`SIMULATOR_APP`) z węzła, który faktycznie nadaje, modeluje łącze (ITM + fading) i wstrzykuje je odbiorcom jako `SIMULATOR_APP` (firmware traktuje to jako odbiór radiowy)
- **InfluxDB** — `packet_ota_tx`, `link_attempt`, `node_stats`
- **Grafana** — dashboardy na danych Influx (Flux), port 3000

## Wymagania

- Python 3.11+, Docker, Node.js 18+
- Kafelki SRTM w `data/srtm_cache/` (N*.hgt)
- Modele budynków LoD1 CityGML w `data/buildings_raw/` (dla modelu zabudowy)
- Obraz `meshtastic/meshtasticd:latest`

## Uruchomienie symulacji Dockerowej

### 1. Wymagania

```bash
docker pull meshtastic/meshtasticd:latest
# użytkownik uruchamiający backend musi mieć dostęp do docker.sock
```

### 2. Backend **bez** DRY_RUN

```bash
# Terminal 1 — cały stack (zalecane)
docker compose up --build

# LUB lokalnie na hoście:
DRY_RUN=false PYTHONPATH=backend uvicorn app.main:app --host 0.0.0.0 --port 8000
```

**Nie używaj** `DRY_RUN=true` — wtedy Start w UI nic nie uruchomi w Dockerze.

### 3. Frontend

```bash
# Terminal 2
cd frontend && npm run dev
```

Otwórz http://localhost:5173

**Grafana** (gdy używasz `docker compose up`): http://localhost:3000 — `admin` / `admin`. Datasource InfluxDB (Flux, org `meshtastic`, bucket `meshtastic_sim`) jest provisionowany z [docker/grafana/provisioning](docker/grafana/provisioning).

### Dashboardy (folder **Meshtastic**)

| Dashboard | Cel |
|-----------|-----|
| **Przegląd eksperymentu** | KPI na żywo: tempo TX/RF, delivery %, rebroadcast %, top pary TX→RX |
| **Routing i ścieżki** | Jeden `packet_id`: łańcuch TX, krawędzie RF, rebroadcasty, macierz dostaw |
| **Skalowalność** | Porównanie runów po `experiment_id` i `node_count` (w tym overlay O(N²)) |
| **Rodzaje wiadomości** | Rozkład `portnum` (SIMULATOR_APP, TELEMETRY_APP, …) |

Wspólne zmienne: `scenario_id`, `experiment_id` (multi + All). Na dashboardzie routingu dodatkowo `packet_id` (ostatnie ID z okna czasu).

**Workflow badawczy:** dla każdej liczby węzłów N ustaw unikalny `experiment_id` w panelu środowiska UI → uruchom symulację → na *Skalowalność* porównaj metryki; na *Routing* wybierz `packet_id` i prześledź rebroadcasty oraz skoki RF. Routing logiczny = firmware `meshtasticd`; Grafana pokazuje rebroadcasty, łańcuch TX i `link_attempt`, nie tabelę tras z firmware.

### 4. Kroki w UI

1. **Nowy scenariusz** — wybierz scenariusz z listy  
2. **Łącza ITM** → **Precompute ITM** — poczekaj aż status = `links_ready` (heatmapa)  
3. **Symulacja** → **Start (Docker)** — status przejdzie przez `starting` → `running` (10–30 s)  
4. **Pakiety** — ślad transmisji (po wysłaniu wiadomości z węzłów)

Jeśli Start „nic nie robi”: sprawdź czerwony komunikat błędu w kroku Symulacja lub `GET /api/scenarios/{id}/status`.

### 5. Sprawdzenie kontenerów

```bash
docker ps | grep sim-
docker logs sim-XXXXXXXX-shard-0
```

## Tryb dry-run (tylko ITM, bez meshtasticd)

```bash
export DRY_RUN=true
PYTHONPATH=backend uvicorn app.main:app --port 8000
```

## Scenariusz badawczy

1. Utwórz scenariusz (≥2 węzły, `hop_limit` per węzeł, rola ROUTER/CLIENT).
2. **Precompute ITM** — macierz `data/scenarios/{id}/link_matrix.npz`.
3. **Start** — shardy Docker + RadioGateway.
4. Opcjonalnie traffic: `scenarios/example_traffic.yaml` w `POST /start` z `{"traffic_config": "/app/scenarios/example_traffic.yaml"}`.

### Budynki LoD1

`agl_m` jest wysokością masztu nad terenem albo nad dachem. Węzeł wewnątrz obrysu
CityGML jest automatycznie montowany na dachu. Indeks 227 dostarczonych plików można
odtworzyć poleceniem:

```bash
PYTHONPATH=backend python scripts/import_buildings.py --force
```

Precompute odświeża indeks automatycznie, gdy zmieni się zestaw plików. W środowisku
scenariusza `building_model_enabled` steruje warstwą zabudowy, a
`fresnel_clearance_ratio` określa wymaganą wolną część pierwszej strefy Fresnela.

## Metryki Influx (routing)

| Measurement | Opis |
|-------------|------|
| `simulation_run` | Start runu: tagi `node_count`, `preset`; pola `fading_sigma_db`, `shard_size` |
| `packet_ota_tx` | Każda transmisja OTA (`packet_id`, `hop_limit`, `hop_start`, rebroadcast); tag `node_count` |
| `link_attempt` | Para (tx, rx): median PL, shadow, RSSI, delivered; tag `node_count` |
| `node_stats` | channel_util, air_util_tx, num_tx_relay, num_rx_dupe (planowane / README) |

Tagi `experiment_id`, `scenario_id` — filtry i porównania skalowalności (różne N, preset, fading).

## Model symulacji i `bootstrap_services.py`

- **Dokument metodologiczny:** [docs/simulation_model.md](docs/simulation_model.md) — wejścia, precompute ITM, runtime RF, trace, ograniczenia modelu.
- **`scripts/bootstrap_services.py`** — generator plików w `backend/app/services/` (UTF-8). **Źródłem prawdy w repozytorium jest aktualny kod w `backend/`**; skryptu używaj świadomie do odtworzenia plików po awarii lub bootstrapu od zera. Po ręcznych zmianach w serwisach nie uruchamiaj generatora nadpisującego te same pliki, chyba że scalisz diff ręcznie.

## Testy

```bash
PYTHONPATH=backend pytest tests/ -c pytest.ini
```

Test end-to-end na prawdziwym `meshtasticd` (Docker): linia A–B–C, gdzie A nie słyszy C —
wiadomość A musi dotrzeć do C wyłącznie przez retransmisję B, a konfiguracja węzłów musi
przetrwać restart po provisioningu:

```bash
PYTHONPATH=backend .venv/bin/python scripts/e2e_line_test.py
```

## Skalowanie 100+ węzłów

- `environment.shard_size` (domyślnie 10) — procesów `meshtasticd` na kontener
- `connectivity_threshold_dbm` — pomija pary poza zasięgiem mediany w runtime
- Pełny zapis `link_attempt` do Influx (N×(N-1) na broadcast)

## Zmienne środowiskowe

| Zmienna | Domyślnie |
|---------|-----------|
| `INFLUX_URL` | http://localhost:8086 |
| `INFLUX_TOKEN` | meshtastic-dev-token |
| `DRY_RUN` | false |
| `MESHTASTIC_TCP_HOST` | 127.0.0.1 |
| `SIM_MESH_BOOT_GRACE_S` | 8.0 — poczekaj po starcie shardów przed provisionem |
| `SIM_POST_PROVISION_SETTLE_S` | 2.0 — pauza po adminie przed ponownym TCP |
| `SIM_MESHTASTIC_TCP_TIMEOUT_S` | 120.0 — timeout oczekiwania na port TCP |
| `SIM_START_THREAD_JOIN_S` | 45.0 — join wątku startu przy `force_stop` |

## Powtarzalność eksperymentów

Ustaw `environment.random_seed` w scenariuszu, aby kolejne uruchomienia korzystały z tej
samej sekwencji fadingu. Po każdej zmianie scenariusza macierz ITM jest unieważniana i musi
zostać obliczona ponownie. Błędy wysyłania firmware nie są zastępowane syntetycznymi
broadcastami, dzięki czemu typ badanego ruchu pozostaje zgodny z harmonogramem.

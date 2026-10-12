# Meshtastic Routing Simulator

Symulator sieci Meshtastic do badań **routingu i skalowalności** z modelem łącza **Longley–Rice (ITM)** i warstwą RF opartą o **fading** (ITM liczony tylko przy starcie scenariusza).

## Architektura

- **FastAPI** — orkiestrator i API REST
- **React + Leaflet** — kreator scenariusza, macierz łączy ITM, podgląd pakietów
- **meshtasticd** (Docker, jeden kontener na węzeł) — prawdziwy firmware Meshtastic w trybie `-s`
- **RadioGateway** — przechwytuje transmisje SimRadio (`SIMULATOR_APP`) z węzła, który faktycznie nadaje, modeluje łącze (ITM + fading) i wstrzykuje je odbiorcom jako `SIMULATOR_APP` (firmware traktuje to jako odbiór radiowy)
- **InfluxDB** — `simulation_run`, `link_precompute`, `packet_ota_tx`, `link_attempt`
- **Grafana** — dashboardy na danych Influx (Flux), port 3000

## Wymagania

- Docker (z Docker Compose), Node.js 18+; Python 3.11+ tylko do testów i uruchamiania bez Dockera
- Dostęp do internetu przy obliczaniu łączy: teren (NMT) i budynki LoD1 pobierane są na bieżąco
  z usług GUGiK (tylko obszar Polski), bez ręcznego pobierania danych

## Uruchomienie

### 1. Backend, InfluxDB i Grafana

```bash
docker pull meshtastic/meshtasticd:latest
docker compose up --build
```

API: http://localhost:8000. Backend uruchamia kontenery węzłów przez `/var/run/docker.sock`
i łączy się z nimi po nazwie w sieci `meshtastic_sim` (`SIM_NODE_ACCESS=network`).
Po zmianie kodu backendu: `docker compose restart backend` (restart sam usuwa kontenery
węzłów pozostałe po poprzednim procesie).

Backend bez Dockera (np. do debugowania) — węzły są wtedy osiągane przez porty opublikowane
na 127.0.0.1 (`SIM_NODE_ACCESS=host`, domyślnie):

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
PYTHONPATH=backend .venv/bin/uvicorn app.main:app --port 8000
```

### 2. Frontend

```bash
cd frontend && npm install && npm run dev
```

Otwórz http://localhost:5173 (Vite przekazuje `/api` do backendu na porcie 8000; inny adres:
`BACKEND_URL=http://localhost:8001 npm run dev`).

**Grafana**: http://localhost:3000 — `admin` / `admin`. Datasource InfluxDB (Flux, org `meshtastic`, bucket `meshtastic_sim`) jest provisionowany z [docker/grafana/provisioning](docker/grafana/provisioning).

### Dashboardy (folder **Meshtastic**)

| Dashboard | Cel |
|-----------|-----|
| **Przegląd eksperymentu** | KPI na żywo: tempo TX/RF, delivery %, rebroadcast %, top pary TX→RX |
| **Routing i ścieżki** | Jeden `packet_id`: łańcuch TX, krawędzie RF, rebroadcasty, macierz dostaw |
| **Skalowalność** | Porównanie runów po `experiment_id` i `node_count` (w tym overlay O(N²)) |
| **Rodzaje wiadomości** | Rozkład `portnum` (SIMULATOR_APP, TELEMETRY_APP, …) |

Wspólne zmienne: `scenario_id`, `experiment_id` (multi + All). Na dashboardzie routingu dodatkowo `packet_id` (ostatnie ID z okna czasu).

**Workflow badawczy:** dla każdej liczby węzłów N ustaw unikalny `experiment_id` w panelu środowiska UI → uruchom symulację → na *Skalowalność* porównaj metryki; na *Routing* wybierz `packet_id` i prześledź rebroadcasty oraz skoki RF. Routing logiczny = firmware `meshtasticd`; Grafana pokazuje rebroadcasty, łańcuch TX i `link_attempt`, nie tabelę tras z firmware.

### Kroki w UI

1. **Środowisko** — utwórz lub wybierz scenariusz, ustaw preset i parametry modelu
2. **Węzły** — rozmieść co najmniej dwa węzły na mapie
3. **Łącza ITM** → **Oblicz łącza** — poczekaj, aż status zmieni się na „Łącza gotowe”
4. **Symulacja** → **Start** — status przejdzie przez „Uruchamianie” do „Symulacja działa”
5. **Pakiety** — ślad transmisji i prób radiowych

Kontenery węzłów: `docker ps --filter label=mesh-sim` (nazwy `sim-<scenariusz>-node-<n>`),
logi firmware: `docker logs sim-<scenariusz>-node-0`.

## Scenariusz badawczy

1. Utwórz scenariusz (≥2 węzły, `hop_limit` i rola per węzeł). Dostępne są wszystkie role
   firmware, które `meshtasticd` 2.7.15 zachowuje po restarcie; przestarzałe `REPEATER` i
   `ROUTER_CLIENT` firmware zapisuje jako `CLIENT`, więc symulator ich nie oferuje.
2. **Precompute ITM** — macierz `data/scenarios/{id}/link_matrix.npz`.
3. **Start** — kontenery węzłów + RadioGateway.
4. Opcjonalnie traffic: `scenarios/example_traffic.yaml` w `POST /start` z `{"traffic_config": "/app/scenarios/example_traffic.yaml"}`.

### Teren i budynki (dane GUGiK)

Precompute pobiera dane sam, tylko dla obszaru scenariusza (`backend/app/services/propagation/gugik.py`):

- **Teren** — numeryczny model terenu GUGiK (siatka 1 m, sam grunt, bez roślinności i zabudowy)
  z usługi `services.gugik.gov.pl/nmt` (`GetHByPointList`). Profil ITM próbkowany jest co
  `terrain_profile_step_m` (30 m). Przy wielu węzłach na małym obszarze pobierana jest siatka
  wysokości zamiast osobnych profili. Wysokości są trzymane tylko w pamięci procesu.
- **Budynki** — modele 3D LoD1 z GUGiK (CC BY 4.0) w paczkach powiatowych. Powiaty, przez które
  biegną łącza, wskazuje usługa PRG (WFS z granicami powiatów); paczka jest pobierana do pliku
  tymczasowego, czytana prosto do indeksu `data/buildings/buildings.sqlite` i usuwana. Indeks ma
  limit `buildings_index_max_mb` (500 MB) — po jego przekroczeniu usuwane są najdawniej
  używane powiaty.

Podgląd indeksu i ręczne odświeżenie powiatu (np. przed pracą offline):

```bash
PYTHONPATH=backend python scripts/import_buildings.py            # lista powiatów
PYTHONPATH=backend python scripts/import_buildings.py 1863 1816  # pobierz / odśwież
```

`agl_m` jest wysokością masztu nad terenem albo nad dachem. Węzeł wewnątrz obrysu budynku
jest automatycznie montowany na dachu. W środowisku scenariusza `building_model_enabled`
steruje warstwą zabudowy, a `fresnel_clearance_ratio` określa wymaganą wolną część pierwszej
strefy Fresnela.

## Metryki Influx (routing)

| Measurement | Opis |
|-------------|------|
| `simulation_run` | Start runu: tagi `node_count`, `preset`; pola `fading_sigma_db`, model budynków |
| `packet_ota_tx` | Każda transmisja OTA (`packet_id`, `hop_limit`, `hop_start`, rebroadcast); tagi `tx_node`, `origin_node`, `portnum`, `node_count` |
| `link_attempt` | Para (tx, rx): median PL, shadow, RSSI, delivered; tag `node_count` |

Tagi `experiment_id`, `scenario_id` — filtry i porównania skalowalności (różne N, preset, fading).

## Model symulacji

Dokument metodologiczny: [docs/simulation_model.md](docs/simulation_model.md) — wejścia,
precompute ITM, runtime RF, trace, ograniczenia modelu.

## Testy

```bash
.venv/bin/pytest
```

Test end-to-end na prawdziwym `meshtasticd` (Docker): linia A–B–C, gdzie A nie słyszy C —
wiadomość A musi dotrzeć do C wyłącznie przez retransmisję B, a konfiguracja węzłów musi
przetrwać restart po provisioningu:

```bash
PYTHONPATH=backend .venv/bin/python scripts/e2e_line_test.py
```

## Skalowanie 100+ węzłów

- Każdy węzeł to osobny kontener; węzły są konfigurowane równolegle (`SIM_PROVISION_WORKERS`).
- `connectivity_threshold_dbm` — pomija pary poza zasięgiem mediany w runtime.
- Pełny zapis `link_attempt` do Influx (N×(N-1) na broadcast).

## Kampania walidacyjna (rozdział 4 pracy)

Porównanie symulatora z siecią rzeczywistą na danych z [lorastats.pl](https://lorastats.pl/API).
Kryteria wyboru węzłów i par opisuje [docs/validation_lorastats.md](docs/validation_lorastats.md),
przebieg i wyniki kampanii z 8.10.2026 [docs/test_campaign.md](docs/test_campaign.md), a jej
powtórzenie po przeglądzie metodyki (12.10.2026) [docs/test_campaign_v3.md](docs/test_campaign_v3.md).

```bash
export PYTHONPATH=backend:scripts/validation
D=data/lorastats/2026-10-08            # zrzut API: <region>_PacketsRaw.json, <region>_Nodes.json

# 1. dekodowanie raportów odbioru (tylko raporty po kalibracji)
.venv/bin/python scripts/validation/lorastats_dataset.py $D --since 2026-10-07T17:35
# 2. węzły z dokładną pozycją, obserwacje łączy, klastry
.venv/bin/python scripts/validation/lorastats_pairs.py $D/dataset_2026-10-07T1735.json
# 3. ocena modelu łączy (T1), bez uruchamiania symulacji
.venv/bin/python scripts/validation/link_eval.py $D/clusters_2026-10-07T1735.json

# 4. plan przebiegów: routing | medium | sweep | scale
.venv/bin/python scripts/validation/make_plan.py routing $D/clusters_2026-10-07T1735.json -o data/campaign/plan_routing.json
# 5. kilka backendów na hoście (bez Dockera dla backendu) i kampania przez REST API
scripts/validation/backends.sh 8101 8102 8103
.venv/bin/python scripts/validation/campaign.py data/campaign/plan_routing.json \
    --out data/campaign --backends 8101,8102,8103
# 6. analiza (T2–T6) i rysunki do latex/rysunki/
.venv/bin/python scripts/validation/routing_eval.py data/campaign $D/clusters_2026-10-07T1735.json
.venv/bin/python scripts/validation/figures.py $D/links_2026-10-07T1735.json data/campaign/report.json
```

Przebiegi już zapisane w `data/campaign/runs/` są pomijane, więc kampanię można wznowić.
Kolejne backendy w trakcie kampanii uruchamia `scripts/validation/backend_noclean.py <port>`
(zwykły backend przy starcie usuwa kontenery wszystkich instancji).

## Praca dyplomowa

Źródła LaTeX są w `latex/` (plik główny `Praca_inzynierska.tex`, rozdziały w `latex/rozdzialy/`).

```bash
cd latex && latexmk        # PDF w latex/build/
```

## Zmienne środowiskowe

| Zmienna | Domyślnie | Opis |
|---------|-----------|------|
| `INFLUX_URL` / `INFLUX_TOKEN` | http://localhost:8086 / meshtastic-dev-token | InfluxDB |
| `MESHTASTIC_IMAGE` | meshtastic/meshtasticd:latest | obraz firmware |
| `SIM_NODE_ACCESS` | host | `network` (backend w compose) albo `host` (backend na hoście) |
| `SIM_DOCKER_NETWORK` | meshtastic_sim | sieć kontenerów w trybie `network` |
| `SIM_PROVISION_WORKERS` | 4 | ile węzłów konfigurować równolegle |
| `SIM_MESHTASTIC_TCP_TIMEOUT_S` | 120 | oczekiwanie na API węzła |
| `SIM_MESH_CONNECT_TIMEOUT_S` | 90 | oczekiwanie na handshake Meshtastic |
| `SIM_TRAFFIC_WARMUP_S` | 5 | pauza między startem węzłów a ruchem |
| `TERRAIN_PROFILE_STEP_M` | 30 | odstęp próbek profilu terenu dla ITM |
| `GUGIK_WORKERS` | 6 | równoległe zapytania do usług GUGiK |
| `BUILDINGS_INDEX_MAX_MB` | 500 | limit indeksu budynków LoD1 |

## Powtarzalność eksperymentów

Ustaw `environment.random_seed` w scenariuszu, aby kolejne uruchomienia korzystały z tej
samej sekwencji fadingu. Po każdej zmianie scenariusza macierz ITM jest unieważniana i musi
zostać obliczona ponownie. Błędy wysyłania firmware nie są zastępowane syntetycznymi
broadcastami, dzięki czemu typ badanego ruchu pozostaje zgodny z harmonogramem.

# Model symulacji (metodologia)

Ten dokument opisuje **co symulator robi**, **z czego wynika**, i **czego świadomie nie modeluje**. Definicja „poprawności metodologicznej” w tym projekcie: **spójny, jawny model badawczy** — nie pełna emulacja stosu RF Meshtastic.

## Wejścia

- Scenariusz: węzły (pozycje, anteny, szum, role, `hop_limit`), środowisko LoRa (`EnvironmentConfig`), próg łączności, parametry ITM.
- Dane terenu: SRTM w `data/srtm_cache/` używane do profilu ścieżki i strat Longley–Rice (ITM).
- Budynki: CityGML LoD1 w `data/buildings_raw/`, lokalnie indeksowane w SQLite/RTree.

`agl_m` oznacza wysokość masztu nad powierzchnią montażu. Punkt leżący w obrysie
budynku jest automatycznie klasyfikowany jako montaż dachowy, więc wysokość anteny to
`wysokość terenu SRTM + measuredHeight + agl_m`. Poza obrysem jest to
`wysokość terenu SRTM + agl_m`. Własny budynek anteny dachowej nie jest przeszkodą.

## Precompute (ITM)

Dla każdej uporządkowanej pary (TX, RX):

1. Budowany jest profil terenu i dystans 3D.
2. Liczona jest strata terenu `terrain_loss_db` (ITM).
3. Jeśli włączono model budynków, wyszukiwane są bryły LoD1 w korytarzu pierwszej
   strefy Fresnela. Dla budynków naruszających wymagany prześwit (domyślnie 60%)
   liczona jest strata knife-edge. Przy wielu przeszkodach wybierana jest dominująca
   strata — nie sumuje się niezależnych krawędzi.
4. `path_loss_median_db = terrain_loss_db + building_loss_db`.
5. **Medianowy RSSI** (bez fadingu):  
   `RSSI = P_tx + G_tx + G_rx − PL` — ten sam wzór co w module `app.services.rf_link_model` (`compute_rssi_db`, `shadow_db = 0`).
6. Graf sąsiedztwa: krawędź TX→RX jeśli `median_rssi_db[tx,rx] ≥ connectivity_threshold_dbm`.

Wysokości bezwzględne CityGML są w PL-EVRF2007-NH, a SRTM w EGM96. Aby ich nie
mieszać, model wykorzystuje z CityGML względne `measuredHeight` i osadza dach na
lokalnym profilu SRTM. Jest to świadome uproszczenie zapewniające jeden układ pionowy.

Macierz i sąsiedztwo są **zamrożone** na czas symulacji (brak zmian topologii w runtime).

## Runtime RF (`RadioGateway`)

1. W trybie `meshtasticd -s` (SimRadio) każda transmisja radiowa węzła jest raportowana jego
   klientowi TCP jako pakiet `SIMULATOR_APP`. Tylko te pakiety są traktowane jako „TX na eterze”;
   nadajnikiem jest węzeł, którego połączenie zgłosiło pakiet (a nie pole `from`, które przy
   retransmisji wskazuje pierwotnego autora). Zwykłe odbiory nie są transmisjami.
2. Dla każdego sąsiada z precompute:  
   `RSSI = P_tx + G_tx + G_rx − PL_median + X`, gdzie `X ~ N(0, σ²)` i `σ = fading_sigma_db`.
3. **SNR** jest liczony jako `min(RSSI − noise_floor_rx, 12 dB)` — nasycenie jak w odbiorniku
   LoRa. Firmware wylicza z SNR opóźnienie retransmisji, więc nienasycona wartość (np. 70 dB
   dla bliskich węzłów) blokowała kolejkę nadawczą na dziesiątki minut.
4. **Dekodowanie** (uproszczenie): `RSSI ≥ sensitivity` (LoRa z presetu) — jawny wybór RSSI zamiast SNR przy progu; uzasadnienie: spójność z jednym progiem odbioru w symulatorze.
5. Dla odbiorców z `delivered == true` pakiet jest wstrzykiwany przez `ToRadio` jako `SIMULATOR_APP`
   z RSSI/SNR z modelu — firmware odbiorcy rozpakowuje go jak odbiór z układu LoRa. Inny `portnum`
   firmware potraktowałby jako wiadomość od własnego klienta i nadał jako swoją.

Firmware każdego węzła jest konfigurowany zgodnie z modelem RF: region EU_868, preset modemu
(lub jawne SF/BW/CR dla `custom`), `hop_limit`, rola i stała pozycja. Konfiguracja jest
weryfikowana po restarcie węzła; rozbieżność przerywa start symulacji.

Routing, kolejki i logika mesh są w **firmware**; Python zastępuje tylko **medium RF** między węzłami.

## Trace i metryki

- `simulation_run` — jednorazowy punkt przy starcie runu (`node_count`, `preset`, fading, model budynków).
- `link_precompute` — wszystkie skierowane pary, również odcięte przez próg runtime;
  zawiera osobne straty terenu i budynków oraz wynikowy RSSI medianowy.
- `packet_ota_tx` — każda uchwycona transmisja (tag `portnum`, pola `hop_start` / `hop_limit`, rebroadcast); tag `node_count` z bieżącego runu.
- `link_attempt` — próba z RSSI/SNR, `terrain_loss_db`, `building_diffraction_db`,
  `blocked_buildings_count`, `fresnel_clearance_m`, identyfikatorem dominującego
  budynku i rokiem danych.
- `PacketOtaRx` w pamięci API — po udanym wstrzyknięciu do odbiorcy.

Dashboardy Grafana (folder Meshtastic) czytają te serie w Flux; routing w UI = sekwencja TX + krawędzie RF, nie tabela tras firmware.

## Czego model nie robi

- Kolizje CSMA, backoff, capture effect, zajętość kanału w czasie.
- Zmiana topologii w czasie (ruch węzłów, pogoda).
- Pełna zgodność z implementacją odbioru w chipie (tylko prosty próg RSSI).
- Wielokrawędziowa procedura Deygout/ITU-R P.526; obecna wersja wybiera jedną
  dominującą przeszkodę i jest przeznaczona do późniejszej kalibracji pomiarami.
- Odbicia od elewacji, dyfrakcja boczna, kaniony uliczne i przenikanie do wnętrz.
- Symulacja wieloskokowa zastępująca firmware (wieloskok jest w `meshtasticd`, RF między hopami nadal przez ten model „skok po skoku” z gateway).

## Reprodukowalność

Pole `environment.random_seed` steruje generatorem fadingu. Ustawienie tego samego seeda,
scenariusza i harmonogramu pozwala odtworzyć tę samą sekwencję losowań RF. Brak wartości
oznacza przebieg niedeterministyczny i powinien być używany tylko do eksploracji.

Nieudane wysłanie przez firmware jest rejestrowane w logu i nie jest zastępowane innym
typem pakietu. W szczególności DM i traceroute nigdy nie są zamieniane na broadcast, ponieważ
zafałszowałoby to wyniki routingu.

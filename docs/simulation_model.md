# Model symulacji (metodologia)

Ten dokument opisuje **co symulator robi**, **z czego wynika**, i **czego świadomie nie modeluje**. Definicja „poprawności metodologicznej” w tym projekcie: **spójny, jawny model badawczy** — nie pełna emulacja stosu RF Meshtastic.

## Wejścia

- Scenariusz: węzły (pozycje, anteny, szum, role, `hop_limit`), środowisko LoRa (`EnvironmentConfig`), próg łączności, parametry ITM.
- Teren: numeryczny model terenu GUGiK (NMT, siatka 1 m, wysokości gruntu bez roślinności
  i zabudowy), pobierany na żądanie z usługi wysokości GUGiK do profilu ścieżki ITM.
- Budynki: CityGML LoD1 GUGiK, pobierane na żądanie paczkami powiatowymi (powiaty wskazuje
  usługa PRG) i indeksowane w SQLite/RTree; surowe paczki nie zostają na dysku.

Teren bez pokrycia (SRTM jest modelem powierzchni — radar odbija się od koron drzew i dachów)
jest potrzebny, żeby budynki LoD1 nie były liczone dwukrotnie: raz w podniesionym terenie,
raz jako przeszkody.

`agl_m` oznacza wysokość masztu nad powierzchnią montażu. Punkt leżący w obrysie
budynku jest automatycznie klasyfikowany jako montaż dachowy, więc wysokość anteny to
`wysokość terenu NMT + measuredHeight + agl_m`. Poza obrysem jest to
`wysokość terenu NMT + agl_m`. Własny budynek anteny dachowej nie jest przeszkodą.

## Precompute (ITM)

Dla każdej uporządkowanej pary (TX, RX):

1. Budowany jest profil terenu (próbka co `terrain_profile_step_m` = 30 m wzdłuż prostej
   w układzie PL-1992, co najmniej 50 odcinków) i dystans geodezyjny. Gdy siatka wysokości nad
   obszarem scenariusza ma mniej punktów niż wszystkie profile razem, pobierana jest siatka
   o tym samym oczku i profile są z niej interpolowane. Dystans poniżej 1 m jest traktowany jak 1 m.
2. Liczona jest strata terenu `terrain_loss_db` (ITM), jako średnia z obu kierunków:
   itmlogic nie jest dokładnie wzajemny, a strata propagacyjna jest.
3. Jeśli włączono model budynków, wyszukiwane są bryły LoD1 w korytarzu pierwszej
   strefy Fresnela. Dla budynków naruszających wymagany prześwit (domyślnie 60%)
   liczona jest strata knife-edge. Przy wielu przeszkodach wybierana jest dominująca
   strata — nie sumuje się niezależnych krawędzi.
4. `path_loss_median_db = terrain_loss_db + building_loss_db + clutter_loss_db + link_shadowing_db`:
   - `clutter_loss_db` — stała strata otoczenia ze scenariusza (zabudowa i roślinność spoza
     danych LoD1, anteny w budynkach, kable);
   - `link_shadowing_db ~ N(0, σ²)`, `σ = link_shadowing_sigma_db` — trwałe cieniowanie łącza,
     losowane raz na parę węzłów (ta sama wartość w obu kierunkach) z seedem
     `link_shadowing_seed`. Opisuje różnice między łączami, których ITM nie widzi; fading w
     runtime opisuje zmienność kolejnych transmisji na tym samym łączu;
   - opcjonalnie strata instalacji węzła `N(0, node_installation_sigma_db²)` (losowana raz na
     węzeł, doliczana do wszystkich jego łączy) i odsetek par zablokowanych
     `link_blocked_fraction` (para bez krawędzi) — oba z tym samym seedem.

   Wartości z kalibracji: `docs/test_campaign_v3.md` (wcześniejsze: `docs/validation_lorastats.md`).
5. **Medianowy RSSI** (bez fadingu):  
   `RSSI = P_tx + G_tx + G_rx − PL` (domyślnie `P_tx` = 22 dBm, typowe SX1262) — ten sam wzór co w module `app.services.rf_link_model` (`compute_rssi_db`, `shadow_db = 0`).
6. Graf sąsiedztwa: krawędź TX→RX jeśli `median_rssi_db[tx,rx] ≥ connectivity_threshold_dbm`.

NMT udostępniany przez usługę ma wysokości w PL-KRON86-NH, a CityGML w PL-EVRF2007-NH
(różnica rzędu centymetrów). Model wykorzystuje z CityGML względne `measuredHeight` i osadza
dach na tym samym gruncie NMT, z którego zbudowany jest profil ITM, więc całe łącze ma jedno
źródło wysokości gruntu.

Macierz i sąsiedztwo są **zamrożone** na czas symulacji (brak zmian topologii w runtime).

## Runtime RF (`RadioGateway`)

1. W trybie `meshtasticd -s` (SimRadio) każda transmisja radiowa węzła jest raportowana jego
   klientowi TCP jako pakiet `SIMULATOR_APP`. Tylko te pakiety są traktowane jako „TX na eterze”;
   nadajnikiem jest węzeł, którego połączenie zgłosiło pakiet (a nie pole `from`, które przy
   retransmisji wskazuje pierwotnego autora). Zwykłe odbiory nie są transmisjami.
2. Dla każdego sąsiada z precompute:  
   `RSSI = P_tx + G_tx + G_rx − PL_median + X`, gdzie `X ~ N(0, σ²)` i `σ = fading_sigma_db`
   (domyślnie 1 dB — rozrzut RSSI kolejnych odbiorów na łączach stacjonarnych w danych
   lorastats.pl; wcześniej przyjmowane 8 dB).
3. **SNR** jest liczony jako `min(RSSI − N, 12 dB)`, gdzie szum odbiornika
   `N = −174 + 10·log10(BW) + NF` (NF = 6 dB; −114 dBm dla 250 kHz) — ten sam, z którego wynika
   czułość `N + SNR_min(SF)`; węzeł może go nadpisać polem `noise_floor_dbm`. Nasycenie na 12 dB
   odpowiada odbiornikowi LoRa. Firmware wylicza z SNR opóźnienie retransmisji, więc nienasycona wartość (np. 70 dB
   dla bliskich węzłów) blokowała kolejkę nadawczą na dziesiątki minut.
4. **Dekodowanie** (uproszczenie): `RSSI ≥ sensitivity` (LoRa z presetu) — jawny wybór RSSI zamiast SNR przy progu; uzasadnienie: spójność z jednym progiem odbioru w symulatorze.
5. Dla odbiorców z `delivered == true` pakiet jest wstrzykiwany przez `ToRadio` jako `SIMULATOR_APP`
   z RSSI/SNR z modelu — firmware odbiorcy rozpakowuje go jak odbiór z układu LoRa. Inny `portnum`
   firmware potraktowałby jako wiadomość od własnego klienta i nadał jako swoją.

### Kolizje (`collisions_enabled`, domyślnie włączone)

Model za Borem i in. (MSWiM '16, LoRaSim), moduł `app.services.radio_medium`:

1. **Czas w eterze** wg Semtech AN1200.13: preambuła 16 symboli (jak w firmware Meshtastic),
   nagłówek jawny, CRC, LDRO dla symbolu > 16 ms; długość = dane + 16 B nagłówka pakietu.
2. **Odbiór jest rozstrzygany po zakończeniu pakietu** (plus 50 ms zapasu na zgłoszenia innych
   węzłów) i dopiero wtedy wstrzykiwany — tak jak prawdziwy odbiornik oddaje pakiet po odebraniu
   całości. `SimRadio` (bez `USERPREFS_SIMRADIO_EMULATE_COLLISIONS`) przetwarza wstrzyknięty
   pakiet natychmiast, więc opóźnienie się nie dubluje.
3. Pakiet `p` u odbiorcy `r` jest tracony, gdy:
   - `RSSI < czułość` — `weak`;
   - `r` sam nadawał w czasie trwania `p` (półdupleks) — `half_duplex`;
   - nakłada się z nim pakiet `q`, którego moc u `r` jest mniejsza od mocy `p` o mniej niż
     `capture_threshold_db` (6 dB), chyba że `q` skończył się w pierwszych 16 − 5 = 11 symbolach
     preambuły `p` — `collision`. Reguła jest parami (pakiet przeciw pakietowi), jak w LoRaSim;
     zakłócenia z węzłów spoza sąsiedztwa `r` (poniżej `connectivity_threshold_dbm`) są pomijane.
4. **CAD**: prawdziwe radio (SX126x) przed nadaniem po losowym opóźnieniu z okna rywalizacji
   wykonuje CAD i przy zajętym kanale losuje opóźnienie ponownie. `SimRadio` tego nie robi
   (kanał nigdy nie jest „zajęty”), więc gateway przesuwa start transmisji węzła, który słyszy
   sygnał nakładający się na planowany czas nadawania (RSSI ≥ czułość), za jego koniec plus
   losowe `0…2^CWmin − 1` slotów (CWmin = 3, slot = 2,5 symbolu + 7,6 ms, jak
   `RadioInterface::getTxDelayMsec`) — również sygnał już odroczony na później, bo jego węzeł
   zrobi CAD w chwili startu. CAD nie wykrywa transmisji startującej bliżej niż jeden slot
   (oba węzły widzą wolny kanał i kolidują). Własna, trwająca transmisja węzła zawsze blokuje
   jego radio: firmware zgłasza kolejny pakiet, nie wiedząc, że poprzedni został odroczony.
   Sprawdzenie i rejestracja transmisji są atomowe (`RadioMedium.place`).
5. **Anulowanie retransmisji**: prawdziwy węzeł przy zajętym kanale trzyma pakiet w kolejce
   i anuluje retransmisję, gdy w tym czasie odbierze jej duplikat (`perhapsCancelDupe`;
   nie dotyczy ról ROUTER i ROUTER_LATE). Firmware w SimRadio zgłosił pakiet już jako nadany,
   więc odroczoną retransmisję anuluje medium (`RadioMedium.cancel_pending`, pomiar
   `packet_ota_cancel`).
6. Pakiet identyfikuje para (nadawca pierwotny, id) — kontenery `meshtasticd` potrafią
   wylosować to samo id. Chwila nadania to moment odebrania zgłoszenia z TCP (opóźnienie obsługi
   w kolejce biblioteki jest mierzone: `report_delay_ms`).
7. Przyczyna utraty trafia do `link_attempt` (tag `loss_reason`) i do widoku pakietów.

Bez kolizji (`collisions_enabled = false`) odbiór też następuje po czasie w eterze, tylko bez
strat kolizyjnych i CAD — ablacja zmienia wyłącznie straty.

### Start i rozgrzewka

Gateway działa już podczas konfiguracji węzłów. Okresy NodeInfo i pozycji pozostają domyślne
(firmware: 3 h i 15 min). Przed ruchem z harmonogramu każdy węzeł rozgłasza swoje NodeInfo
w losowej chwili okna `max(sim_nodeinfo_window_s, sim_nodeinfo_gap_s × N)` (20 s, 2 s), po
czym następuje przerwa `sim_traffic_warmup_s` (10 s) — sieć zaczyna w stanie, w którym węzły
się znają, jak sieć rzeczywista.

Weryfikacja na `meshtasticd` (ukryty węzeł: A i C się nie słyszą, u B różnica mocy 5,7 dB,
8 rund jednoczesnych broadcastów): z kolizjami B traci oba pakiety w 6 z 8 rund (w 2 rundach
losowe opóźnienie nadania w firmware rozsunęło je w czasie), bez kolizji odbiera wszystkie.

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

- Synchronizacja preambuły w szczegółach (tylko reguła 11 symboli), częściowa ortogonalność
  różnych SF (wszystkie węzły mają ten sam preset), zmienność mocy zakłócenia w czasie pakietu;
  CAD wykrywa sygnał przez cały czas jego trwania, nie tylko preambułę.
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

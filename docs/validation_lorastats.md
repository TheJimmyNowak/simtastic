# Walidacja symulatora na danych lorastats.pl

Porównanie macierzy łączy ITM i routingu symulatora z rzeczywistą siecią Meshtastic
w Polsce. Dane: API [lorastats.pl](https://lorastats.pl/API) (ostatnie 24 h, pobrane
7.10.2026), wszystkie regiony serwisu.

## Dane wejściowe

- `PacketsRaw` (surowe `MeshPacket`, dekodowane protobufem), `PacketReports`,
  `PacketsParsed`, `Nodes` — ok. 1,07 mln raportów odbioru od 360 bramek MQTT.
- Preset radiowy klastra ustalony z hasha kanału: Warszawa, Rzeszów, Lublin, Szczecin —
  `MediumFast`; „Zachód” (Zielona Góra, Poznań) — `ShortSlow` (SF8/250 kHz, w symulatorze
  jako `custom`); Siedlce — `LongFast`.

### Precyzja pozycji

Węzły rozgłaszają pozycję skwantowaną do `precision_bits` (komórka siatki
2^(32−bits) × 1e-7°, węzeł podaje jej środek). Rozkład wśród 1389 węzłów z pozycją:

| bity | 13 | 14 | 15 | 16 | 17 | 18 | 19 | 32 | inne |
|---|---|---|---|---|---|---|---|---|---|
| węzły | 386 | 98 | 280 | 103 | 99 | 57 | 92 | 258 | 16 |

Do testów wybrano tylko węzły, które **we wszystkich** raportach podają 32 bity, są
stacjonarne (rozrzut < 30 m), leżą w Polsce i nie mają „okrągłych” współrzędnych — 149
węzłów, w tym 34 bramki. Uwaga: 62 z 72 węzłów w klastrach ma pozycję wpisaną ręcznie
(`location_source = MANUAL`), więc pełna precyzja zapisu nie gwarantuje poprawności.
Wysokość anteny: z raportowanej wysokości minus SRTM, gdy wynik mieści się w 1,5–80 m
(40 z 72 węzłów), w przeciwnym razie 6 m.

### Obserwacje łączy

- **Łącze istnieje**: bramka odebrała pakiet z `hop_start == hop_limit` i `relay_node`
  równym ostatniemu bajtowi nadawcy (RSSI/SNR z raportu) albo węzeł wymienia sąsiada
  w `NEIGHBORINFO_APP`.
- **Brak łącza bezpośredniego**: bramka zaraportowała ≥ 5 pakietów nadawcy wyłącznie po
  retransmisji. Bramka wysyła do MQTT pierwszą odebraną kopię, a oryginalna transmisja
  jest zawsze pierwsza, więc brak kopii bezpośredniej oznacza, że jej nie zdekodowała.
- Odrzucono bramki niebędące obserwatorami radiowymi (< 1% odbiorów bezpośrednich lub
  > 50% pakietów z `hop_limit = 0`, np. `849b831c`, `69119bc8`, `1ba13fc4`, `b2a796a8`,
  `b03c4f38`) — ich „brak łącza” nie jest dowodem.
- Pary bliższe niż 200 m (urządzenia w jednym domu) i dalsze niż 60 km pominięto.

## Klastry

Rozłączne grupy ≤ 10 węzłów, dobierane zachłannie według liczby obserwacji między nimi.
Scenariusze w symulatorze: `lorastats-k0` … `lorastats-k7` (domyślne parametry: 27 dBm,
anteny 2/2 dBi, σ = 8 dB, model budynków włączony — żaden klaster nie leży w obszarze
z danymi CityGML).

| klaster | rejon | preset | węzły | bramki | łącza obserwowane | brak łącza |
|---|---|---|---|---|---|---|
| k0 | Zielona Góra / Bory Dolnośląskie | ShortSlow | 10 | 4 | 16 | 28 |
| k1 | Rzeszów | MediumFast | 10 | 2 | 6 | 5 |
| k2 | Siedlce | LongFast | 8 | 2 | 7 | 0 |
| k3 | Lublin | MediumFast | 10 | 3 | 1 | 20 |
| k4 | Szczecin | MediumFast | 10 | 1 | 0 | 9 |
| k5 | Poznań | ShortSlow | 8 | 1 | 0 | 7 |
| k6 | Nowa Sól | ShortSlow | 9 | 2 | 1 | 9 |
| k7 | Warszawa | MediumFast | 7 | 2 | 0 | 9 |

(liczby przed odfiltrowaniem bramek niebędących obserwatorami radiowymi)

## Wyniki: łącza

87 par po filtracji: 31 z obserwowanym łączem, 56 bez łącza bezpośredniego.

| metryka | wynik |
|---|---|
| AUC marginesu `median_rssi − sensitivity` | **0,80** |
| klasyfikacja `median_rssi ≥ sensitivity` | TPR 0,94, TNR 0,36 (TP 29, FN 2, FP 36, TN 20) |
| RSSI sym. − rzecz. (n = 15 łączy z RSSI) | średnio +13,8 dB, mediana +10,3 dB, RMSE 29,6 dB, r = 0,39 |
| PDR (bramki, n = 71 par) | przewidywane średnio 0,71, obserwowane 0,12 |

Według odległości:

| dystans | par | łącze rzeczywiste | model przewiduje łącze |
|---|---|---|---|
| 0,2–2 km | 13 | 9 | 13 |
| 2–5 km | 14 | 12 | 14 |
| 5–15 km | 18 | 4 | 18 |
| 15–60 km | 42 | 6 | 20 |

Przesunięcie progu o dodatkową stratę Δ (zbalansowana trafność):

| Δ [dB] | 0 | 10 | 20 | 25 | 30 | 35 | 45 | 55 |
|---|---|---|---|---|---|---|---|---|
| TPR | 0,94 | 0,87 | 0,84 | 0,71 | 0,65 | 0,61 | 0,52 | 0,26 |
| TNR | 0,36 | 0,50 | 0,59 | 0,70 | 0,80 | 0,82 | 0,84 | 0,96 |
| bal. acc. | 0,65 | 0,69 | 0,71 | 0,70 | 0,72 | 0,72 | 0,68 | 0,61 |

Dopasowanie metodą największej wiarygodności na liczbach pakietów zbiega do Δ ≈ 50–60 dB,
σ ≈ 16–20 dB (z członem awarii niezależnych od propagacji ε = 0,3: Δ = 48 dB, σ = 16 dB),
ale jest zdominowane przez kilka par z setkami pakietów i nie powinno być używane jako
pojedyncza poprawka.

**Wnioski**

- Model poprawnie porządkuje łącza (AUC 0,8), ale jest systematycznie optymistyczny:
  przewiduje łącza na 5–15 km, które w praktyce prawie nie występują.
- Rozsądna kalibracja to dodatkowa strata 20–30 dB. Prawdopodobne składniki: moc TX
  27 dBm w modelu wobec typowych 22 dBm (5 dB), anteny w budynkach i straty kabli,
  zabudowa i roślinność niewidoczne w SRTM.
- SNR w symulatorze (`RSSI − noise_floor`, szum −120 dBm) nie jest nasycany; rzeczywisty
  SNR LoRa nasyca się ok. +12 dB, więc porównanie SNR ma sens tylko dla słabych łączy.
- Profil terenu ma stałe 50 segmentów — dla tras 40 km to próbkowanie co 800 m.

## Wyniki: wpływ precyzji pozycji

Monte Carlo na 25 parach z klastrów: prawdziwa pozycja losowana w komórce siatki, węzeł
raportuje jej środek, porównanie strat ITM dla pozycji raportowanej i prawdziwej
(12 losowań na parę).

| bity | komórka | średni błąd pozycji | MAE strat | RMSE | 95. percentyl | pary w tej samej komórce |
|---|---|---|---|---|---|---|
| 11 | 23 km | 8,1 km | 16,5 dB | 21,9 dB | 45,6 dB | 90 / 300 |
| 13 | 5,8 km | 1,9 km | 10,4 dB | 14,9 dB | 33,0 dB | 48 / 300 |
| 15 | 1,5 km | 461 m | 5,4 dB | 8,1 dB | 19,4 dB | 5 / 300 |
| 17 | 365 m | 119 m | 3,4 dB | 5,3 dB | 11,2 dB | 3 / 300 |
| 19 | 91 m | 29 m | 1,2 dB | 2,2 dB | 4,1 dB | 0 |
| 21 | 23 m | 7 m | 0,5 dB | 1,1 dB | 2,5 dB | 0 |

Węzły z domyślną precyzją kanału (13 bitów) nie nadają się do walidacji modelu łączy;
od ok. 19 bitów błąd pozycji jest mniejszy od rozrzutu fadingu.

**Znaleziony błąd**: dwa węzły o identycznych współrzędnych (częste przy kwantyzacji)
dają dystans 0 i `ZeroDivisionError` w `itmlogic` (`hzns`) — precompute całego
scenariusza się nie powiedzie. `itm_precompute._pair_task` nie ma zabezpieczenia.

## Wyniki: routing (częściowe)

Rzeczywiste dane: dla par nadawca → bramka rozkład liczby skoków (`hop_start − hop_limit`)
i retransmitujący węzeł (`relay_node`) przy jednym skoku. Symulacja: klaster na
prawdziwym `meshtasticd` z rolami (`ROUTER_LATE` → `ROUTER`, `CLIENT_BASE` → `CLIENT`)
i `hop_limit` węzłów z sieci, 3 rundy broadcastów z każdego węzła i traceroute do
głównej bramki, ruch przez `TrafficGenerator` backendu.

Wykonano jeden przebieg — klaster k1 (Rzeszów), model bez kalibracji:

| metryka | wynik |
|---|---|
| zgodność „bezpośrednio / przez pośrednika” | 7 / 10 par |
| ta sama liczba skoków (moda) | 6 / 10; w pozostałych symulacja ma mniej skoków |
| zgodny węzeł retransmitujący z klastra | 2 / 2 (w sieci i w symulacji głównie `fa2d18f2`) |

**Ograniczenia — wyników routingu nie należy jeszcze traktować jako miarodajnych**:

- na eter trafiło 24 z 30 broadcastów, żaden traceroute nie został nadany, dostarczono
  14 z 48 pakietów mimo silnych łączy — to problem stanowiska testowego (wysyłanie przez
  `TCPInterface` blokuje się na kolejce, `close()` się zawiesza), nie modelu;
- w większości klastrów rzeczywiste retransmisje wykonują węzły spoza klastra (bez precyzyjnej
  pozycji), więc klaster precyzyjnych węzłów nie odtworzy rzeczywistego routingu;
- symulator nie ma roli `ROUTER_LATE`, a model nie uwzględnia kolizji.

Do zrobienia: naprawić wysyłanie w środowisku testowym, powtórzyć k0, k1, k2, k3, k6
w wariantach Δ = 0 i Δ = 25 dB.

## Reprodukcja

Skrypty analizy (pobranie API z nagłówkiem `User-Agent`, dekodowanie `PacketsRaw`,
ocena precyzji, dowody łączy, wybór klastrów, porównanie, kalibracja, Monte Carlo
precyzji, test routingu na `meshtasticd`) nie są częścią repozytorium. Dane lorastats
obejmują tylko ostatnie 24 h, więc powtórzenie da inną próbkę.

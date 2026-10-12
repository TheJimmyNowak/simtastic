# Kampania testowa v3 (12.10.2026)

Powtórzenie kampanii z [8.10](test_campaign.md) po przeglądzie metodyki. Dane te same
(zrzut lorastats.pl z 8.10.2026), ale zmienione zostały model symulatora, kalibracja
i statystyka. Wyniki v2 nie są z v3 bezpośrednio porównywalne — zmieniło się m.in. źródło
terenu (SRTM → NMT GUGiK) i analiza (błąd identyfikacji pakietów, pkt 1 poniżej).

Przebiegi: `data/campaign_v3/runs/` (332), raport: `data/campaign_v3/report.json`,
kalibracja: `data/campaign_v3/calibration.json`, T1: `data/campaign_v3/t1.json`.
14 równoległych backendów; opóźnienie obsługi zgłoszeń transmisji w backendzie mierzone
w każdym przebiegu (mediana 0,1 ms, p99 ≤ 9 ms), więc równoległość nie zaburza czasu.

## Błędy metodologiczne usunięte przed kampanią

1. **Identyfikacja pakietów po samym `packet_id` (analiza).** Kontenery `meshtasticd`
   losują te same identyfikatory pakietów (np. NodeInfo czterech węzłów miało jedno id).
   Firmware deduplikuje po parze (nadawca pierwotny, id), ale `routing_eval.py` łączył
   odbiory z sondami po samym id. W kampanii v2 dotyczyło to 8% sond (63% sond NodeInfo)
   we wszystkich 248 przebiegach routingu — odbiór cudzego pakietu był liczony jako odbiór
   sondy. Teraz klucz to (nadawca pierwotny, id) w medium, w śladzie (`link_attempt` ma tag
   `origin_node`) i w analizie.
2. **Emulacja CAD wyłączała tłumienie retransmisji.** Firmware w SimRadio zgłasza pakiet
   jako nadany, a gateway przesuwał go za zajęty kanał; prawdziwy węzeł trzymałby go
   w kolejce i anulował po usłyszeniu duplikatu. Odtworzenie przebiegów T7 v2: 26–38%
   retransmisji, które firmware by anulował. Medium anuluje teraz odroczoną retransmisję,
   gdy węzeł (rola inna niż ROUTER/ROUTER_LATE) odbierze duplikat przed jej startem
   (`packet_ota_cancel`).
3. **Ruch tła ustawiony przez symulator.** Provisioning ustawiał NodeInfo i pozycję co 90 s
   (firmware: 3 h i 15 min); w T7 v2 88% transmisji przy 80 węzłach stanowiły te beacony.
   Teraz zostają wartości domyślne, a przed ruchem jest rozgrzewka: gateway działa już podczas
   konfiguracji węzłów, każdy węzeł rozgłasza NodeInfo w losowej chwili okna
   `max(20 s, 2 s × N)` — sieć startuje w stanie zbliżonym do ustalonego (znane węzły,
   klucze, węzły pośredniczące).
4. **Fading 8 dB na pakiet.** Rozrzut RSSI kolejnych odbiorów bezpośrednich tego samego łącza
   w danych lorastats wynosi 0,9 dB (zbiorczo, 12 łączy ≥ 10 dB nad czułością, 330 stopni
   swobody; wszystkie łącza: 1,1 dB). Domyślny `fading_sigma_db` = 1 dB. Straty pakietów na
   silnych łączach w sieci (20–50% przy 15–20 dB zapasu) nie wynikają z fadingu, tylko
   z kanału (kolizje, półdupleks bramki) — w kalibracji opisuje je osobny człon ε.
5. **Kalibracja porównywała różne zdarzenia.** Etykieta „łącze” to ≥ 1 odbiór bezpośredni
   z n pakietów, a model dawał prawdopodobieństwo odbioru jednego pakietu; σ fadingu
   przyjęte, nie zmierzone; straty liczone bez budynków, a stosowane z budynkami (Rzeszów —
   podwójne liczenie). Teraz `calibrate.py`: model dwumianowy liczby odbiorów bezpośrednich
   z n pakietów, z cieniowaniem, zmierzonym fadingiem i ε, na stratach NMT + LoD1 we
   wszystkich parach; wybór modelu regułą ustaloną przed dopasowaniem (Brier w walidacji
   krzyżowej z pominięciem bramki).
6. **Testy zakładały niezależność par.** Wilcoxon na parach (v2: p = 1,7·10⁻¹⁰) mimo
   zależności par z jedną bramką. Teraz test permutacyjny znaku na poziomie bramek i klastrów
   (dokładny dla ≤ 16 grup) z korektą Holma dla całej rodziny porównań; Wilcoxon zostaje
   tylko do porównania.
7. **Ablacja kolizji mieszała dwa czynniki.** Bez kolizji pakiet docierał natychmiast,
   z kolizjami po czasie w eterze. Teraz zawsze po czasie w eterze — ablacja zmienia tylko
   straty.
8. Pozostałe: straty ITM uśredniane w obu kierunkach (niewzajemność itmlogic to artefakt
   numeryczny); chwila nadania = odebranie zgłoszenia z TCP, nie jego obsługa w kolejce
   biblioteki; wysokości anten węzłów lorastats względem NMT; usługa NMT potrafi pod
   obciążeniem zwrócić 0 m — takie punkty są odpytywane ponownie.

Nie usunięte (ograniczenia v3): klastry bez węzłów spoza klastra; zakłócenia parami, bez
sumowania; CAD wykrywa każdy sygnał ≥ czułość; czas w eterze z długości opakowania
`Compressed`; metryka opóźnienia dostarczenia w v3 jest niewiarygodna (znacznik próby
ustawiany przy nadaniu — poprawione po kampanii).

## Kalibracja (zbiór kalibracyjny: raporty sprzed 7.10 17:35)

132 pary nadawca → bramka (19 z łączem, 16 bramek), straty NMT + LoD1 (78 powiatów), σ_f = 1 dB.

| model | L [dB] | σ [dB] | inne | −log L | CV: Brier / skill / AUC |
|---|---|---|---|---|---|
| **pary** (wybrany) | 67,1 | 43,8 | ε = 0,11 | 130,1 | **0,113** / +0,08 / 0,76 |
| efekt instalacji bramki | 45,3 | σ_w 30,0, σ_p 30,0 | ε = 0,11 | 129,4 | 0,125 / −0,01 / 0,69 |
| mieszanina (część par zablokowana) | 9,9 | 18,0 | π = 0,74, ε = 0,11 | 127,6 | 0,117 / +0,05 / 0,71 |

Bootstrap po bramkach (90%, modelu par): L 42–101 dB, σ 29–61 dB.

**Wniosek:** kalibracja na danych lorastats jest słabo wyznaczalna. Trzy modele o bardzo
różnych parametrach przewidują łącza prawie tak samo i niewiele lepiej niż stała. Dane są
bimodalne: łącza z RSSI pasują do modelu w granicach ok. 20 dB, a wiele par z zapasem
40–60 dB nie ma żadnego odbioru bezpośredniego — skupione przy kilku bramkach (antena
w budynku, instalacja). Wybrany model par ma wysokie L i σ, więc w symulacji łącza są
„prawie losowe z trendem odległości”. Mieszanina jest fizycznie bardziej wiarygodna
(obciążenie RSSI −9 dB zamiast −66 dB, tabela T1) i jest w T2 wariantem wrażliwości.

## T1: model łączy na zbiorze testowym

156 par (18 z łączem, 16 bramek), prawdopodobieństwo ≥ 1 odbioru z n pakietów.

| metryka | fizyczny | skalibrowany (pary) | mieszanina |
|---|---|---|---|
| AUC [95% CI, bootstrap po bramkach] | 0,82 [0,63–0,92] | ten sam ranking | |
| AUC samego dystansu | 0,69 [0,48–0,87] | | |
| Brier [CI] | 0,43 [0,27–0,56] | **0,084** [0,052–0,119] | 0,090 |
| Brier skill | −3,17 | **+0,18** | +0,12 |
| ECE | 0,43 | 0,02 | 0,02 |
| średnie p (łącza: 0,12) | 0,52 | 0,09 | 0,10 |
| obciążenie RSSI łączy istniejących (n = 16) | +0,9 dB | −66,2 dB | −9,0 dB |

v2 (SRTM, stara kalibracja): AUC 0,79, Brier skalibrowany 0,17, skill −0,68. Model
skalibrowany po raz pierwszy jest lepszy od stałej równej częstości łączy, ale ranking
(AUC) to jedyna jego właściwość niezależna od kalibracji; teren i budynki dają +0,13 AUC
ponad sam dystans.

## T2: routing (87 par w 6 klastrach, 10 seedów; mieszanina 5 seedów)

| metryka | fizyczny | skalibrowany | mieszanina |
|---|---|---|---|
| zgodność trybu bezpośrednio / przez pośrednika [CI po klastrach] | 0,42 [0,32–0,64] | 0,62 [0,50–0,79] | 0,47 [0,26–0,58] |
| kappa Cohena | 0,07 | 0,13 | 0,06 |
| Brier P_sim(bezpośrednio) | 0,54 | 0,25 | 0,47 |
| różnica skoków sym. − sieć [CI] | −1,61 [−2,18 – −0,72] | −1,43 [−2,09 – −0,47] | −1,39 [−2,23 – −0,38] |
| zgodna liczba skoków / Wasserstein | 19% / 1,85 | 29% / 1,75 | 18% / 1,81 |
| zgodny węzeł pośredniczący | 5 / 13 | 4 / 22 | 5 / 13 |
| pary osiągnięte w przebiegu (sieć: 100%) | 0,71 | 0,28 ± 0,14 | 0,18 ± 0,07 |
| PDR sond do bramki | 0,67 | 0,22 | 0,14 |
| straty prób: słaby / kolizja / półdupleks | 17% / 7% / 2% | 30% / 3% / 0% | 20% / 2% / 1% |

Porównania (|błąd| na parach osiągniętych w obu wariantach; p permutacyjne po bramkach /
po klastrach; Holm dla 8 porównań):

| porównanie | n par (bramek) | błąd trybu A / B | p bramki | p klastry | p Holm | p Wilcoxon par |
|---|---|---|---|---|---|---|
| fizyczny vs skalibrowany | 59 (11) | 0,59 / 0,42 | 0,14 | 0,31 | 1,0 | 0,02 |
| skalibrowany vs mieszanina | 51 (11) | 0,43 / 0,52 | 0,13 | 0,19 | 1,0 | 0,26 |
| liczba skoków: wszystkie pary porównań | | różnice ≤ 0,11 skoku | ≥ 0,12 | | 1,0 | |

**Wnioski T2**
- Kalibracja poprawia zgodność trybu przekazania (0,42 → 0,62), ale przy jednostce
  losowania „bramka” różnica nie jest istotna (p = 0,14; Holm 1,0). Istotność z v2
  (p = 1,7·10⁻¹⁰) była artefaktem testu zakładającego niezależność par; ten sam test na
  danych v3 daje p = 0,02.
- We wszystkich wariantach trasy są krótsze o 1,4–1,6 skoku — błąd systematyczny niezależny
  od kalibracji (brak węzłów spoza klastra, przez które w sieci idą trasy).
- Skalibrowany model łączy rozspójnia klastry: w pojedynczym przebiegu osiąga 28% par, a sieć
  dostarcza do wszystkich. Wyższa zgodność trybu wynika częściowo z tego, że osiągane są głównie
  pary bliskie.
- Klastry (skalibrowany, zgodność / różnica skoków): Rzeszów 0,89 / +0,33, Lublin 0,85 / −2,1,
  Zachód 0,75 / −0,9, Piotrków 0,50 / −0,3, Warszawa 0,48 / −2,3, Śląsk 0,33 / −2,5.

## T3: ablacje (5 seedów, wariant skalibrowany)

| porównanie | błąd trybu A / B | p bramki (Holm) | błąd skoków A / B | p bramki (Holm) |
|---|---|---|---|---|
| kolizje wł. / wył. | 0,38 / 0,34 | 0,62 (1,0) | 1,49 / 1,38 | 0,12 (1,0) |
| budynki wł. / wył. | 0,35 / 0,31 | 0,28 (1,0) | 1,64 / 1,61 | 0,77 (1,0) |

Ani kolizje, ani budynki nie zmieniają istotnie zgodności routingu z siecią. Wynik v2
„budynki poprawiają zgodność w Rzeszowie, p = 0,039” nie przetrwał: dotyczył 9 par, nie
był korygowany na wielokrotne porównania, a kalibracja v2 liczyła budynki dwukrotnie.
Bez budynków (przy tej samej stracie otoczenia) więcej par jest osiągalnych (0,60 wobec 0,28).

## T4: powtarzalność (seed 1 × 3 dla obu wariantów, 6 klastrów)

Ten sam tryb przekazania w 94% par, ta sama moda liczby skoków w 93%, SD PDR między
powtórzeniami 0,01 (v2: 83% / 67% / 0,023). Niedeterminizm firmware ma teraz mały udział
w rozrzucie — w v2 zawyżały go fading 8 dB (losowy SNR → losowy wybór przekaźnika) i burza
beaconów.

## T5: medium (10 i 20 węzłów w zasięgu wzajemnym, hop_limit 0, fading 0)

| węzły | G | PDR sond (kolizje) | teoria CSMA | ALOHA e^(−2G) |
|---|---|---|---|---|
| 10 | 0,05 | 0,98 | 0,99 | 0,90 |
| 10 | 0,13 | 0,97 | 0,98 | 0,76 |
| 10 | 0,42 | 0,89 | 0,93 | 0,44 |
| 20 | 0,07 | 1,00 | 0,99 | 0,86 |
| 20 | 0,16 | 0,98 | 0,98 | 0,73 |
| 20 | 0,45 | 0,84 | 0,92 | 0,41 |

(średnie dwóch seedów). Bez kolizji PDR = 1,000 we wszystkich 20 przebiegach. Wyniki jak
w v2: zgodność z CSMA przy małym obciążeniu, ok. 0,05 poniżej przy G ≈ 0,45, dużo lepiej
niż ALOHA. To weryfikacja implementacji (teoria CSMA zakłada to samo okno podatności co
model), nie walidacja względem rzeczywistego radia.

## T6: czułość routingu na stratę otoczenia (σ = 43,8 dB, 3 seedy; 67 dB — 10 seedów)

| strata otoczenia [dB] | 57 | 62 | 67 (kalibracja) | 77 |
|---|---|---|---|---|
| zgodność trybu | 0,77 ± 0,04 | 0,74 ± 0,09 | 0,64 ± 0,07 | 0,57 ± 0,12 |
| różnica skoków | −0,45 ± 0,68 | −0,70 ± 0,84 | −0,89 ± 0,62 | −0,98 ± 1,08 |
| pary osiągnięte w przebiegu | 0,35 | 0,29 | 0,28 | 0,14 |
| PDR sond | 0,29 | 0,24 | 0,23 | 0,11 |

Przy dużym σ mniejsza strata otoczenia lepiej odwzorowuje routing — przeciwnie niż w v2
(gdzie σ = 20 dB). Rozrzut między seedami (SD różnicy skoków 0,6–1,1) jest tego samego rzędu
co różnice między poziomami, więc kierunek jest wskazówką, nie wynikiem.

## T7: skalowalność (losowo na 10 × 10 km wokół Rzeszowa, model skalibrowany, 30 wiadomości)

| węzły | 10 | 25 | 50 | 80 |
|---|---|---|---|---|
| precompute / start (konfiguracja + rozgrzewka) [s] | 24 / 67 | 38 / 131 | 90 / 266 | 94 / 370 |
| średni stopień węzła (łącza nad czułością) | 1,0 | 3,3 | 6,3 | 9,4 |
| zasięg rozgłoszenia: średnio / 10. percentyl | 0,24 / 0,00 | 0,81 / 0,66 | 0,89 / 0,83 | 0,90 / 0,89 |
| retransmisje na wiadomość (v2) | 2,0 (4,5) | 10,7 (9,9) | 18,9 (18,7) | 25,9 (25,9) |
| retransmisje anulowane przez medium na wiadomość | 0 | 0,5 | 2,1 | 3,1 |
| kolizje wśród prób odbioru wiadomości (v2: wszystkich pakietów) | 0,8% (6%) | 3,9% (24%) | 10,6% (38%) | 18,3% (41%) |
| udział wiadomości w transmisjach | 52% | 39% | 20% | 11% |
| opóźnienie obsługi zgłoszeń p99 [ms] | 4,7 | 8,7 | 0,3 | 0,6 |

Kolizje wiadomości są 3–6 razy rzadsze niż w v2, gdzie liczono je łącznie z burzą beaconów
co 90 s (88% transmisji przy 80 węzłach). Liczba retransmisji na wiadomość prawie się nie
zmieniła — anulowanie odroczonych retransmisji (do 3 na wiadomość) równoważy mniejszą liczbę
kolizji, po których węzły retransmitowały ponownie. Pozostałe transmisje to głównie rozgrzewka
(NodeInfo 80 węzłów z retransmisjami). Przy 10 węzłach na 100 km² sieć jest rozspójniona
(średnio 1 sąsiad). Start rośnie liniowo (ok. 4,6 s na węzeł, w tym okno rozgrzewki 2 s na węzeł).
Pierwsza próba przebiegu 80 węzłów nie powiodła się przy eksporcie z InfluxDB (limit
10 s zapytania; podniesiony do 300 s) i została powtórzona w całości.

## Podsumowanie

- Usunięto osiem błędów metodologicznych; dwa z nich (identyfikacja pakietów, test
  zakładający niezależność par) zmieniały wnioski v2.
- Model łączy porządkuje łącza (AUC 0,82, +0,13 ponad sam dystans), a skalibrowane
  prawdopodobieństwa są po raz pierwszy lepsze od stałej (Brier skill +0,18). Parametry
  kalibracji są jednak słabo wyznaczalne, a modele o skrajnie różnych parametrach przewidują
  łącza podobnie.
- Routing: kalibracja podnosi zgodność trybu przekazania z 0,42 do 0,62, ale różnica nie jest
  istotna statystycznie przy poprawnej jednostce losowania. Trasy są systematycznie krótsze
  o ok. 1,5 skoku w każdym wariancie, a model skalibrowany rozspójnia klastry.
- Kolizje i budynki nie zmieniają istotnie zgodności routingu; medium zachowuje się zgodnie
  z teorią CSMA; powtarzalność przebiegów jest wysoka (94% / 93%).
- Do pracy: twierdzenie, że model skalibrowany odwzorowuje tryb przekazania w trzech
  czwartych par i że jest istotnie lepszy od fizycznego (p < 10⁻⁹), należy zastąpić wynikami
  v3; przewaga budynków w Rzeszowie nie jest potwierdzona.

## Reprodukcja

```bash
export PYTHONPATH=backend:scripts/validation BUILDINGS_INDEX_MAX_MB=4000
D=data/lorastats/2026-10-08
python scripts/validation/lorastats_dataset.py $D --until 2026-10-07T17:35     # zbiór kalibracyjny
python scripts/validation/lorastats_pairs.py $D/dataset_2026-10-07T1735.json --meta $D/dataset.json
python scripts/validation/lorastats_pairs.py $D/dataset_do2026-10-07T1735.json --meta $D/dataset.json
python scripts/validation/link_eval.py $D/clusters_do2026-10-07T1735.json   # straty NMT + LoD1
python scripts/validation/link_eval.py $D/clusters_2026-10-07T1735.json
python scripts/validation/calibrate.py $D/links_do2026-10-07T1735.json -o data/campaign_v3/calibration.json
python scripts/validation/link_eval.py $D/clusters_2026-10-07T1735.json --reuse \
    --calibration data/campaign_v3/calibration.json                         # T1
for k in routing medium sweep scale; do
  python scripts/validation/make_plan.py $k $D/clusters_2026-10-07T1735.json \
      --calibration data/campaign_v3/calibration.json -o data/campaign_v3/plan_$k.json
done
CAMPAIGN_DIR=data/campaign_v3 scripts/validation/backends.sh $(seq 8201 8214)
python scripts/validation/campaign.py data/campaign_v3/plan_all.json --out data/campaign_v3 \
    --backends $(seq -s, 8201 8214)
python scripts/validation/routing_eval.py data/campaign_v3 $D/clusters_2026-10-07T1735.json
```

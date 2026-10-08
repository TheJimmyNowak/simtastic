# Kampania testowa 8.10.2026

Ponowny test symulatora po dodaniu modelu kolizji i budynków. Główna różnica w stosunku do
[walidacji z 7.10](validation_lorastats.md): **nowy, niezależny w czasie zbiór danych** i brak
jakiegokolwiek dopasowania parametrów — kalibracja z 7.10 (strata otoczenia 27 dB, cieniowanie
łącza σ = 20 dB) jest sprawdzana na danych, których nie widziała.

## Dane

Zrzut API lorastats.pl (ostatnie 24 h) pobrany 8.10.2026 o 08:10, wszystkie regiony
(nowy region ShortFast). Kalibracja z 7.10 trafiła do repozytorium o 17:33, więc była
dopasowana do danych sprzed tej godziny. **Zbiór testowy** to raporty od 7.10 17:35 do
8.10 08:11 (678 tys. raportów odbioru, 357 bramek). Pozycje, hash kanału i `hop_start`
węzłów pochodzą z pełnych 24 h (to cechy węzła, a nie wynik łącza).

Kryteria jak w walidacji z 7.10: pozycja z `precision_bits = 32` we wszystkich raportach,
węzeł stacjonarny (< 30 m), w Polsce, bez „okrągłych” współrzędnych; bramki będące
obserwatorami radiowymi; pary 200 m – 60 km na tym samym presecie. Łącze istnieje, gdy
bramka odebrała pakiet bezpośrednio (`hop_start = hop_limit`, `relay_node` równy nadawcy
albo 0 w starszym firmware) albo para występuje w `NEIGHBORINFO_APP`; brak łącza, gdy bramka
zaraportowała ≥ 5 pakietów nadawcy wyłącznie po retransmisji. Pakiet z `hop_start = hop_limit`,
ale `relay_node` innym niż nadawca, to retransmisja przez router „zero-hop” (ulubiony węzeł,
firmware 2.6+), a nie odbiór bezpośredni.

| | zbiór testowy | pełne 24 h |
|---|---|---|
| węzły z pełną precyzją | 187 | 187 |
| bramki-obserwatorzy wśród nich | 17 | 17 |
| pary nadawca → bramka z dowodem | 156 (18 z łączem) | 171 (21 z łączem) |
| pary NeighborInfo (tylko łącze) | 58 | 67 |

Odsetek par z łączem (12%) jest dużo niższy niż w próbie z 7.10 (36%): tym razem wśród bramek
z dokładną pozycją przeważają duże bramki miejskie, które słyszą większość nadawców tylko po
retransmisji.

## Proponowane metryki jakości symulatora

Symulator jest stochastyczny (fading, cieniowanie, czas działania firmware), a dane
z sieci są obserwacjami binarnymi i rozkładami, więc metryki są probabilistyczne,
a niepewność liczona jest bootstrapem po grupach (bramka, klaster) — pary z tą samą
bramką nie są niezależne.

**Warstwa łącza** (para nadawca → bramka, klasyfikacja „jest łącze / nie ma”):

| metryka | co mierzy | wartość idealna |
|---|---|---|
| AUC (Mann–Whitney) marginesu `RSSI_med − czułość` | czy model porządkuje łącza od najlepszych do najgorszych | 1 (0,5 = losowo) |
| Brier score `mean((p − y)²)` | błąd przewidywanego prawdopodobieństwa łącza | 0 |
| Brier skill score względem częstości bazowej | czy model jest lepszy od stałej równej odsetkowi łączy | > 0 |
| log-loss | jak Brier, ale mocno karze pewne pomyłki | 0 |
| ECE (expected calibration error) | średnia różnica przewidywanego i obserwowanego odsetka łączy w przedziałach p | 0 |
| prosta kalibracji `logit P(y) = a + b·logit(p)` | `a < 0` — model za optymistyczny, `b < 1` — za pewny siebie | a = 0, b = 1 |
| TPR, TNR, zbalansowana trafność, MCC | jakość decyzji przy progu `p = 0,5` | 1 |
| bias, RMSE, granice zgodności Blanda–Altmana, Spearman RSSI/SNR | błąd poziomu sygnału na istniejących łączach | 0 / wąskie / 1 |

**Routing** (para nadawca → bramka, pierwszy odbiór każdego pakietu na bramce):

| metryka | co mierzy |
|---|---|
| odsetek osiągniętych par | czy symulacja w ogóle dostarcza pakiety tam, gdzie sieć dostarcza |
| zgodność trybu „bezpośrednio / przez pośrednika” + kappa Cohena | zgodność skorygowana o przypadek (przy 12% łączy bezpośrednich sama trafność jest myląca) |
| Brier P_sim(bezpośrednio) | prawdopodobieństwo z wielu przebiegów zamiast jednej decyzji |
| bias i MAE liczby skoków (moda), odsetek zgodnych | systematyczne skracanie / wydłużanie tras |
| odległość Wassersteina rozkładów skoków | zgodność całych rozkładów, nie tylko mody |
| zgodność węzła pośredniczącego | czy firmware w symulacji wybiera te same węzły co w sieci |
| test Wilcoxona par (wariant A vs B) | czy różnica między wariantami modelu jest istotna na tych samych parach |

**Stabilność i medium**: rozrzut metryk między seedami (σ), zgodność przebiegów
z identycznym seedem (niedeterminizm firmware), PDR w funkcji obciążenia na tle
ALOHA `e^{−2G}`, udział strat kolizyjnych i półdupleksu.

## Plan

| test | zakres | przebiegi |
|---|---|---|
| T0 regresja | `pytest` + `scripts/e2e_line_test.py` (A–B–C na `meshtasticd`) | – |
| T1 łącza | wszystkie pary z dowodem, model fizyczny i skalibrowany, bez dopasowania | precompute |
| T2 routing | 6 klastrów × {fizyczny, skalibrowany} × 10 seedów, kolizje włączone | 120 |
| T3 ablacje | kolizje wyłączone (6 klastrów × 5 seedów); budynki wyłączone (Rzeszów, 2 warianty × 10 seedów) | 50 |
| T4 powtarzalność | seed 1 powtórzony 3× dla obu wariantów i wszystkich klastrów | 24 |
| T5 medium | 10 i 20 węzłów w zasięgu wzajemnym, `hop_limit = 0`, 0,25–4 pakietów/s, kolizje wł./wył., 2 seedy | 40 |
| T6 czułość | strata otoczenia 10 / 20 / 35 dB (σ = 20 dB), 6 klastrów × 3 seedy | 54 |
| T7 skalowalność | 10 / 25 / 50 / 80 węzłów losowo na 10 × 10 km (Rzeszów), 30 wiadomości zalewanych | 4 |

Łącznie 292 przebiegi końcowe (po poprawkach modelu kolizji) plus 114 przebiegów sprzed
poprawek zachowanych do porównania. Przebiegi idą przez API aplikacji (utworzenie
scenariusza, precompute, start, stop) na 6–12 równoległych instancjach backendu (CPU w fazie
ruchu 85–90% bezczynne, więc równoległość nie zaburza czasu); ślad z InfluxDB zapisywany
jest dla każdego przebiegu. Seed = seed cieniowania łącza i fadingu. Ruch: 3 rundy
broadcastów z każdego węzła co 5 s, potem traceroute z każdego węzła do głównej bramki
klastra; do tego własne pakiety firmware (NodeInfo, pozycja). Dla każdej pary nadawca →
bramka liczy się pierwszy skuteczny odbiór każdego pakietu nadawcy na bramce.

## T0: regresja

63/63 testów jednostkowych (po poprawkach z kampanii 66/66), test e2e linii A–B–C: OK (wiadomość A dociera do C wyłącznie
przez retransmisję B, konfiguracja przetrwała restart).

## T1: łącza na zbiorze testowym

156 par z bramkami (18 z łączem). Model nie był dopasowywany do tych danych.

| metryka | fizyczny | skalibrowany (7.10) |
|---|---|---|
| AUC [95% CI, bootstrap po bramkach] | 0,79 [0,62–0,89] | 0,79 (ten sam ranking) |
| Brier [95% CI] | 0,51 [0,35–0,65] | 0,17 [0,12–0,24] |
| Brier skill (vs stała 0,12) | −3,97 | −0,68 |
| log-loss | 3,38 | 0,50 |
| ECE | 0,57 | 0,22 |
| prosta kalibracji a / b | — (rozbieżna) | −1,77 / 0,66 |
| średnie p vs odsetek łączy | 0,68 vs 0,12 | 0,33 vs 0,12 |
| TPR / TNR | 1,00 / 0,36 | 0,61 / 0,72 |
| zbalansowana trafność / MCC | 0,68 / 0,25 | 0,67 / 0,23 |
| RSSI łączy istniejących (n = 16): bias / RMSE | +7,9 / 15,8 dB | −19,1 / 23,5 dB |
| SNR łączy słabych (< 5 dB, n = 12): bias / RMSE / Spearman | | −0,8 / 6,4 dB / 0,83 |
| TPR na 58 parach NeighborInfo | 1,00 | 0,64 |

Według odległości (zbiór testowy):

| dystans | par | łącze w sieci | model fizyczny przewiduje | skalibrowany przewiduje | średnie p (skalibr.) |
|---|---|---|---|---|---|
| 0,2–2 km | 11 | 3 | 11 | 11 | 0,83 |
| 2–5 km | 12 | 5 | 12 | 11 | 0,67 |
| 5–15 km | 42 | 3 | 41 | 20 | 0,49 |
| 15–60 km | 91 | 7 | 42 | 7 | 0,16 |

Na pełnych 24 h wyniki są praktycznie te same (AUC 0,76 [0,60–0,88], Brier 0,18 skalibrowany).

**Wnioski T1**

- Zdolność porządkowania łączy się przenosi: AUC 0,79 na nowych danych wobec 0,80 na
  danych kalibracyjnych.
- Kalibracja poprawia prawdopodobieństwa trzykrotnie (Brier 0,51 → 0,17), ale model nadal
  przewiduje za dużo łączy (średnie p 0,33 przy 12% rzeczywistych; wyraz wolny −1,8)
  i jest zbyt pewny siebie (nachylenie 0,66). Nie jest lepszy od stałej równej częstości
  łączy (Brier skill < 0) — ta stała jest jednak znana dopiero po fakcie i zależy od próby
  (36% 7.10, 12% 8.10).
- Kalibrowana strata otoczenia zależy od próby: przy innym składzie bramek optimum
  byłoby wyższe. To argument za modelem straty per węzeł (instalacja), a nie globalnym.
- SNR słabych łączy jest odwzorowany dobrze (bias −0,8 dB, Spearman 0,83); RSSI łączy
  istniejących w wariancie skalibrowanym jest zaniżone o 19 dB (efekt selekcji opisany 7.10
  przewiduje ok. −15 dB).

## T2: routing na `meshtasticd` (kolizje włączone, po poprawkach)

87 par nadawca → bramka w 6 klastrach (Śląsk ShortFast, Warszawa, Zachód ShortSlow,
Rzeszów, Lublin, okolice Piotrkowa Tryb. ShortFast), 10 seedów na wariant. Metryki liczone dla par z rozkładu
zebranego ze wszystkich seedów; CI 95% bootstrapem po klastrach, „między seedami” = średnia
± SD metryki liczonej osobno dla każdego seeda.

| metryka | fizyczny | skalibrowany |
|---|---|---|
| pary osiągnięte (w którymkolwiek przebiegu / średnio w przebiegu) | 87 / 0,96 | 87 / 0,93 |
| pary z trybem „bezpośrednio”: symulacja / sieć | 58 / 7 | 29 / 7 |
| zgodność trybu bezpośrednio / przez pośrednika [CI] | 0,41 [0,20–0,69] | **0,75 [0,58–0,96]** |
| kappa Cohena | 0,08 | 0,30 |
| Brier P_sim(bezpośrednio) [CI] | 0,42 [0,23–0,56] | **0,17 [0,08–0,25]** |
| różnica skoków sym. − sieć (moda) [CI] | −1,93 [−2,56 – −1,07] | −1,53 [−2,11 – −0,77] |
| MAE skoków / zgodna liczba skoków | 1,93 / 20% | 1,62 / 24% |
| odległość Wassersteina rozkładów skoków | 2,07 | 1,67 |
| zgodny węzeł pośredniczący z klastra | 6 / 24 | 6 / 25 |
| PDR sond do bramki | 0,83 | 0,73 |
| straty prób odbioru: słaby sygnał / kolizja / półdupleks | 24% / 11% / 3% | 31% / 7% / 2% |
| między seedami: zgodność trybu, różnica skoków | 0,37 ± 0,02; −1,90 ± 0,08 | 0,66 ± 0,07; −1,42 ± 0,12 |

Test Wilcoxona na tych samych 87 parach (|błąd| trybu i liczby skoków): skalibrowany lepszy
od fizycznego, p = 1,7·10⁻¹⁰ (tryb) i p = 6,8·10⁻⁵ (skoki).

Klastry (wariant skalibrowany): zgodność trybu / różnica skoków — Śląsk 1,00 / −1,8,
Warszawa 0,50 / −2,4, Zachód 0,89 / −1,1, **Rzeszów 1,00 / 0,0 (78% par z tą samą liczbą
skoków)**, Lublin 0,92 / −2,2, Piotrków 0,50 / −0,7. Rzeszów — jedyny klaster
z danymi budynków i gęstą siecią w klastrze — jest odwzorowany prawie idealnie; najgorzej
wypadają Warszawa i Lublin, gdzie trasy w sieci prowadzą przez wiele węzłów spoza klastra.

![różnica skoków](../latex/rysunki/t2_skoki.pdf)

## T3: ablacje

| porównanie (Wilcoxon, |błąd| trybu) | n par | błąd A | błąd B | p |
|---|---|---|---|---|
| skalibrowany: kolizje wł. (A) vs wył. (B) | 87 | 0,32 | 0,34 | 0,17 |
| Rzeszów skalibrowany: budynki wł. (A) vs wył. (B) | 9 | 0,23 | 0,37 | **0,039** |
| Rzeszów fizyczny: budynki wł. (A) vs wył. (B) | 9 | 0,46 | 0,58 | 0,31 |

Kolizje nie zmieniają istotnie zgodności routingu z siecią (przed poprawką CAD różnica była
istotna, p = 0,0007 — był to efekt zawyżonych kolizji). Budynki poprawiają zgodność trybu
w Rzeszowie przy kalibracji (p = 0,039 na 9 parach, 10 seedów); różnice liczby skoków nie
są istotne (p = 0,25).

## T4: powtarzalność

Identyczna konfiguracja i seed (cieniowanie i fading), 3 przebiegi — różnią się tylko
czasem działania firmware (losowe opóźnienia, kolejność wątków). Średnio 83% par ma ten
sam tryb i 67% tę samą modę liczby skoków; SD PDR między powtórzeniami 0,023 wobec 0,072
między seedami. Niedeterminizm firmware odpowiada więc za mniej więcej jedną trzecią
rozrzutu wyników — pojedynczy przebieg nie wystarcza, wnioski trzeba uśredniać.

## T5: medium — kolizje względem teorii

Syntetyczna sieć: węzły na okręgu 300 m, każdy słyszy każdego, `hop_limit = 0`, fading 0;
sondy TEXT w chwilach z procesu Poissona. Średnie z 2 seedów.

| węzły | λ sond [1/s] | G | PDR sond | teoria CSMA e^(−2λτ) | ALOHA e^(−2G) | PDR pakietów firmware |
|---|---|---|---|---|---|---|
| 10 | 0,25 | 0,06 | 0,98 | 0,99 | 0,89 | 0,61 |
| 10 | 1 | 0,13 | 0,97 | 0,98 | 0,76 | 0,87 |
| 10 | 4 | 0,41 | 0,82 | 0,93 | 0,44 | 0,77 |
| 20 | 0,25 | 0,09 | 0,98 | 0,99 | 0,84 | 0,60 |
| 20 | 1 | 0,17 | 0,95 | 0,97 | 0,71 | 0,73 |
| 20 | 4 | 0,42 | 0,82 | 0,93 | 0,43 | 0,79 |

Bez kolizji PDR = 1,000 we wszystkich 20 przebiegach (punkt odniesienia: medium nie gubi
pakietów samo z siebie). Z kolizjami PDR sond przy małym obciążeniu zgadza się z teorią
CSMA z oknem podatności równym slotowi CAD, a przy G ≈ 0,4 jest o ok. 0,1 niższe: po
zwolnieniu kanału kilka odroczonych węzłów losuje sloty jednocześnie (prosty wzór tego
nie obejmuje). We wszystkich punktach wynik jest dużo lepszy niż ALOHA — CAD działa.
Pakiety firmware mają niski PDR przy małym obciążeniu, bo wszystkie węzły nadają NodeInfo
i pozycję synchronicznie (wspólny start kontenerów) — to artefakt scenariusza.

![T5](../latex/rysunki/t5_medium.pdf)

## T6: czułość routingu na stratę otoczenia

σ cieniowania 20 dB, strata otoczenia 10 / 20 / 27 (kalibracja) / 35 dB; średnia ± SD seedów.

| strata otoczenia | 10 dB | 20 dB | 27 dB | 35 dB |
|---|---|---|---|---|
| zgodność trybu | 0,52 ± 0,04 | 0,62 ± 0,02 | 0,66 ± 0,07 | 0,75 ± 0,10 |
| różnica skoków | −1,67 ± 0,09 | −1,42 ± 0,05 | −1,42 ± 0,12 | −1,13 ± 0,15 |
| pary osiągnięte w przebiegu | 0,99 | 0,95 | 0,93 | 0,86 |
| PDR sond | 0,86 | 0,77 | 0,73 | 0,58 |

Większa strata (mniej łączy) monotonicznie poprawia zgodność trybu i zmniejsza skracanie
tras, ale kosztem osiągalności: przy 35 dB 14% par w przebiegu nie jest osiąganych,
a w sieci wszystkie są. Nawet przy 35 dB trasy są średnio o 1,1 skoku krótsze — reszta
różnicy to nie nadmiar łączy, tylko brak w symulacji węzłów spoza klastra, przez które
w sieci idą trasy (tłumaczy to też, czemu Rzeszów, z gęstą siecią wewnątrz klastra, nie
ma tej różnicy). Na 27 dB są wyniki z 10 seedów, na pozostałych z 3.

![T6](../latex/rysunki/t6_strata_otoczenia.pdf)

## T7: skalowalność aplikacji

| węzły | precompute | start (kontenery + konfiguracja) | wiadomości nadane | zasięg rozgłoszenia: śr. / 10. percentyl | retransmisje na wiadomość | kolizje |
|---|---|---|---|---|---|---|
| 10 | 4 s | 36 s | 30 / 30 | 0,65 / 0,00 | 4,5 | 6% |
| 25 | 10 s | 82 s | 30 / 30 | 0,92 / 0,91 | 9,9 | 24% |
| 50 | 22 s | 162 s | 30 / 30 | 0,92 / 0,87 | 18,7 | 38% |
| 80 | 91 s | 219 s | 30 / 30 | 0,92 / 0,83 | 25,9 | 41% |

Start rośnie liniowo (ok. 2,7 s na węzeł), precompute szybciej (liczba par ~N²). Przy
zalewaniu liczba retransmisji rośnie z N, a odsetek kolizji z 6% do 41% (burza
rozgłoszeniowa); średni zasięg trzyma się dzięki nadmiarowości, ale najgorzej obsłużone
wiadomości docierają do coraz mniejszej części sieci. Przy 10 węzłach na 100 km² sieć jest
rozspójniona (średnio 2 sąsiadów).

## Podsumowanie

- **Aplikacja**: żaden z 406 przebiegów przez API nie zakończył się błędem aplikacji, do 80 węzłów i 12 równoległych
  instancji. Kampania znalazła dwa błędy w emulacji CAD (naprawione, testy regresyjne)
  i brakujący element modelu (okno podatności CAD).
- **Łącza** (niezależny zbiór): ranking łączy się przenosi (AUC 0,79), kalibracja z 7.10
  poprawia prawdopodobieństwa trzykrotnie, ale model wciąż przewiduje za dużo łączy —
  globalna strata otoczenia zależy od próby.
- **Routing**: model skalibrowany odwzorowuje tryb przekazania w 75% par (kappa 0,30) wobec
  41% dla modelu fizycznego (p < 10⁻⁹). Systematyczny błąd to trasy krótsze o ok.
  1,5 skoku. Część to nadmiar łączy (różnica maleje z −1,9 dla modelu fizycznego do −1,1
  przy stracie 35 dB), reszta to brak w symulacji węzłów spoza klastra. Tam, gdzie klaster obejmuje lokalną sieć i są dane budynków
  (Rzeszów), routing jest odwzorowany niemal idealnie.
- **Kolizje**: po poprawkach zgodne z teorią CSMA przy małym obciążeniu, dużo lepsze niż
  ALOHA; na zgodność routingu z siecią wpływ nieistotny.
- **Powtarzalność**: niedeterminizm firmware to ok. 1/3 rozrzutu — potrzebne są serie
  przebiegów, nie pojedyncze uruchomienia.

**Możliwe dalsze kroki**: strata instalacji per węzeł zamiast globalnej straty otoczenia;
dołączanie do klastra węzłów pośredniczących spoza niego (z przybliżoną pozycją);
rozłożenie startu węzłów w czasie, żeby uniknąć synchronicznych beaconów firmware.

## Reprodukcja

```bash
# dane (24 h z lorastats.pl) -> zbiór, pary i klastry
PYTHONPATH=backend .venv/bin/python scripts/validation/lorastats_dataset.py data/lorastats/2026-10-08 --since 2026-10-07T17:35
PYTHONPATH=backend .venv/bin/python scripts/validation/lorastats_dataset.py data/lorastats/2026-10-08
PYTHONPATH=backend .venv/bin/python scripts/validation/lorastats_pairs.py \
    data/lorastats/2026-10-08/dataset_2026-10-07T1735.json --meta data/lorastats/2026-10-08/dataset.json
# T1
PYTHONPATH=backend:scripts/validation .venv/bin/python scripts/validation/link_eval.py \
    data/lorastats/2026-10-08/clusters_2026-10-07T1735.json
# plan i kampania (backendy na portach 8101…)
PYTHONPATH=backend .venv/bin/python scripts/validation/make_plan.py routing <clusters.json> -o data/campaign/plan_routing.json
scripts/validation/backends.sh 8101 8102 8103 8104 8105 8106
PYTHONPATH=backend .venv/bin/python scripts/validation/campaign.py data/campaign/plan_routing.json \
    --out data/campaign --backends 8101,8102,8103,8104,8105,8106
# analiza i rysunki
PYTHONPATH=backend:scripts/validation .venv/bin/python scripts/validation/routing_eval.py data/campaign <clusters.json>
PYTHONPATH=backend:scripts/validation .venv/bin/python scripts/validation/figures.py <links.json> data/campaign/report.json
```

Zależności analizy: `pip install -e ".[validation]"`.

## Błędy znalezione przez kampanię

**1. Emulacja CAD nie widziała transmisji odroczonych (naprawione).** `RadioMedium.busy_until`
sprawdzał tylko, czy kanał jest zajęty w chwili zgłoszenia. Pakiet odroczony przez CAD ma start
w przyszłości, więc nowy nadawca go nie widział i wchodził na niego — w prawdziwym radiu
odroczony węzeł zrobiłby CAD w chwili startu i ustąpił. Do tego sprawdzenie kanału
i rejestracja transmisji były osobnymi sekcjami krytycznymi, więc zgłoszenia z wątków dwóch
węzłów mogły jednocześnie uznać kanał za wolny. Objaw w T5: w sieci 10 węzłów przy
najmniejszym obciążeniu (G = 0,06) 15% prób kończyło się kolizją — więcej niż przy G = 0,2.
Kolidowały wyłącznie pakiety POSITION/NODEINFO, które wszystkie węzły nadają co 90 s
w tej samej chwili (wspólny start). Poprawka: `RadioMedium.place` sprawdza konflikt z każdą
słyszalną transmisją nakładającą się na planowany czas nadawania i rejestruje pakiet pod
jedną blokadą.

**2. Węzeł mógł nadawać dwa pakiety naraz (naprawione).** Firmware zgłasza kolejny pakiet,
nie wiedząc, że poprzedni został przesunięty przez emulację CAD; medium pomijało własne
transmisje węzła, więc oba pakiety nakładały się w czasie. Poprawka: własna, trwająca
transmisja zajmuje radio węzła.

**Po dwóch poprawkach CAD był idealny** — w sieci, w której każdy słyszy każdego, PDR wynosiło
1,000 przy każdym obciążeniu, bo nawet transmisje zgłoszone w odstępie 1 ms rozsuwały się.
Prawdziwy CAD trwa ułamek slotu: dwa węzły, które sprawdzą kanał w tym samym slocie, oba widzą
go wolnym i kolidują. Medium ma teraz okno podatności równe slotowi CAD (to samo
`slot_time_s`, którego firmware używa w backoffie; 12,7 ms dla MediumFast): transmisja
startująca bliżej niż slot od innej nie jest przez nią wykrywana. Własna transmisja węzła
blokuje go zawsze.

Odtworzenie zarejestrowanych zgłoszeń przebiegu `m-n10-l0.25-s2-c` na logice medium:
11 par nakładających się transmisji przed poprawkami, 0 po poprawkach 1–2 (idealny CAD)
i 15 z oknem slotu CAD. Te 15 to paczki POSITION/NODEINFO: wszystkie węzły startują
jednocześnie, więc ich 90-sekundowe zegary są zsynchronizowane, a firmware losuje opóźnienie
z 8 slotów — część trafia w ten sam slot. Fizycznie to poprawne, ale synchronizacja jest
artefaktem wspólnego startu kontenerów, dlatego T5 ocenia osobno sondy (Poisson) i pakiety
firmware. Testy regresyjne w `tests/test_radio_medium.py` (odroczona transmisja, własny
pakiet, start w tym samym slocie) i `tests/test_packet_trace.py`.
Wszystkie przebiegi z kolizjami powtórzono po poprawkach; wyniki sprzed poprawek
(`runs_v1/`) służą tylko do porównania.

**3. Eksport w skrypcie kampanii (nie w aplikacji).** Zapytanie do InfluxDB filtrowało tylko
po `experiment_id`, a powtórzony przebieg ma ten sam identyfikator — eksport łączył oba
przebiegi. Zapytanie jest teraz ograniczone do czasu przebiegu.

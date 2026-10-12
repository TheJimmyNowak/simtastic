"""Wspólne medium radiowe: czas w eterze, kolizje z efektem przechwycenia, CAD.

Model kolizji za Borem i in. (MSWiM '16, LoRaSim): dwa nakładające się w czasie pakiety
o tym samym SF i kanale — u danego odbiorcy przeżywa ten, który jest silniejszy co
najmniej o próg przechwycenia (6 dB); przy mniejszej różnicy giną oba. Zakłócenie, które
kończy się w pierwszych (preambuła − 5) symbolach preambuły, nie przeszkadza — odbiornik
zdąży się zsynchronizować na pozostałych symbolach.

Czas w eterze wg Semtech AN1200.13, z tymi samymi założeniami co firmware Meshtastic
(`SimRadio::getPacketTime`): nagłówek jawny, CRC, preambuła 16 symboli, LDRO dla
symbolu dłuższego niż 16 ms.

CAD: prawdziwy węzeł przed nadaniem sprawdza kanał (channel activity detection) i przy
zajętym kanale losuje nowe opóźnienie. `SimRadio` tego nie robi, więc medium przesuwa
start transmisji za koniec słyszalnego sygnału o losową liczbę slotów (CWmin, jak
`RadioInterface::getTxDelayMsec`).
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Callable

# Firmware Meshtastic: RadioInterface::preambleLength i nagłówek PacketHeader (16 B).
PREAMBLE_SYMBOLS = 16
PACKET_HEADER_BYTES = 16
# Bor i in.: odbiornik może stracić najwyżej (preambuła − 5) symboli preambuły.
PREAMBLE_LOCK_SYMBOLS = 5
# RadioInterface: CWmin oraz czas slotu = max(2.25, NUM_SYM_CAD + 0.5) symbolu + 7,6 ms.
CW_MIN = 3
_CAD_SYMBOLS = 2.5
_SLOT_OVERHEAD_S = (0.2 + 0.4 + 7) / 1000.0

OUTCOME_OK = "ok"
OUTCOME_WEAK = "weak"
OUTCOME_COLLISION = "collision"
OUTCOME_HALF_DUPLEX = "half_duplex"


def symbol_time_s(spreading_factor: int, bandwidth_khz: float) -> float:
    return (2 ** spreading_factor) / (bandwidth_khz * 1000.0)


def lora_airtime_s(
    payload_bytes: int,
    spreading_factor: int,
    bandwidth_khz: float,
    coding_rate: int,
    preamble_symbols: int = PREAMBLE_SYMBOLS,
) -> float:
    """Czas w eterze [s]; `coding_rate` to mianownik CR (5…8 dla 4/5…4/8)."""
    t_sym = symbol_time_s(spreading_factor, bandwidth_khz)
    low_data_rate = 1 if t_sym > 16e-3 else 0
    t_preamble = (preamble_symbols + 4.25) * t_sym
    payload_symbols = 8 + max(
        math.ceil((8 * payload_bytes - 4 * spreading_factor + 28 + 16) / (4 * (spreading_factor - 2 * low_data_rate)))
        * coding_rate,
        0,
    )
    return t_preamble + payload_symbols * t_sym


def slot_time_s(spreading_factor: int, bandwidth_khz: float) -> float:
    return _CAD_SYMBOLS * symbol_time_s(spreading_factor, bandwidth_khz) + _SLOT_OVERHEAD_S


@dataclass
class Transmission:
    """Jedna transmisja w eterze i jej moc u każdego sąsiada nadawcy."""

    tx_id: str
    packet_id: int
    start: float
    end: float
    rssi_at: dict[str, float] = field(default_factory=dict)
    # Pierwotny nadawca (pole `from`): id pakietu jest unikalne tylko razem z nim.
    origin: int | None = None
    # Odroczona (CAD) retransmisja anulowana, bo węzeł usłyszał duplikat przed jej startem.
    cancelled: bool = False

    def overlaps(self, other: "Transmission") -> bool:
        return self.start < other.end and other.start < self.end


class RadioMedium:
    """Transmisje w eterze z ostatnich sekund; bezpieczne dla wielu wątków."""

    def __init__(
        self,
        *,
        sensitivity_dbm: float,
        capture_threshold_db: float,
        spreading_factor: int,
        bandwidth_khz: float,
        history_s: float = 60.0,
    ) -> None:
        self.sensitivity_dbm = sensitivity_dbm
        self.capture_threshold_db = capture_threshold_db
        self.preamble_lock_s = (PREAMBLE_SYMBOLS - PREAMBLE_LOCK_SYMBOLS) * symbol_time_s(
            spreading_factor, bandwidth_khz
        )
        self.history_s = history_s
        # CAD trwa ułamek slotu: transmisja, która zaczęła się krócej niż slot temu (albo
        # zacznie się w tym samym slocie), nie jest jeszcze wykrywalna — dwa węzły
        # sprawdzające kanał w tym samym slocie nadają oba i kolidują.
        self.cad_window_s = slot_time_s(spreading_factor, bandwidth_khz)
        self._air: list[Transmission] = []
        self._lock = threading.Lock()

    def busy_until(self, node_id: str, t: float, end: float | None = None) -> float | None:
        """Koniec sygnału słyszalnego przez węzeł w chwili t (None = kanał wolny).

        Z `end` sprawdza cały przedział [t, end): także transmisje już zaplanowane na
        później (odroczone przez CAD) — ich węzły zrobią CAD w chwili startu i nie
        nadadzą w trakcie cudzego pakietu, więc nowy pakiet nie może na nie wejść.
        Wyjątkiem są transmisje startujące w odstępie krótszym niż slot CAD.
        """
        with self._lock:
            return self._busy_until_locked(node_id, t, end)

    def _busy_until_locked(self, node_id: str, t: float, end: float | None) -> float | None:
        # Zajęte jest też własne radio węzła: firmware zgłasza kolejny pakiet, nie wiedząc,
        # że poprzedni został odroczony przez emulację CAD i jeszcze trwa.
        ends = []
        for tx in self._air:
            if not (tx.start <= t < tx.end if end is None else tx.start < end and t < tx.end):
                continue
            if tx.tx_id == node_id:
                ends.append(tx.end)
            elif tx.rssi_at.get(node_id, -math.inf) >= self.sensitivity_dbm and (
                end is None or abs(tx.start - t) >= self.cad_window_s
            ):
                ends.append(tx.end)
        return max(ends) if ends else None

    def add(self, transmission: Transmission) -> None:
        with self._lock:
            self._add_locked(transmission)

    def _add_locked(self, transmission: Transmission) -> None:
        horizon = transmission.start - self.history_s
        self._air = [tx for tx in self._air if tx.end >= horizon]
        self._air.append(transmission)

    def place(
        self,
        tx_id: str,
        packet_id: int,
        start: float,
        airtime: float,
        rssi_at: dict[str, float],
        backoff: Callable[[], float],
        origin: int | None = None,
    ) -> Transmission:
        """Emulacja CAD: przesuwa start za słyszalne transmisje (o `backoff()` po każdej)
        i rejestruje pakiet. Sprawdzenie i rejestracja są atomowe — zgłoszenia z wątków
        różnych węzłów nie mogą jednocześnie uznać kanału za wolny."""
        with self._lock:
            while (busy_end := self._busy_until_locked(tx_id, start, start + airtime)) is not None:
                start = busy_end + backoff()
            transmission = Transmission(tx_id, packet_id, start, start + airtime, rssi_at, origin)
            self._add_locked(transmission)
        return transmission

    def cancel_pending(
        self, tx_id: str, packet_id: int, heard_at: float, origin: int | None = None
    ) -> Transmission | None:
        """Usuwa transmisję pakietu (`origin`, `packet_id`) węzła `tx_id`, która zaczyna się
        po `heard_at`.

        Prawdziwy węzeł przy zajętym kanale trzyma pakiet w kolejce i anuluje retransmisję,
        gdy w tym czasie odbierze jej duplikat (`FloodingRouter::perhapsCancelDupe`).
        Firmware w SimRadio zgłosił już ten pakiet, więc anulowanie wykonuje medium.
        """
        with self._lock:
            for tx in self._air:
                if (
                    tx.tx_id == tx_id
                    and tx.packet_id == packet_id
                    and tx.origin == origin
                    and tx.start > heard_at
                ):
                    self._air.remove(tx)
                    tx.cancelled = True
                    return tx
        return None

    def outcome(self, transmission: Transmission, rx_id: str) -> str:
        """Wynik odbioru `transmission` przez `rx_id` po jej zakończeniu."""
        power = transmission.rssi_at.get(rx_id, -math.inf)
        if power < self.sensitivity_dbm:
            return OUTCOME_WEAK
        with self._lock:
            others = [tx for tx in self._air if tx is not transmission and tx.overlaps(transmission)]
        for other in others:
            if other.tx_id == rx_id:
                return OUTCOME_HALF_DUPLEX  # odbiorca sam nadawał
        for other in others:
            interference = other.rssi_at.get(rx_id)
            if interference is None:
                continue  # poza sąsiedztwem odbiorcy — pomijalnie słaby
            if other.end <= transmission.start + self.preamble_lock_s:
                continue  # zakłócił tylko początek preambuły
            if power - interference < self.capture_threshold_db:
                return OUTCOME_COLLISION
        return OUTCOME_OK

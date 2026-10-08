import pytest

from app.services.radio_medium import (
    OUTCOME_COLLISION,
    OUTCOME_HALF_DUPLEX,
    OUTCOME_OK,
    OUTCOME_WEAK,
    RadioMedium,
    Transmission,
    lora_airtime_s,
    symbol_time_s,
)


def medium() -> RadioMedium:
    # LONG_FAST: SF11, 250 kHz, czułość -131,5 dBm
    return RadioMedium(sensitivity_dbm=-131.5, capture_threshold_db=6.0, spreading_factor=11, bandwidth_khz=250)


def test_airtime_matches_semtech_formula() -> None:
    # SF11/250 kHz, CR 4/5, preambuła 16, 16 B: 20,25 + 23 symbole po 8,192 ms
    assert lora_airtime_s(16, 11, 250, 5) == pytest.approx(0.3543, abs=1e-4)
    assert lora_airtime_s(61, 9, 250, 5) > lora_airtime_s(16, 9, 250, 5)


def test_equal_overlapping_packets_both_collide() -> None:
    m = medium()
    a = Transmission("a", 1, 0.0, 0.4, {"b": -100.0})
    c = Transmission("c", 2, 0.1, 0.5, {"b": -102.0})
    m.add(a)
    m.add(c)
    assert m.outcome(a, "b") == OUTCOME_COLLISION
    assert m.outcome(c, "b") == OUTCOME_COLLISION


def test_stronger_packet_is_captured() -> None:
    m = medium()
    strong = Transmission("a", 1, 0.0, 0.4, {"b": -90.0})
    weak = Transmission("c", 2, 0.1, 0.5, {"b": -96.0})
    m.add(strong)
    m.add(weak)
    assert m.outcome(strong, "b") == OUTCOME_OK
    assert m.outcome(weak, "b") == OUTCOME_COLLISION


def test_interferer_ending_early_in_preamble_is_harmless() -> None:
    m = medium()
    t_sym = symbol_time_s(11, 250)
    earlier = Transmission("c", 2, -0.3, 10 * t_sym, {"b": -100.0})
    late = Transmission("a", 1, 0.0, 0.4, {"b": -100.0})
    m.add(earlier)
    m.add(late)
    # Zakłócenie kończy się po 10 z 16 symboli preambuły — synchronizacja na pozostałych.
    assert m.outcome(late, "b") == OUTCOME_OK
    # Zakłócenie trwające do 12. symbolu preambuły psuje synchronizację.
    later = Transmission("c", 3, 0.5, 0.6 + 12 * t_sym, {"b": -100.0})
    victim = Transmission("a", 4, 0.6, 1.0, {"b": -100.0})
    m.add(later)
    m.add(victim)
    assert m.outcome(victim, "b") == OUTCOME_COLLISION


def test_receiver_cannot_hear_while_transmitting() -> None:
    m = medium()
    incoming = Transmission("a", 1, 0.0, 0.4, {"b": -80.0})
    own = Transmission("b", 2, 0.2, 0.6, {"a": -80.0})
    m.add(incoming)
    m.add(own)
    assert m.outcome(incoming, "b") == OUTCOME_HALF_DUPLEX


def test_weak_packet_and_cad() -> None:
    m = medium()
    faint = Transmission("a", 1, 0.0, 0.4, {"b": -140.0, "c": -100.0})
    m.add(faint)
    assert m.outcome(faint, "b") == OUTCOME_WEAK
    # CAD wykrywa tylko sygnały powyżej czułości.
    assert m.busy_until("b", 0.2) is None
    assert m.busy_until("c", 0.2) == pytest.approx(0.4)
    assert m.busy_until("c", 0.5) is None


def test_cad_respects_transmission_deferred_to_later() -> None:
    m = medium()
    on_air = m.place("a", 1, 0.0, 0.4, {"b": -100.0, "c": -100.0}, lambda: 0.05)
    # b słyszy a i odracza nadanie za jego koniec (+ backoff).
    deferred = m.place("b", 2, 0.1, 0.4, {"a": -100.0, "c": -100.0}, lambda: 0.05)
    assert deferred.start == pytest.approx(0.45)
    # c zgłasza pakiet, gdy a już skończył, a b jeszcze nie zaczął: nie może wejść na b.
    late = m.place("c", 3, 0.40, 0.4, {"a": -100.0, "b": -100.0}, lambda: 0.05)
    assert late.start >= deferred.end
    assert m.outcome(deferred, "a") == OUTCOME_OK
    assert m.outcome(late, "a") == OUTCOME_OK
    assert on_air.end <= deferred.start


def test_node_does_not_overlap_its_own_deferred_packet() -> None:
    m = medium()
    m.place("a", 1, 0.0, 0.4, {"b": -100.0}, lambda: 0.05)
    first = m.place("b", 2, 0.1, 0.4, {"a": -100.0}, lambda: 0.05)  # odroczony do 0,45
    # Firmware b zgłasza następny pakiet, zanim odroczony się skończył.
    second = m.place("b", 3, 0.5, 0.4, {"a": -100.0}, lambda: 0.05)
    assert second.start >= first.end


def test_transmissions_starting_in_same_cad_slot_collide() -> None:
    m = medium()
    a = m.place("a", 1, 0.0, 0.4, {"b": -100.0, "c": -100.0}, lambda: 0.05)
    # c sprawdza kanał 5 ms po starcie a — krócej niż slot CAD, więc nie wykrywa a.
    c = m.place("c", 2, 0.005, 0.4, {"a": -100.0, "b": -100.0}, lambda: 0.05)
    assert c.start == pytest.approx(0.005)
    assert m.outcome(a, "b") == OUTCOME_COLLISION
    assert m.outcome(c, "b") == OUTCOME_COLLISION

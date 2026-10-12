import math
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class LoraPreset(BaseModel):
    name: str
    frequency_mhz: float
    bandwidth_khz: float
    spreading_factor: int
    coding_rate: int
    tx_power_dbm: float
    sensitivity_dbm: float
    noise_floor_dbm: float

# Region EU_868 (Polska): jedyny kanał 250 kHz w paśmie 869.4–869.65 MHz, limit 27 dBm.
EU_868_FREQUENCY_MHZ = 869.525
# Typowe węzły (SX1262) nadają z mocą maksymalną układu, 22 dBm — firmware Meshtastic
# przy tx_power = 0 używa maksimum układu, a nie limitu regionu.
LORA_TX_POWER_DBM = 22.0
LORA_NOISE_FIGURE_DB = 6.0
# Minimalny SNR demodulacji LoRa wg Semtech (SX126x) dla SF7…SF12.
_LORA_MIN_SNR_DB = {7: -7.5, 8: -10.0, 9: -12.5, 10: -15.0, 11: -17.5, 12: -20.0}


def lora_noise_floor_dbm(bandwidth_khz: float) -> float:
    """Szum odbiornika = -174 dBm/Hz + 10·log10(BW) + NF."""
    return round(-174.0 + 10.0 * math.log10(bandwidth_khz * 1000.0) + LORA_NOISE_FIGURE_DB, 1)


def lora_sensitivity_dbm(spreading_factor: int, bandwidth_khz: float) -> float:
    """Czułość = szum odbiornika + SNR_min(SF)."""
    snr_min = _LORA_MIN_SNR_DB.get(spreading_factor, -2.5 * (spreading_factor - 4))
    return round(lora_noise_floor_dbm(bandwidth_khz) + snr_min, 1)


def _preset(name: str, bandwidth_khz: float, spreading_factor: int, coding_rate: int = 5) -> LoraPreset:
    return LoraPreset(
        name=name,
        frequency_mhz=EU_868_FREQUENCY_MHZ,
        bandwidth_khz=bandwidth_khz,
        spreading_factor=spreading_factor,
        coding_rate=coding_rate,
        tx_power_dbm=LORA_TX_POWER_DBM,
        sensitivity_dbm=lora_sensitivity_dbm(spreading_factor, bandwidth_khz),
        noise_floor_dbm=lora_noise_floor_dbm(bandwidth_khz),
    )


# Parametry zgodne z presetami firmware Meshtastic (Config.LoRaConfig.ModemPreset).
LORA_PRESETS = {
    "LONG_FAST": _preset("LONG_FAST", 250, 11),
    "MEDIUM_SLOW": _preset("MEDIUM_SLOW", 250, 10),
    "MEDIUM_FAST": _preset("MEDIUM_FAST", 250, 9),
    "SHORT_FAST": _preset("SHORT_FAST", 250, 7),
    "SHORT_TURBO": _preset("SHORT_TURBO", 500, 7),
}


class EnvironmentConfig(BaseModel):
    preset: str = Field(
        default="LONG_FAST",
        description="Nazwa presetu LoRa (np. LONG_FAST) lub custom — patrz LORA_PRESETS.",
    )
    custom_frequency_mhz: Optional[float] = Field(
        default=None, description="Nadpisanie częstotliwości [MHz] gdy preset lub custom."
    )
    custom_bandwidth_khz: Optional[float] = Field(
        default=None, description="Nadpisanie szerokości pasma [kHz]."
    )
    custom_spreading_factor: Optional[int] = Field(
        default=None, description="Nadpisanie spreading factor."
    )
    custom_coding_rate: Optional[int] = Field(default=None, description="Nadpisanie coding rate.")
    custom_tx_power_dbm: Optional[float] = Field(
        default=None, description="Nadpisanie mocy nadawczej EIRP-side [dBm] (z anteną TX w węźle)."
    )
    custom_sensitivity_dbm: Optional[float] = Field(
        default=None,
        description="Nadpisanie progu czułości odbiornika [dBm] używanego do delivered (RSSI).",
    )
    fading_sigma_db: float = Field(
        default=1.0,
        ge=0,
        description=(
            "Odchylenie standardowe fadingu [dB], N(0,σ²) na każdą transmisję w runtime. "
            "Domyślnie 1 dB: rozrzut RSSI kolejnych pakietów na łączach stacjonarnych w danych "
            "lorastats.pl (docs/test_campaign.md)."
        ),
    )
    itm_climate: int = Field(
        default=5,
        ge=1,
        le=7,
        description="Klimat ITM (1–7), parametr Longley–Rice przy precompute.",
    )
    itm_polarization: int = Field(
        default=1,
        ge=0,
        le=1,
        description="Polaryzacja ITM (0 – pozioma, 1 – pionowa), parametr Longley–Rice.",
    )
    connectivity_threshold_dbm: float = Field(
        default=-140.0,
        description=(
            "Próg medianowego RSSI (bez fadingu) z ITM: krawędź TX→RX w grafie sąsiadów, "
            "jeśli median_rssi_db >= ten próg."
        ),
    )
    experiment_id: str = Field(
        default="default",
        description="Identyfikator eksperymentu (tagi Influx / trace).",
    )
    random_seed: int | None = Field(
        default=None,
        description="Seed generatora fadingu; taki sam seed daje powtarzalny przebieg RF.",
    )
    building_model_enabled: bool = Field(
        default=True,
        description="Uwzględnia budynki LoD1 jako przeszkody zewnętrzne w precompute.",
    )
    fresnel_clearance_ratio: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="Wymagana wolna część promienia pierwszej strefy Fresnela.",
    )
    clutter_loss_db: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Dodatkowa strata otoczenia [dB] doliczana do każdego łącza: zabudowa i roślinność "
            "niewidoczne w SRTM, anteny w budynkach, kable. Kalibracja: docs/validation_lorastats.md."
        ),
    )
    link_shadowing_sigma_db: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Odchylenie trwałego cieniowania łącza [dB]: losowane raz na parę węzłów przy "
            "precompute (to samo w obu kierunkach), w odróżnieniu od fadingu losowanego "
            "na każdą transmisję."
        ),
    )
    node_installation_sigma_db: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Odchylenie straty instalacji węzła [dB] (antena w budynku, kable, otoczenie masztu): "
            "losowane raz na węzeł i doliczane do wszystkich jego łączy, w obu kierunkach. "
            "Seed jak cieniowanie łącza."
        ),
    )
    link_blocked_fraction: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Odsetek par węzłów bez łącza bezpośredniego niezależnie od propagacji (model mieszaniny "
            "z kalibracji). Losowane raz na parę, symetrycznie; seed jak cieniowanie łącza."
        ),
    )
    link_shadowing_seed: int | None = Field(
        default=None,
        description="Seed trwałego cieniowania; brak = nowe losowanie przy każdym precompute.",
    )
    collisions_enabled: bool = Field(
        default=True,
        description=(
            "Kolizje w eterze: odbiór oceniany po zakończeniu pakietu (przechwycenie, "
            "półdupleks) i emulacja CAD przed nadaniem. Wyłączone = każdy odbiór powyżej "
            "czułości dociera natychmiast."
        ),
    )
    capture_threshold_db: float = Field(
        default=6.0,
        ge=0.0,
        description="Minimalna przewaga mocy nad nakładającym się pakietem, przy której pakiet przeżywa.",
    )

    @field_validator("preset")
    @classmethod
    def preset_must_exist(cls, value: str) -> str:
        if value != "custom" and value not in LORA_PRESETS:
            allowed = ", ".join([*LORA_PRESETS, "custom"])
            raise ValueError(f"unknown LoRa preset {value!r}; allowed: {allowed}")
        return value

    @field_validator("experiment_id")
    @classmethod
    def experiment_id_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("experiment_id required")
        return value

    def resolve_lora(self) -> LoraPreset:
        base = LORA_PRESETS.get(self.preset) or _preset("custom", 250, 10)
        bandwidth = self.custom_bandwidth_khz if self.custom_bandwidth_khz is not None else base.bandwidth_khz
        sf = self.custom_spreading_factor if self.custom_spreading_factor is not None else base.spreading_factor
        return LoraPreset(
            name=base.name,
            frequency_mhz=self.custom_frequency_mhz if self.custom_frequency_mhz is not None else base.frequency_mhz,
            bandwidth_khz=bandwidth,
            spreading_factor=sf,
            coding_rate=self.custom_coding_rate if self.custom_coding_rate is not None else base.coding_rate,
            tx_power_dbm=self.custom_tx_power_dbm if self.custom_tx_power_dbm is not None else base.tx_power_dbm,
            sensitivity_dbm=(
                self.custom_sensitivity_dbm
                if self.custom_sensitivity_dbm is not None
                else lora_sensitivity_dbm(sf, bandwidth)
            ),
            noise_floor_dbm=lora_noise_floor_dbm(bandwidth),
        )

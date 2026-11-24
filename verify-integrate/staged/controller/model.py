from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, assert_never


COALESCE_SECONDS: Final = 0.4
MIN_APPLY_SECONDS: Final = 2.0
VOLUME_PATTERN: Final = re.compile(r"/\s*(\d{1,3})%\s*/")

type PropertyValue = bool | float | int


class Zone(StrEnum):
    LOW = "low"
    MID = "mid"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class PropertyChange:
    plugin: str
    name: str
    value: PropertyValue


@dataclass(slots=True)  # noqa: MUTABLE_OK - exception traceback state is mutable
class InvalidVolumeError(Exception):
    response: str

    def __str__(self) -> str:
        return "physical speaker volume could not be parsed"


def parse_volume(response: str) -> int:
    volumes = parse_channel_volumes(response)
    if not volumes:
        raise InvalidVolumeError(response=response)
    return volumes[0]


def parse_channel_volumes(response: str) -> tuple[int, ...]:
    return tuple(int(match.group(1)) for match in VOLUME_PATTERN.finditer(response))


def property_value_text(value: PropertyValue) -> str:
    match value:
        case bool() as enabled:
            return str(enabled).lower()
        case int() | float():
            return str(value)
        case unreachable:
            assert_never(unreachable)


def set_command(change: PropertyChange) -> str:
    value = property_value_text(change.value)
    return f"set_property:output:{change.plugin}:0:{change.name}:{value}"


def get_command(change: PropertyChange) -> str:
    return f"get_property:output:{change.plugin}:0:{change.name}"


def property_response_matches(change: PropertyChange, response: str) -> bool:
    match change.value:
        case bool() as enabled:
            return response.strip() == str(enabled).lower()
        case int() | float() as expected:
            try:
                observed = float(response.strip())
            except ValueError:
                return False
            return math.isclose(observed, float(expected), abs_tol=1e-6)
        case unreachable:
            assert_never(unreachable)


def apply_deadline(event_at: float, last_apply_at: float) -> float:
    return max(event_at + COALESCE_SECONDS, last_apply_at + MIN_APPLY_SECONDS)


def zone_for_volume(volume: int, current: Zone | None) -> Zone:
    match current:
        case Zone.LOW:
            if volume < 40:
                return Zone.LOW
        case Zone.HIGH:
            if volume > 70:
                return Zone.HIGH
        case Zone.MID | None:
            pass
        case unreachable:
            assert_never(unreachable)

    if volume <= 30:
        return Zone.LOW
    if volume >= 80:
        return Zone.HIGH
    return Zone.MID


# Warm · Rich follows the volume setting, which PipeWire applies AFTER EasyEffects
# (software volume on the speaker sink), so the chain alone cannot know it:
#  - below his usual 80 % it adds the ISO 226:2023 equal-loudness difference (LSP
#    loudness compensator, output gain cancels its built-in attenuation), so quiet
#    listening keeps its body instead of going thin;
#  - above about 71 % it lowers the limiter ceiling so the amplifiers never receive
#    more than 0.4 of full scale, where their own protection starts to distort.
# Both are bounded: the bass lift is always smaller than the volume cut it follows,
# so the speakers never get more than they do at the reference volume.
TRACKING_REFERENCE_DB: Final = -5.8  # 80 % on the cubic volume scale
AMPLIFIER_KNEE_DB: Final = -8.0  # 0.4 of full scale, from the 2026-07 acoustic test
LIMITER_CEILING_DB: Final = -1.0
LIMITER_FLOOR_DB: Final = -9.0
LOUDNESS_FLOOR_DB: Final = -30.0


def volume_db(volume: int) -> float:
    return 60.0 * math.log10(max(volume, 1) / 100.0)


def volume_tracking_changes(volume: int) -> tuple[PropertyChange, ...]:
    level = volume_db(volume)
    contour = math.ceil(max(LOUDNESS_FLOOR_DB, min(0.0, level - TRACKING_REFERENCE_DB)) * 2) / 2
    ceiling = math.floor(max(LIMITER_FLOOR_DB, min(LIMITER_CEILING_DB, AMPLIFIER_KNEE_DB - level)) * 2) / 2
    return (
        PropertyChange("loudness", "volume", contour + 0.0),
        PropertyChange("loudness", "outputGain", -contour + 0.0),
        PropertyChange("limiter", "threshold", ceiling),
    )


# Warm · Deep runs on the PipeWire filter-chain speaker engine (omarchy-speaker-engine): the
# Warm · Rich chain with its loudness contour rebuilt from builtin filters (LSP's compensator
# keeps a residue of its last volume step, so its level depends on the volume history) plus
# the perceived-bass generator. The law above is translated, never re-derived: Rich's contour value
# becomes a depth (0 at the reference volume, 1 at the -30 dB floor; the engine's contour is
# fitted to Rich's at every value and needs no make-up gain), the ceiling a linear gain. The
# graph glides the depth and the amount over ~80 ms, so no step of the law can click.
ENGINE_CONTOUR: Final = ("contour", "Add")
ENGINE_CEILING: Final = ("lim", "th")
ENGINE_DEEP_BASS: Final = ("deep", "Add")


def db_to_gain(decibels: float) -> float:
    return 10.0 ** (decibels / 20.0)


def limiter_ceiling_db(volume: int) -> float:
    return float(next(c.value for c in volume_tracking_changes(volume) if c.name == "threshold"))


def contour_depth(volume: int) -> float:
    contour = next(float(c.value) for c in volume_tracking_changes(volume) if c.name == "volume")
    return round(contour / LOUDNESS_FLOOR_DB, 6) + 0.0


def deep_bass_amount(volume: int, deep_bass: float) -> float:
    """The perceived-bass generator follows the volume like the loudness contour does:
    loudness-matched up to his reference volume (the graph's bands carry the ISO 226 match),
    then faded 2 dB for every 1 dB the limiter ceiling comes down, because the ear needs less
    bass help when it is loud and, with the amplifiers at their knee, the harmonics would only
    take headroom from the music."""
    fade_db = 2.0 * min(0.0, limiter_ceiling_db(volume) - limiter_ceiling_db(REFERENCE_VOLUME))
    return round(deep_bass * db_to_gain(fade_db), 4)


REFERENCE_VOLUME: Final = 80  # TRACKING_REFERENCE_DB, as the volume setting


def engine_tracking_changes(volume: int, deep_bass: float) -> tuple[PropertyChange, ...]:
    return (
        PropertyChange(*ENGINE_CONTOUR, contour_depth(volume)),
        PropertyChange(*ENGINE_CEILING, db_to_gain(limiter_ceiling_db(volume))),
        PropertyChange(*ENGINE_DEEP_BASS, deep_bass_amount(volume, deep_bass)),
    )


# Studio DSP (libxps17_studio_dsp.so, 92-xps17-studio.conf) accepts the live listening
# level directly in dB (`studio:Volume (dB)` in [-45.0, 0.0]) and computes its own
# ISO 226:2023 equal-loudness contour and Klippel excursion-tracking sliding HPF inside
# the plugin. Updating only `studio:Volume (dB)` keeps loudness compensation locked to
# the PipeWire volume continuously across 0..100 % (fixing the frozen-contour bug of
# 91-spatial-fdtr.conf) while leaving `studio:Drive (dB)` (LOUDNESS_DRIVE_DB mastering
# loudness authority) untouched.
STUDIO_VOLUME_CONTROL: Final = ("studio", "Volume (dB)")
STUDIO_VOLUME_MIN_DB: Final = -45.0
STUDIO_VOLUME_MAX_DB: Final = 0.0
STUDIO_HIGH_PASS_PARAM: Final = "studio:Physical Bass Hz"


def studio_volume_db(volume: int) -> float:
    """Convert PipeWire cubic sink volume percentage (0..100+ %) to `studio:Volume (dB)`,
    clamped strictly to [STUDIO_VOLUME_MIN_DB, STUDIO_VOLUME_MAX_DB] (-45.0..0.0 dB)."""
    if volume <= 0:
        return STUDIO_VOLUME_MIN_DB
    clamped = max(STUDIO_VOLUME_MIN_DB, min(STUDIO_VOLUME_MAX_DB, volume_db(volume)))
    return round(clamped, 3) + 0.0


def studio_tracking_changes(volume: int) -> tuple[PropertyChange, ...]:
    """Live PipeWire volume update for xps17_studio_dsp filter-chain nodes."""
    return (PropertyChange(*STUDIO_VOLUME_CONTROL, studio_volume_db(volume)),)


def engine_param_key(change: PropertyChange) -> str:
    return f"{change.plugin}:{change.name}"


def engine_props(changes: tuple[PropertyChange, ...]) -> str:
    """SPA-JSON for `pw-cli set-param <node> Props`: one atomic batch of controls."""
    body = " ".join(f'"{engine_param_key(change)}" {float(change.value)!r}' for change in changes)
    return f"{{ params = [ {body} ] }}"


def gain_safe_batches(
    changes: tuple[PropertyChange, ...],
    current: dict[tuple[str, str], float | None],
) -> tuple[tuple[PropertyChange, ...], tuple[PropertyChange, ...]]:
    """The gain-safe order as two batches: everything that lowers the level, then the rest."""
    ordered = gain_safe_order(changes, current)

    def lowers(change: PropertyChange) -> bool:
        observed = current.get((change.plugin, change.name))
        return observed is None or float(change.value) <= observed

    split = sum(1 for change in ordered if lowers(change))
    return ordered[:split], ordered[split:]


def gain_safe_order(
    changes: tuple[PropertyChange, ...],
    current: dict[tuple[str, str], float | None],
) -> tuple[PropertyChange, ...]:
    """Send every change that lowers the level before any that raises it.

    EasyEffects applies each property on its own audio cycle, so a half-applied
    update must be momentarily quieter, never louder: raising the volume from 30 %
    would otherwise apply the flat contour while its +25 dB make-up gain is still in
    place, and lowering it from 100 % would raise the limiter before the contour.
    """

    def lowers(change: PropertyChange) -> bool:
        observed = current.get((change.plugin, change.name))
        return observed is None or float(change.value) <= observed

    return (
        *(change for change in changes if lowers(change)),
        *(change for change in changes if not lowers(change)),
    )


def changes_for_zone(zone: Zone) -> tuple[PropertyChange, ...]:
    match zone:
        case Zone.LOW:
            return (
                PropertyChange("equalizer", "outputGain", -1.0),
                PropertyChange("loudness", "bypass", True),
                PropertyChange("loudness", "volume", -6.0),
                PropertyChange("loudness", "outputGain", 6.0),
                PropertyChange("bass_enhancer", "amount", 5.0),
                PropertyChange("bass_enhancer", "blend", 0.6),
                PropertyChange("multiband_compressor", "band0Ratio", 1.4),
                PropertyChange("multiband_compressor", "band0AttackThreshold", -12.0),
                PropertyChange("multiband_compressor", "band0Makeup", 1.0),
                PropertyChange("stereo_tools", "stereoBase", 0.08),
                PropertyChange("limiter", "inputGain", 17.0),
                PropertyChange("loudness", "bypass", False),
            )
        case Zone.MID:
            return (
                PropertyChange("equalizer", "outputGain", -1.0),
                PropertyChange("loudness", "bypass", True),
                PropertyChange("loudness", "volume", 0.0),
                PropertyChange("loudness", "outputGain", 0.0),
                PropertyChange("bass_enhancer", "amount", 4.0),
                PropertyChange("bass_enhancer", "blend", 0.55),
                PropertyChange("multiband_compressor", "band0Ratio", 1.5),
                PropertyChange("multiband_compressor", "band0AttackThreshold", -12.0),
                PropertyChange("multiband_compressor", "band0Makeup", 1.0),
                PropertyChange("stereo_tools", "stereoBase", 0.08),
                PropertyChange("limiter", "inputGain", 17.0),
            )
        case Zone.HIGH:
            return (
                PropertyChange("equalizer", "outputGain", -1.0),
                PropertyChange("loudness", "bypass", True),
                PropertyChange("loudness", "volume", 0.0),
                PropertyChange("loudness", "outputGain", 0.0),
                PropertyChange("bass_enhancer", "amount", 5.0),
                PropertyChange("bass_enhancer", "blend", 0.6),
                PropertyChange("multiband_compressor", "band0Ratio", 1.3),
                PropertyChange("multiband_compressor", "band0AttackThreshold", -10.0),
                PropertyChange("multiband_compressor", "band0Makeup", 1.5),
                PropertyChange("stereo_tools", "stereoBase", 0.08),
                PropertyChange("limiter", "inputGain", 17.0),
            )
        case unreachable:
            assert_never(unreachable)

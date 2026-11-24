from __future__ import annotations

import fcntl
import json
import math
import os
import re
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from omarchy_audio.model import (
    PropertyChange,
    STUDIO_HIGH_PASS_PARAM,
    Zone,
    changes_for_zone,
    engine_props,
    engine_tracking_changes,
    gain_safe_batches,
    gain_safe_order,
    get_command,
    parse_channel_volumes,
    parse_volume,
    property_response_matches,
    set_command,
    studio_tracking_changes,
    volume_tracking_changes,
    zone_for_volume,
)
from omarchy_audio.server import LocalServer


PHYSICAL_SINK: Final = "alsa_output.pci-0000_00_1f.3-platform-sof_sdw.HiFi__Speaker__sink"
EASY_EFFECTS_SINK: Final = "easyeffects_sink"
INTERNAL_AUDIO_CARD: Final = "sof-soundwire"
SPEAKER_PROFILE_NAME: Final = "HiFi: Speaker: sink"
FALLBACK_SINK_SUFFIX: Final = ".stereo-fallback"
PRESET_NAME: Final = "XPS17-Adaptive-Premium-v2"
DEFAULT_PRESET_NAME: Final = "XPS17-FIR-Warm"
SPEAKER_PROFILE_PRESETS: Final = {
    "baseline": PRESET_NAME,
    "balanced": "XPS17-FIR-Balanced",
    "warm": "XPS17-FIR-Warm",
    "voice": "XPS17-FIR-Voice",
    "flat": "XPS17-FIR-Flat",
    "warm-clear": "XPS17-FIR-Warm-Clear",
    "warm-full": "XPS17-FIR-Warm",  # retired trial of the corrected Warm
    "warm-classic": "XPS17-FIR-Warm-Classic",  # Warm as it sounded until 2026-11-11
    "warm-rich": "XPS17-FIR-Warm-Rich",  # Warm refit + more body, follows the volume
    "balanced-eq": "XPS17-Tuned-Balanced",
    "warm-eq": "XPS17-Tuned-Warm",
    "voice-eq": "XPS17-Tuned-Voice",
    "flat-eq": "XPS17-Tuned-Flat",
}
# Profiles hosted by the PipeWire filter-chain speaker engine instead of EasyEffects, with
# their perceived-bass amount: 1.0 gives the harmonics the loudness the missing notes would
# have had at his reference volume (ISO 226:2023, built into the graph's bands), Subtle is
# 6 dB less. Every other profile stays on EasyEffects exactly as before.
EASY_EFFECTS_ENGINE: Final = "easyeffects"
FILTER_CHAIN_ENGINE: Final = "filter-chain"
ENGINE_PROFILES: Final = {"warm-deep": 1.0, "warm-deep-subtle": 0.5}
ENGINE_NODE: Final = "omarchy_speaker_engine"
ENGINE_OUTPUT_NODE: Final = f"{ENGINE_NODE}_output"  # its playback stream, after the limiter
ENGINE_HIGH_PASS_PARAM: Final = "hp:f"
# Studio DSP profiles hosted by 92-xps17-studio.conf inside omarchy-speaker-engine.service.
# Each profile owns a distinct passive PipeWire filter-chain sink whose `studio` LADSPA
# node (`xps17_studio_dsp`) tracks live PipeWire volume via `studio:Volume (dB)` across
# 0..100 %, while keeping its mastering loudness drive (`Drive (dB)`) authoritative.
STUDIO_PROFILES: Final = {
    "studio-deep": "omarchy_speaker_studio",
    "studio-spatial": "omarchy_speaker_studio_spatial",
    "studio-ref": "omarchy_speaker_studio_ref",
    "studio-voice": "omarchy_speaker_studio_voice",
}
STUDIO_NODE_TO_PROFILE: Final = {node: name for name, node in STUDIO_PROFILES.items()}
STUDIO_NODES: Final = frozenset(STUDIO_PROFILES.values())
STUDIO_OUTPUT_NODES: Final = frozenset(f"{node}_output" for node in STUDIO_NODES)
LEGACY_SPATIAL_NODES: Final = frozenset(("omarchy_speaker_spatial", "omarchy_speaker_spatial_71"))
LEGACY_SPATIAL_OUTPUT_NODES: Final = frozenset(f"{node}_output" for node in LEGACY_SPATIAL_NODES)
ALL_FILTER_CHAIN_PROFILES: Final = frozenset((*ENGINE_PROFILES, *STUDIO_PROFILES))
ALL_ENGINE_NODES: Final = frozenset((ENGINE_NODE, *STUDIO_NODES))
ALL_ENGINE_OUTPUT_NODES: Final = frozenset((ENGINE_OUTPUT_NODE, *STUDIO_OUTPUT_NODES))
# pactl's name for "whatever the default output is". Moving a stream there clears its
# target (pipewire-pulse writes target.object -1), so the stream keeps following the
# default instead of being pinned, and WirePlumber forgets any target it remembered for
# that application. pactl does the same for the name of the sink that is currently the
# default, so a stream can only be pinned to the engine through the metadata itself.
FOLLOW_DEFAULT_SINK: Final = "@DEFAULT_SINK@"
# The engine sink sits at a fixed 100 % and unmuted, as Omarchy's speaker tunings do:
# a volume in front of the limiter changes the tone, not the loudness, which lives on the
# physical speaker beneath it. Its output stream is held there too: it comes after the
# limiter, where anything above 100 % would undo the ceiling, yet Omarchy's audio panel
# lists it as an app ("Laptop Speakers") with a slider up to 150 % and a mute switch.
PA_VOLUME_NORM: Final = 65536
# "off" keeps EasyEffects loaded but globally bypassed on the internal speaker,
# so the correction can be compared against the untouched drivers and restored
# instantly; the controller must honour it instead of re-enabling the chain.
OFF_PROFILE: Final = "off"
DEFAULT_CUTOFF_HZ: Final = 90
ALLOWED_CUTOFFS_HZ: Final = frozenset((80, 90, 100, 110))
COMMAND_TIMEOUT_SECONDS: Final = 10.0
ROUTE_DISCOVERY_ATTEMPTS: Final = 6
ROUTE_DISCOVERY_DELAY_SECONDS: Final = 0.5
ROUTE_RECOVERY_RESTART_ATTEMPTS: Final = 2
ROUTE_STABILITY_ATTEMPTS: Final = 3
PRESET_RELOAD_ATTEMPTS: Final = 20
PRESET_RELOAD_DELAY_SECONDS: Final = 0.05
# How long a newly started EasyEffects may show a preset other than the saved choice: its
# unit loads the base Warm preset first, and the saved choice follows from its start-up hook
# or from the switch that started it, well within 10 s (the unit's own start timeout).
EASY_EFFECTS_START_SECONDS: Final = 10.0
BYPASS_ENABLED_RESPONSE: Final = "1"
BYPASS_DISABLED_RESPONSE: Final = "2"
COMMON_PRESET_BASELINE: Final = (
    PropertyChange("filter", "bypass", False),
    PropertyChange("limiter", "bypass", False),
    PropertyChange("limiter", "threshold", -1.0),
)
# Loudness for the internal speakers comes from INSIDE the chain (AGENTS.md): the controller
# drives the true-peak limiter with this input gain and the limiter's -1 dB ceiling holds the
# result, so every app is loud at 100 % and nothing in front of the chain has to go above it
# (cap_speaker_gain keeps it that way). 7 dB is what he reached for by hand: his sound-panel
# sliders stood at 130 % and 140 % (+6.8 and +8.8 dB) for the browser on 2026-11-17/18, and the
# ceiling alone, without this, left the speakers "very weak" (2026-11-19).
# It is the controller's, not the presets': a preset load resets the limiter to the preset's
# own 0 dB and the next apply puts the drive back. Warm · Rich is left out: its law lowers the
# ceiling to stay under the amplifiers' knee, which a drive would only fight.
LOUDNESS_DRIVE_DB: Final = 7.0
FIR_PRESET_KERNELS: Final = {
    "XPS17-FIR-Balanced": "xps17-balanced",
    "XPS17-FIR-Warm": "xps17-warm",
    "XPS17-FIR-Voice": "xps17-voice",
    "XPS17-FIR-Flat": "xps17-flat",
    "XPS17-FIR-Warm-Clear": "xps17-warm-clear",
    "XPS17-FIR-Warm-Classic": "xps17-warm",
    "XPS17-FIR-Warm-Rich": "xps17-warm-rich",
}
RICH_OUTPUT_GAIN_DB: Final = 2.0
FIR_PRESET_OUTPUT_GAINS: Final = {
    "XPS17-FIR-Balanced": 1.8,
    "XPS17-FIR-Warm": 1.3,
    "XPS17-FIR-Voice": 0.0,
    "XPS17-FIR-Flat": 0.1,
    "XPS17-FIR-Warm-Clear": 1.1,
    "XPS17-FIR-Warm-Classic": 2.3,
    "XPS17-FIR-Warm-Rich": RICH_OUTPUT_GAIN_DB,
}
# High-pass corner each FIR preset ships with. Warm and Warm · Clear replace the
# eight-pole RLC filter (real -3 dB near 330 Hz) with an 8th-order Butterworth
# whose -3 dB point really is at 180 Hz; their output gains are trimmed (Warm
# 2.3 to 1.3 dB, Clear 2.1 to 1.1 dB) so speech and music stay within about
# 0.7 dB of the loudness they had. Warm · Classic keeps the old filter and gain.
FIR_PRESET_HIGH_PASS_HZ: Final = {
    "XPS17-FIR-Warm": 180.0,
    "XPS17-FIR-Warm-Clear": 180.0,
    "XPS17-FIR-Warm-Rich": 180.0,
}
# Presets whose loudness contour and limiter ceiling the controller sets from the
# volume (model.volume_tracking_changes). Their limiter threshold is therefore not
# part of the fixed baseline, or every volume change would look like a broken
# preset and be "repaired" by a reload.
VOLUME_TRACKING_PRESETS: Final = frozenset(("XPS17-FIR-Warm-Rich",))
FIXED_PRESET_BASELINE: Final = tuple(
    change for change in COMMON_PRESET_BASELINE if change.name != "threshold"
)
PRESET_BASELINES: Final = {
    PRESET_NAME: (
        *COMMON_PRESET_BASELINE,
        PropertyChange("equalizer", "numBands", 10),
    ),
    **{
        preset: (
            *(FIXED_PRESET_BASELINE if preset in VOLUME_TRACKING_PRESETS else COMMON_PRESET_BASELINE),
            *(
                (PropertyChange("loudness", "bypass", False),)
                if preset in VOLUME_TRACKING_PRESETS
                else ()
            ),
            PropertyChange("filter", "frequency", FIR_PRESET_HIGH_PASS_HZ.get(preset, 100.0)),
            PropertyChange("convolver", "bypass", False),
            PropertyChange("convolver", "inputGain", 0.0),
            PropertyChange(
                "convolver", "outputGain", FIR_PRESET_OUTPUT_GAINS[preset]
            ),
            *(
                (PropertyChange("limiter", "inputGain", 0.0),)
                if preset in VOLUME_TRACKING_PRESETS
                else ()
            ),
        )
        for preset in FIR_PRESET_KERNELS
    },
    **{
        preset: (
            *COMMON_PRESET_BASELINE,
            PropertyChange("filter", "frequency", 150.0),
            PropertyChange("equalizer", "numBands", 12),
        )
        for preset in (
            "XPS17-Tuned-Balanced",
            "XPS17-Tuned-Warm",
            "XPS17-Tuned-Voice",
            "XPS17-Tuned-Flat",
        )
    },
}


@dataclass(slots=True)  # noqa: MUTABLE_OK - exception traceback state is mutable
class AudioRuntimeError(Exception):
    detail: str

    def __str__(self) -> str:
        return self.detail


class PresetPending(AudioRuntimeError):
    """EasyEffects started moments ago and the saved choice is not loaded yet: look again
    shortly instead of refusing its start-up preset."""


@dataclass(frozen=True, slots=True)
class ApplyResult:
    volume: int
    zone: Zone
    default_sink: str
    preset: str
    cutoff_hz: int
    changes: tuple[PropertyChange, ...]
    ready: bool
    speaker_active: bool


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    volume: int
    zone: Zone
    default_sink: str
    preset: str
    selected_cutoff_hz: int
    live_cutoff_hz: float
    socket_ready: bool
    speaker_active: bool


class ServerClient(Protocol):
    def request(self, command: str) -> str: ...


def socket_path() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return Path(runtime_dir) / "EasyEffectsServer"


def cutoff_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))
    return Path(state_home) / "omarchy-audio/high-pass-hz"


def speaker_profile_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))
    return Path(state_home) / "omarchy/speaker-profile"


def fir_kernel_path(kernel: str) -> Path:
    return Path.home() / ".local/share/easyeffects/irs" / f"{kernel}.irs"


def saved_profile() -> str | None:
    try:
        return speaker_profile_path().read_text().strip()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise AudioRuntimeError(detail=f"cannot read saved speaker profile: {error}") from error


def selected_engine() -> str:
    return FILTER_CHAIN_ENGINE if saved_profile() in ALL_FILTER_CHAIN_PROFILES else EASY_EFFECTS_ENGINE


def profile_engine_node(profile: str | None) -> str:
    """Configured PipeWire filter-chain sink node for a saved speaker profile."""
    if profile in STUDIO_PROFILES:
        return STUDIO_PROFILES[profile]
    return ENGINE_NODE


def resolve_active_engine_node(active_sink: str, profile: str | None) -> str:
    """Return whichever managed filter-chain node is currently active.

    When the live default sink is one of the 4 Studio DSP sinks or the Warm · Deep
    sink, track that live node directly so switching sinks in PipeWire/pavucontrol
    keeps ISO 226 loudness contour live; otherwise use the saved profile's sink.
    """
    if active_sink in ALL_ENGINE_NODES:
        return active_sink
    return profile_engine_node(profile)


def selected_preset() -> str:
    path = speaker_profile_path()
    try:
        profile = path.read_text().strip()
    except FileNotFoundError:
        return DEFAULT_PRESET_NAME
    except OSError as error:
        raise AudioRuntimeError(detail=f"cannot read saved speaker profile: {error}") from error
    try:
        return SPEAKER_PROFILE_PRESETS[profile]
    except KeyError as error:
        raise AudioRuntimeError(detail=f"unsupported saved speaker profile: {profile!r}") from error


def tracks_volume() -> bool:
    return selected_engine() == FILTER_CHAIN_ENGINE or (
        not correction_disabled() and selected_preset() in VOLUME_TRACKING_PRESETS
    )


def live_values(
    client: ServerClient, changes: tuple[PropertyChange, ...]
) -> dict[tuple[str, str], float | None]:
    values: dict[tuple[str, str], float | None] = {}
    for change in changes:
        try:
            values[(change.plugin, change.name)] = float(client.request(get_command(change)).strip())
        except ValueError:
            values[(change.plugin, change.name)] = None
    return values


def loudness_drive_changes(client: ServerClient) -> tuple[PropertyChange, ...]:
    """The limiter drive (LOUDNESS_DRIVE_DB) as a change, or nothing when the limiter already
    has it, so an ordinary volume event writes nothing to EasyEffects."""
    drive = PropertyChange("limiter", "inputGain", LOUDNESS_DRIVE_DB)
    if property_response_matches(drive, client.request(get_command(drive))):
        return ()
    return (drive,)


def correction_disabled() -> bool:
    try:
        return speaker_profile_path().read_text().strip() == OFF_PROFILE
    except FileNotFoundError:
        return False
    except OSError as error:
        raise AudioRuntimeError(detail=f"cannot read saved speaker profile: {error}") from error


def lock_path() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return Path(runtime_dir) / "omarchy-speaker-dsp.lock"


@contextmanager
def audio_lock() -> Iterator[None]:
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(lock_path(), flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def run_text(command: tuple[str, ...]) -> str:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise AudioRuntimeError(detail=f"{' '.join(command)} timed out") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise AudioRuntimeError(detail=f"{' '.join(command)} failed: {detail}")
    return completed.stdout.strip()


def sink_inventory() -> tuple[dict[str, object], ...]:
    response = run_text(("/usr/bin/pactl", "--format=json", "list", "sinks"))
    try:
        parsed = json.loads(response)
    except json.JSONDecodeError as error:
        raise AudioRuntimeError(detail="pactl returned invalid sink JSON") from error
    if not isinstance(parsed, list):
        raise AudioRuntimeError(detail="pactl sink inventory was not a list")
    return tuple(item for item in parsed if isinstance(item, dict))


def _sink_name(item: dict[str, object]) -> str:
    name = item.get("name")
    return name if isinstance(name, str) else ""


def _sink_properties(item: dict[str, object]) -> dict[str, object]:
    properties = item.get("properties")
    return properties if isinstance(properties, dict) else {}


def _speaker_sinks(inventory: tuple[dict[str, object], ...]) -> tuple[str, ...]:
    return tuple(
        _sink_name(item)
        for item in inventory
        if _sink_properties(item).get("alsa.card_name") == INTERNAL_AUDIO_CARD
        and _sink_properties(item).get("device.profile.name") == SPEAKER_PROFILE_NAME
        and _sink_name(item)
    )


def physical_sink() -> str:
    matches = _speaker_sinks(sink_inventory())
    if len(matches) != 1:
        raise AudioRuntimeError(
            detail=f"expected one internal HiFi speaker sink, found {len(matches)}"
        )
    return matches[0]


def internal_fallback_sink() -> str | None:
    matches = tuple(
        _sink_name(item)
        for item in sink_inventory()
        if _sink_properties(item).get("alsa.card_name") == INTERNAL_AUDIO_CARD
        and _sink_name(item).endswith(FALLBACK_SINK_SUFFIX)
    )
    if len(matches) > 1:
        raise AudioRuntimeError(detail="multiple internal fallback sinks were found")
    return matches[0] if matches else None


def _wait_for_physical_sink() -> str | None:
    for attempt in range(ROUTE_DISCOVERY_ATTEMPTS):
        try:
            return physical_sink()
        except AudioRuntimeError:
            if attempt + 1 < ROUTE_DISCOVERY_ATTEMPTS:
                time.sleep(ROUTE_DISCOVERY_DELAY_SECONDS)
    return None


def _sink_exists(name: str) -> bool:
    return any(_sink_name(item) == name for item in sink_inventory())


def recover_physical_route() -> str:
    try:
        original_default = default_sink()
    except AudioRuntimeError:
        original_default = ""

    ready = _wait_for_physical_sink()
    if ready is not None:
        return ready

    fallback = internal_fallback_sink()
    if fallback is None:
        raise AudioRuntimeError(
            detail="internal HiFi speaker is absent without a fallback route; "
            "refusing an unbounded WirePlumber restart"
        )

    for _ in range(ROUTE_RECOVERY_RESTART_ATTEMPTS):
        run_text(("/usr/bin/systemctl", "--user", "restart", "wireplumber.service"))
        ready = _wait_for_physical_sink()
        if ready is None:
            continue

        if (
            original_default
            and original_default != fallback
            and original_default != ready
            and _sink_exists(original_default)
        ):
            run_text(("/usr/bin/pactl", "set-default-sink", original_default))
        return ready

    raise AudioRuntimeError(
        detail="internal HiFi speaker did not recover after bounded WirePlumber restarts"
    )


def physical_volume() -> int:
    response = run_text(("/usr/bin/pactl", "get-sink-volume", physical_sink()))
    return parse_volume(response)


def physical_channel_volumes() -> tuple[int, ...]:
    response = run_text(("/usr/bin/pactl", "get-sink-volume", physical_sink()))
    volumes = parse_channel_volumes(response)
    if not volumes:
        raise AudioRuntimeError(detail="physical speaker channel volumes could not be parsed")
    return volumes


def default_sink() -> str:
    return run_text(("/usr/bin/pactl", "get-default-sink"))


def repair_easy_effects_default(
    active_sink: str,
    speaker_sink: str,
    dry_run: bool,
) -> str:
    """Replace only an invalid speaker-engine virtual default with the speaker.

    EasyEffects already captures applications through processAllOutputs. Making
    its virtual sink the WirePlumber default causes the controller to treat the
    internal speaker as inactive and globally bypass the measured chain. The
    Warm · Deep engine sink is just as wrong here: on an EasyEffects profile it is
    idle or on its way out, and EasyEffects' own output may follow the default
    into it (L12). A real external default is intentional and must never be
    changed here.
    """
    if active_sink not in (EASY_EFFECTS_SINK, *ALL_ENGINE_NODES) or dry_run:
        return active_sink

    run_text(("/usr/bin/pactl", "set-default-sink", speaker_sink))
    observed = default_sink()
    if observed != speaker_sink:
        raise AudioRuntimeError(
            detail=(
                "EasyEffects virtual default did not recover to the physical speaker: "
                f"{observed}"
            )
        )
    return observed


def selected_cutoff() -> int:
    path = cutoff_path()
    if not path.exists():
        return DEFAULT_CUTOFF_HZ
    try:
        candidate = int(path.read_text().strip())
    except ValueError:
        return DEFAULT_CUTOFF_HZ
    return candidate if candidate in ALLOWED_CUTOFFS_HZ else DEFAULT_CUTOFF_HZ


def socket_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino


def apply_changes(
    server: ServerClient,
    changes: tuple[PropertyChange, ...],
    dry_run: bool,
) -> None:
    if dry_run:
        return
    for change in changes:
        server.request(set_command(change))
        response = server.request(get_command(change))
        if not property_response_matches(change, response):
            raise AudioRuntimeError(
                detail=f"EasyEffects rejected {change.plugin}.{change.name}: {response}"
            )


def preset_baseline_matches(client: ServerClient, preset: str = PRESET_NAME) -> bool:
    baseline = PRESET_BASELINES.get(preset)
    if baseline is None:
        return False
    if not all(
        property_response_matches(change, client.request(get_command(change)))
        for change in baseline
    ):
        return False

    kernel = FIR_PRESET_KERNELS.get(preset)
    if kernel is None:
        return True
    return (
        fir_kernel_path(kernel).is_file()
        and client.request("get_property:output:convolver:0:kernelName") == kernel
    )


def restore_preset_baseline(
    client: ServerClient,
    dry_run: bool,
    preset: str = PRESET_NAME,
) -> None:
    if dry_run or preset_baseline_matches(client, preset):
        return
    run_text(("/usr/bin/easyeffects", "--load-preset", preset))
    for _ in range(PRESET_RELOAD_ATTEMPTS):
        if (
            client.request("get_last_loaded_preset:output") == preset
            and preset_baseline_matches(client, preset)
        ):
            return
        time.sleep(PRESET_RELOAD_DELAY_SECONDS)
    raise AudioRuntimeError(detail=f"preset baseline did not recover: {preset}")


def set_global_bypass(client: ServerClient, speaker_active: bool, dry_run: bool) -> None:
    if dry_run:
        return
    enabled = not speaker_active
    client.request(f"global_bypass:{1 if enabled else 0}")
    observed = client.request("get_global_bypass")
    expected = BYPASS_ENABLED_RESPONSE if enabled else BYPASS_DISABLED_RESPONSE
    if observed != expected:
        state = "enabled" if enabled else "disabled"
        raise AudioRuntimeError(
            detail=f"EasyEffects global bypass was not {state}: {observed}"
        )


def easy_effects_starting() -> bool:
    """EasyEffects bound its socket less than EASY_EFFECTS_START_SECONDS ago. A socket file's
    timestamp is the moment it was bound; connecting and talking never change it."""
    try:
        age = time.time() - socket_path().stat().st_mtime
    except FileNotFoundError:
        return False
    return 0 <= age < EASY_EFFECTS_START_SECONDS


def stable_route_and_preset(
    client: ServerClient,
    dry_run: bool,
) -> tuple[str, str]:
    speaker_sink = physical_sink()
    for _ in range(ROUTE_STABILITY_ATTEMPTS):
        active_sink = default_sink()
        active_sink = repair_easy_effects_default(active_sink, speaker_sink, dry_run)
        speaker_active = active_sink == speaker_sink
        if not speaker_active or correction_disabled():
            set_global_bypass(client, False, dry_run)
        if correction_disabled():
            if default_sink() != active_sink:
                continue
            return active_sink, client.request("get_last_loaded_preset:output")
        try:
            expected_preset = selected_preset()
        except AudioRuntimeError:
            if not dry_run:
                set_global_bypass(client, False, dry_run)
            raise
        preset = client.request("get_last_loaded_preset:output")
        if default_sink() != active_sink:
            continue
        if preset != expected_preset and not dry_run:
            if easy_effects_starting():
                # Its start-up preset, with the saved choice on its way: refusing it would
                # turn the correction off for nothing.
                raise PresetPending(
                    detail=f"EasyEffects is still starting: {preset}; expected {expected_preset}"
                )
            set_global_bypass(client, False, dry_run)
            raise AudioRuntimeError(
                detail=f"refusing unexpected preset: {preset}; expected {expected_preset}"
            )

        if preset == expected_preset and not preset_baseline_matches(client, preset):
            if not dry_run:
                set_global_bypass(client, False, dry_run)
                restore_preset_baseline(client, dry_run, preset)

        if speaker_active:
            set_global_bypass(client, speaker_active, dry_run)
            if default_sink() != active_sink:
                continue
        return active_sink, preset

    final_sink = default_sink()
    if final_sink != speaker_sink:
        set_global_bypass(client, False, dry_run)
    raise AudioRuntimeError(detail="default audio output changed repeatedly during DSP apply")


def engine_node(node_name: str = ENGINE_NODE) -> tuple[int, str, dict[str, float]] | None:
    """(global id, object serial, current controls) of a filter-chain engine node, or None."""
    response = run_text(("/usr/bin/pw-dump", node_name))
    try:
        objects = json.loads(response or "[]")
    except json.JSONDecodeError as error:
        raise AudioRuntimeError(detail="pw-dump returned invalid JSON") from error
    for item in objects if isinstance(objects, list) else ():
        info = item.get("info") if isinstance(item, dict) else None
        if not isinstance(info, dict) or info.get("props", {}).get("node.name") != node_name:
            continue
        controls: dict[str, float] = {}
        for props in info.get("params", {}).get("Props", ()):
            params = props.get("params") if isinstance(props, dict) else None
            if isinstance(params, list):
                controls.update(
                    (key, float(value))
                    for key, value in zip(params[::2], params[1::2])
                    if isinstance(key, str) and isinstance(value, (int, float))
                )
        return int(item["id"]), str(info["props"].get("object.serial", item["id"])), controls
    return None


def sink_inputs() -> tuple[dict[str, object], ...]:
    response = run_text(("/usr/bin/pactl", "--format=json", "list", "sink-inputs"))
    try:
        parsed = json.loads(response or "[]")
    except json.JSONDecodeError as error:
        raise AudioRuntimeError(detail="pactl returned invalid sink-input JSON") from error
    return tuple(item for item in parsed if isinstance(item, dict)) if isinstance(parsed, list) else ()


def _is_application_stream(item: dict[str, object]) -> bool:
    """Real playback from an application, the streams Omarchy's own output switcher moves:
    it names its application, which is not EasyEffects, and it is not a filter's own
    output (those carry a link-group, the engine's is also pinned with node.dont-move)."""
    properties = _sink_properties(item)
    application = properties.get("application.name")
    node_name = str(properties.get("node.name", ""))
    return (
        isinstance(application, str)
        and application != "EasyEffects"
        and "node.link-group" not in properties
        and str(properties.get("node.dont-move", "false")).lower() != "true"
        and node_name not in ALL_ENGINE_NODES
        and node_name not in ALL_ENGINE_OUTPUT_NODES
        and node_name not in LEGACY_SPATIAL_NODES
        and node_name not in LEGACY_SPATIAL_OUTPUT_NODES
        and not node_name.startswith(ENGINE_NODE)
    )


def configured_default_sink() -> str | None:
    """The output he chose (WirePlumber's default.configured.audio.sink). It differs from
    the effective default while the chosen sink is gone, as during an engine restart."""
    response = run_text(("/usr/bin/pw-metadata", "-n", "default", "0", "default.configured.audio.sink"))
    match = re.search(r"value:'(.*?)' type:", response)
    try:
        name = json.loads(match.group(1)).get("name") if match else None
    except (json.JSONDecodeError, AttributeError):
        return None
    return name if isinstance(name, str) else None


def _hold_engine_level(kind: str, target: str, item: dict[str, object]) -> None:
    """One side of the engine at 100 % and unmuted: its sink or its output stream (pactl's
    "sink" or "sink-input"), written only when it differs."""
    volume = item.get("volume")
    levels = [channel.get("value") for channel in volume.values() if isinstance(channel, dict)] if isinstance(volume, dict) else []
    if any(level != PA_VOLUME_NORM for level in levels):
        run_text(("/usr/bin/pactl", f"set-{kind}-volume", target, "100%"))
    if item.get("mute") is True:
        run_text(("/usr/bin/pactl", f"set-{kind}-mute", target, "0"))


def _channel_levels(item: dict[str, object]) -> tuple[int, ...]:
    """A sink's or stream's raw channel levels as pactl lists them (65536 = 100 %), or ()."""
    volume = item.get("volume")
    levels = tuple(channel.get("value") for channel in volume.values() if isinstance(channel, dict)) if isinstance(volume, dict) else ()
    return levels if levels and all(isinstance(level, int) for level in levels) else ()


def _speaker_chain(inventory: tuple[dict[str, object], ...], active_sink: str) -> dict[object, str]:
    """pactl index -> name of every sink whose sound ends in the internal speakers right now:
    the speaker itself (and its UCM stereo fallback); the Warm · Deep and Studio filter-chain
    engines, whose outputs are pinned to the speaker by name; EasyEffects' sink only while its
    output follows a default that is the speaker (with another output chosen it plays there,
    globally bypassed)."""
    names = {
        *_speaker_sinks(inventory),
        *(_sink_name(item) for item in inventory
          if _sink_properties(item).get("alsa.card_name") == INTERNAL_AUDIO_CARD
          and _sink_name(item).endswith(FALLBACK_SINK_SUFFIX)),
        *ALL_ENGINE_NODES,
    }
    if active_sink in names | {EASY_EFFECTS_SINK}:
        names.add(EASY_EFFECTS_SINK)
    return {item.get("index"): _sink_name(item) for item in inventory if _sink_name(item) in names}


def cap_speaker_gain() -> tuple[str, ...]:
    """The internal speakers' gain rule (AGENTS.md): nothing in front of their DSP chain plays
    above 100 %. Loudness comes from the drive the controller puts on the limiter inside the
    chain (LOUDNESS_DRIVE_DB); a stream or sink above 100 % on top of that pushes the chain
    twice into its -1 dB ceiling, which is what tore the speakers on 2026-11-17 (Chromium at
    140 % into Warm · Deep).

    A ceiling, never a hold: a level above 100 % comes down to exactly 100 %, channel by
    channel (so a balance he set survives); nothing at or below 100 % is touched, nothing is
    raised, no mute is changed, and nothing playing to Bluetooth, HDMI, the headphone jack or
    any other output is looked at, since above 100 % is his to use there. The engine's own
    output stream is left to hold_engine_route, which holds it at exactly 100 %. Returns one
    log line per level it brought down."""
    with audio_lock():
        inventory = sink_inventory()
        chain = _speaker_chain(inventory, default_sink())
        targets = [("sink", _sink_name(item), item, _sink_name(item)) for item in inventory if item.get("index") in chain]
        for item in sink_inputs():
            properties = _sink_properties(item)
            if item.get("sink") not in chain or properties.get("node.name") in ALL_ENGINE_OUTPUT_NODES:
                continue
            # Never media.name: for most players that is the song or page title, not for the journal.
            label = properties.get("application.name") or properties.get("node.name") or "a stream"
            targets.append(("sink-input", str(item.get("index")), item,
                            f"{label} (stream #{item.get('index')}) on {chain[item.get('sink')]}"))
        capped: list[str] = []
        for kind, target, item, label in targets:
            levels = _channel_levels(item)
            if not levels or max(levels) <= PA_VOLUME_NORM:
                continue
            ceiling = tuple(str(min(level, PA_VOLUME_NORM)) for level in levels)
            try:
                run_text(("/usr/bin/pactl", f"set-{kind}-volume", target, *ceiling))
            except AudioRuntimeError:
                continue  # it went away in between; whatever replaces it raises its own event
            capped.append(f"speaker-gain: {label} {round(max(levels) * 100 / PA_VOLUME_NORM)}% -> 100%")
        return tuple(capped)


def _take_default(target: str, inventory: tuple[dict[str, object], ...]) -> None:
    """Make `target` the default output without demoting the output he chose.

    WirePlumber keeps a stack of chosen outputs and plays to the highest one present, and
    `pactl set-default-sink` puts its target on top. When his choice is another output that
    is away for now (Bluetooth headphones switched off, the TV unplugged), it goes back on
    top, so `target` sits right below it: WirePlumber plays to `target` now and returns to
    that output the moment it is back, exactly as it does without the engine."""
    chosen = configured_default_sink()
    run_text(("/usr/bin/pactl", "set-default-sink", target))
    if chosen and chosen not in {*ALL_ENGINE_NODES, EASY_EFFECTS_SINK, *_speaker_sinks(inventory)}:
        run_text(("/usr/bin/pw-metadata", "-n", "default", "0", "default.configured.audio.sink",
                  json.dumps({"name": chosen}), "Spa:String:JSON"))


def hold_engine_route(dry_run: bool) -> str:
    """Warm · Deep presents the engine the way Omarchy presents its speaker tunings: while
    the laptop speakers are the chosen output, the engine sink is the default, so
    WirePlumber links every new stream into it from its first sample. Returns the default.

    This is the standing rule; the watcher applies it on every audio event, so it also
    performs the switch from EasyEffects (the instant EasyEffects' sink is gone) and
    repairs whatever another tool changed:
    - the physical speaker made the default (any chooser, any tool) means the speakers
      were chosen, so the engine becomes the default and streams left on the bare speaker
      follow it;
    - another output chosen: streams still in the engine are released to follow it;
    - the engine's own level, on its sink and on its output stream, is held at 100 % and
      unmuted.
    Nothing happens while EasyEffects' sink exists (it owns the streams until it is gone,
    and its output may follow the default, L12) or while the engine is between a crash
    and its restart: WirePlumber keeps the engine as the configured default and returns
    the default and every stream to it as soon as the node is back.
    """
    inventory = sink_inventory()
    by_name = {_sink_name(item): item for item in inventory}
    speakers = _speaker_sinks(inventory)
    active_sink = default_sink()
    node_name = resolve_active_engine_node(active_sink, saved_profile())
    engine = by_name.get(node_name)
    if EASY_EFFECTS_SINK in by_name or engine is None or len(speakers) != 1:
        return active_sink
    speaker_sink = speakers[0]
    if active_sink == speaker_sink and not dry_run:
        _take_default(node_name, inventory)
        active_sink = default_sink()
    if dry_run:
        return active_sink
    _hold_engine_level("sink", node_name, engine)
    # Streams that do not follow the default on their own: on the bare speaker or another
    # filter-chain sink while the active engine is the default (e.g. an app WirePlumber
    # remembered pinned to the speaker, or left behind after switching between Warm · Deep
    # and Studio profiles), or still in any engine sink after another output was chosen.
    # Moving them to the default makes them follow it from now on.
    engine_indices = {
        item.get("index")
        for name, item in by_name.items()
        if name in (ALL_ENGINE_NODES | LEGACY_SPATIAL_NODES)
    }
    if active_sink == node_name:
        stray_indices = {by_name[speaker_sink].get("index")} | (engine_indices - {engine.get("index")})
    else:
        stray_indices = engine_indices
    for item in sink_inputs():
        if _sink_properties(item).get("node.name") in ALL_ENGINE_OUTPUT_NODES:
            _hold_engine_level("sink-input", str(item.get("index")), item)
        elif _is_application_stream(item) and item.get("sink") in stray_indices:
            run_text(("/usr/bin/pactl", "move-sink-input", str(item.get("index")), FOLLOW_DEFAULT_SINK))
    return active_sink


def physical_sink_or_none() -> str | None:
    try:
        return physical_sink()
    except AudioRuntimeError:
        return None


def route_engine_now() -> str | None:
    """Audio-event fast path for the watcher: the routing rule only, no volume law."""
    with audio_lock():
        if selected_engine() != FILTER_CHAIN_ENGINE:
            return None
        return hold_engine_route(False)


def save_profile(profile: str) -> None:
    path = speaker_profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(profile)


def hand_back_to_easy_effects(profile: str) -> int:
    """Leaving Warm · Deep for an EasyEffects profile, before EasyEffects starts.

    Under the routing lock, in this order: every application stream in the engine is
    pinned to it (by target metadata: pactl would only make a stream follow the default
    when the engine is the default), the new choice is saved, and the physical speaker
    becomes the default if the speakers were the chosen output. The pinned streams stay
    in the engine until EasyEffects captures them (L5), so none plays on the bare speaker,
    and EasyEffects never starts with the engine as the default (L12). Returns how many
    streams were pinned.
    """
    if profile not in SPEAKER_PROFILE_PRESETS and profile != OFF_PROFILE:
        raise AudioRuntimeError(detail=f"not an EasyEffects speaker profile: {profile!r}")
    with audio_lock():
        inventory = sink_inventory()
        engine_sinks = {
            item.get("index"): item
            for item in inventory
            if _sink_name(item) in ALL_ENGINE_NODES
        }
        pinned = 0
        if engine_sinks:
            for item in sink_inputs():
                engine_item = engine_sinks.get(item.get("sink"))
                if engine_item is None:
                    continue
                serial = _sink_properties(engine_item).get("object.serial")
                node_id = _sink_properties(item).get("object.id")
                if _is_application_stream(item) and node_id and serial:
                    run_text(("/usr/bin/pw-metadata", "-n", "default", str(node_id), "target.object", str(serial), "Spa:Id"))
                    pinned += 1
        save_profile(profile)
        speakers = _speaker_sinks(inventory)
        if len(speakers) == 1 and (
            default_sink() in ALL_ENGINE_NODES
            or configured_default_sink() in ALL_ENGINE_NODES
        ):
            _take_default(speakers[0], inventory)
        return pinned


def active_engine_node_name() -> str:
    """Resolve the active filter-chain sink node from live default sink or saved profile."""
    profile = saved_profile()
    try:
        active = default_sink()
    except AudioRuntimeError:
        active = ""
    return resolve_active_engine_node(active, profile)


def engine_identity() -> str | None:
    """Changes whenever the active speaker engine node is (re)created or switched,
    so its controls are re-sent."""
    if selected_engine() != FILTER_CHAIN_ENGINE:
        return None
    node_name = active_engine_node_name()
    node = engine_node(node_name)
    return None if node is None else f"{node_name}:{node[1]}"


def apply_engine_changes(node_id: int, changes: tuple[PropertyChange, ...], dry_run: bool) -> None:
    if dry_run or not changes:
        return
    run_text(("/usr/bin/pw-cli", "set-param", str(node_id), "Props", engine_props(changes)))


def apply_engine_profile(current: Zone | None, dry_run: bool) -> ApplyResult:
    """Filter-chain engine profiles (Warm · Deep and Studio Deep/Spatial/Reference/Voice):
    hold the routing rule, then push live volume tracking to the active filter-chain node.

    - Warm · Deep (`omarchy_speaker_engine`): sends contour, limiter ceiling, and Deep-bass
      amount in two gain-safe batches (every lowering change, then the rest).
    - Studio DSP (`omarchy_speaker_studio*`): tracks live PipeWire volume continuously into
      `studio:Volume (dB)` across 0..100 % (clamped to [-45.0, 0.0] dB) so ISO 226:2023
      loudness and Klippel excursion scaling track every step while keeping mastering
      loudness drive (`studio:Drive (dB)`) untouched.
    """
    profile = saved_profile() or ""
    # Routing first: the engine always holds safe defaults (after a restart, Warm · Deep's
    # cautious 100 % ceiling or Studio's -12.0 dB volume), so taking the default never waits
    # on the law below.
    active_sink = hold_engine_route(dry_run)
    node_name = resolve_active_engine_node(active_sink, profile)
    is_studio = node_name in STUDIO_NODES
    speaker_sink = physical_sink_or_none()
    node = engine_node(node_name)
    preset_label = (
        f"studio:{STUDIO_NODE_TO_PROFILE[node_name]}"
        if is_studio
        else f"engine:{profile}"
    )
    if speaker_sink is None or node is None:
        # Speaker missing (UCM fallback) or engine not up yet: nothing to tune.
        return ApplyResult(volume=0, zone=zone_for_volume(0, current), default_sink=active_sink,
                           preset=preset_label, cutoff_hz=selected_cutoff(), changes=(),
                           ready=False, speaker_active=False)
    volume = physical_volume()
    zone = zone_for_volume(volume, current)
    node_id, _, controls = node
    if is_studio:
        law = studio_tracking_changes(volume)
    else:
        deep_bass = ENGINE_PROFILES.get(profile, ENGINE_PROFILES["warm-deep"])
        law = engine_tracking_changes(volume, deep_bass)
    observed = {(change.plugin, change.name): controls.get(f"{change.plugin}:{change.name}") for change in law}
    lowering, raising = gain_safe_batches(law, observed)
    apply_engine_changes(node_id, lowering, dry_run)
    apply_engine_changes(node_id, raising, dry_run)
    if not dry_run:
        after = engine_node(node_name)
        live = {} if after is None else after[2]
        for change in law:
            value = live.get(f"{change.plugin}:{change.name}")
            if value is None or not math.isclose(value, float(change.value), rel_tol=1e-5, abs_tol=1e-6):
                raise AudioRuntimeError(detail=f"speaker engine rejected {change.plugin}:{change.name}: {value}")
    return ApplyResult(volume=volume, zone=zone, default_sink=active_sink, preset=preset_label,
                       cutoff_hz=selected_cutoff(), changes=(*lowering, *raising), ready=True,
                       speaker_active=active_sink in (speaker_sink, *ALL_ENGINE_NODES))


def apply_profile(
    current: Zone | None,
    dry_run: bool,
    server: ServerClient | None = None,
) -> ApplyResult:
    with audio_lock():
        if selected_engine() == FILTER_CHAIN_ENGINE:
            return apply_engine_profile(current, dry_run)
        client = server or LocalServer(socket_path())
        speaker_sink = physical_sink()
        volume = physical_volume()
        zone = zone_for_volume(volume, current)
        cutoff = selected_cutoff()
        adaptive_changes = (
            PropertyChange("filter", "frequency", float(cutoff)),
            *changes_for_zone(zone),
        )

        for _ in range(ROUTE_STABILITY_ATTEMPTS):
            active_sink, preset = stable_route_and_preset(client, dry_run)
            speaker_active = active_sink == speaker_sink
            ready = preset in PRESET_BASELINES
            if preset == PRESET_NAME:
                changes = adaptive_changes
            elif preset in VOLUME_TRACKING_PRESETS:
                law = volume_tracking_changes(volume)
                changes = gain_safe_order(law, live_values(client, law))
            elif ready and speaker_active and not correction_disabled():
                changes = loudness_drive_changes(client)
            else:
                changes = ()
            if ready and speaker_active and changes and not correction_disabled():
                apply_changes(client, changes, dry_run)

            final_sink = default_sink()
            if final_sink != active_sink:
                if final_sink != speaker_sink:
                    set_global_bypass(client, False, dry_run)
                continue

            return ApplyResult(
                volume=volume,
                zone=zone,
                default_sink=active_sink,
                preset=preset,
                cutoff_hz=cutoff,
                changes=changes,
                ready=ready,
                speaker_active=speaker_active,
            )

        final_sink = default_sink()
        if final_sink != speaker_sink:
            set_global_bypass(client, False, dry_run)
        raise AudioRuntimeError(detail="default audio output changed during DSP property apply")


def applied_zone(client: ServerClient) -> Zone | None:
    response = client.request(
        "get_property:output:multiband_compressor:0:band0Ratio"
    )
    try:
        ratio = float(response)
    except ValueError:
        return None
    for expected, zone in ((1.4, Zone.LOW), (1.5, Zone.MID), (1.3, Zone.HIGH)):
        if math.isclose(ratio, expected, abs_tol=1e-6):
            return zone
    return None


def live_cutoff(client: ServerClient) -> float:
    response = client.request("get_property:output:filter:0:frequency")
    try:
        cutoff = float(response)
    except ValueError as error:
        raise AudioRuntimeError(detail=f"invalid live cutoff readback: {response}") from error
    if not math.isfinite(cutoff) or cutoff <= 0:
        raise AudioRuntimeError(detail=f"invalid live cutoff readback: {response}")
    return cutoff


def read_engine_status() -> RuntimeStatus:
    speaker_sink = physical_sink()
    volume = physical_volume()
    active_sink = default_sink()
    profile = saved_profile()
    node_name = resolve_active_engine_node(active_sink, profile)
    is_studio = node_name in STUDIO_NODES
    node = engine_node(node_name)
    hp_param = STUDIO_HIGH_PASS_PARAM if is_studio else ENGINE_HIGH_PASS_PARAM
    preset_label = (
        f"studio:{STUDIO_NODE_TO_PROFILE[node_name]}"
        if is_studio
        else f"engine:{profile}"
    )
    return RuntimeStatus(
        volume=volume,
        zone=zone_for_volume(volume, None),
        default_sink=active_sink,
        preset=preset_label,
        selected_cutoff_hz=selected_cutoff(),
        live_cutoff_hz=float("nan") if node is None else node[2].get(hp_param, float("nan")),
        socket_ready=node is not None,
        speaker_active=active_sink in (speaker_sink, *ALL_ENGINE_NODES),
    )


def read_status(server: ServerClient | None = None) -> RuntimeStatus:
    if selected_engine() == FILTER_CHAIN_ENGINE:
        return read_engine_status()
    path = socket_path()
    client = server or LocalServer(path)
    speaker_sink = physical_sink()
    volume = physical_volume()
    active_sink = default_sink()
    preset = client.request("get_last_loaded_preset:output")
    current_zone = applied_zone(client) if preset == PRESET_NAME else None
    return RuntimeStatus(
        volume=volume,
        zone=current_zone or zone_for_volume(volume, None),
        default_sink=active_sink,
        preset=preset,
        selected_cutoff_hz=selected_cutoff(),
        live_cutoff_hz=live_cutoff(client),
        socket_ready=socket_identity(path) is not None,
        speaker_active=active_sink == speaker_sink,
    )

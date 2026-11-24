#!/usr/bin/env python3
"""Thermal & IV-Sense Bridge from `omarchy-speaker-guard` to Kernel P4 Deadman Gate.

Zero external dependencies (Python 3 standard library only).

Connects `omarchy-speaker-guard`'s verified IV-sense / two-node voice-coil thermal &
excursion estimator to our patched `snd-soc-rt1316-sdw` kernel driver's Asahi-style
250 ms deadman watchdog ALSA controls:
  - Left amplifier  (SoundWire Link 1): `rt1316-1 Guard Drive Unlock`
  - Right amplifier (SoundWire Link 2): `rt1316-2 Guard Drive Unlock`
  - Unlock heartbeat magic:             `0xdec1be15` (3737239061)
  - Explicit instant lock value:        `0`
  - Kernel deadman watchdog timeout:    `250 ms` (`RT1316_XPS17_GUARD_TIMEOUT_MS`)
  - Bridge heartbeat cadence:           `50 ms` default (hard ceiling `<= 100 ms`)

Fail-closed invariants (ALL must hold simultaneously to emit `0xdec1be15`):
  1. Trusted calibration (`calibration_trusted == True`, no PLACEHOLDER/PROVISIONAL).
  2. Non-colliding 4-channel IV-sense slot tags across Link 1 & Link 2 amps
     (e.g., Amp1 `0xA0/0xB1`, Amp2 `0xE4/0xF5`; rejects Linux stock `0xA0/0xB1` collision).
  3. Valid, fresh IV-sense frame for BOTH amps (`age <= max_frame_age_ms < 250 ms`,
     `plausible == True`, `xrun_count == 0`, sample rate within `48000 Hz +/- 5%`).
  4. IPC4 DRC fence intact (`drc_switch_off == True`).
  5. Voice-coil temperature strictly below `T_unlock_ceiling` (default `70.0 C`), with
     `3.0 C` hysteresis (`<= 67.0 C` required to re-unlock), continuous healthy holdoff
     (`10.0 s`), and predicted steady-state check.
  6. Woofer excursion strictly below `0.90 * X_max` (re-enable at `<= 0.80 * X_max`).
  7. Playback stream active (`stream_active == True`); silence immediately locks (`0`).

Any invariant violation, SIGTERM/SIGINT, write error, or clean shutdown IMMEDIATELY
sends `0` (explicit lock) to BOTH amplifier controls. If the userspace process stalls
or crashes without executing shutdown, heartbeat kicks cease and the kernel 250 ms
deadman watchdog autonomously reverts `0xC60F=0x01`, `0xD002=0xBF`, `0xD004=0xBF`.
"""
from __future__ import annotations

import abc
import enum
import math
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

# ---- Kernel P4 Interface Constants (synced with rt1316-xps17-policy.h) -------
CARD_DEFAULT = "sofsoundwire"
CTL_LEFT = "rt1316-1 Guard Drive Unlock"
CTL_RIGHT = "rt1316-2 Guard Drive Unlock"
GUARD_MAGIC = 0xDEC1BE15           # RT1316_XPS17_GUARD_MAGIC (3737239061)
GUARD_LOCK_VAL = 0                 # Any value != GUARD_MAGIC force-locks immediately
KERNEL_TIMEOUT_MS = 250.0          # RT1316_XPS17_GUARD_TIMEOUT_MS
MAX_HEARTBEAT_MS = 100.0           # Hard upper bound (< 0.5x kernel deadman timeout)
DEFAULT_HEARTBEAT_MS = 50.0        # Matches speaker_guard period_ms = 50 ms

# Hardware IV-sense slot registers (0x3004, 0x3005)
IV_SLOT_AMP1_EXPECTED = (0xA0, 0xB1)
IV_SLOT_AMP2_EXPECTED = (0xE4, 0xF5)

# Hardware register targets mirrored in MockAlsaControlBackend
HV_GAIN_STOCK = 0x01
HV_GAIN_UNLOCKED = 0x05
DIG_GAIN_STOCK = 0xBF
DIG_GAIN_UNLOCKED = 0xA9

REQUIRED_DRIVERS = ("left_woofer", "left_tweeter", "right_woofer", "right_tweeter")
WOOFER_DRIVERS = ("left_woofer", "right_woofer")


class BridgeState(str, enum.Enum):
    LOCKED = "locked"
    UNLOCKED = "unlocked"
    LOCKOUT = "lockout"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True)
class IvSenseSlots:
    """SoundWire IV-sense tag register configuration for both ALC1316 amplifiers."""
    amp1_ch0: int = IV_SLOT_AMP1_EXPECTED[0]  # 0x3004 on Link 1 (0xA0)
    amp1_ch1: int = IV_SLOT_AMP1_EXPECTED[1]  # 0x3005 on Link 1 (0xB1)
    amp2_ch0: int = IV_SLOT_AMP2_EXPECTED[0]  # 0x3004 on Link 2 (0xE4)
    amp2_ch1: int = IV_SLOT_AMP2_EXPECTED[1]  # 0x3005 on Link 2 (0xF5)

    def non_colliding(self) -> tuple[bool, str]:
        """Verify all 4 SoundWire DP2 tag bytes and channel-slot nibbles are distinct."""
        vals = (self.amp1_ch0, self.amp1_ch1, self.amp2_ch0, self.amp2_ch1)
        if any(not (0 <= v <= 0xFF) for v in vals):
            return False, f"iv slot register out of byte range: {vals}"
        if len(set(vals)) != 4:
            return False, (
                f"iv-sense slot collision across amps: "
                f"amp1=(0x{self.amp1_ch0:02x},0x{self.amp1_ch1:02x}) "
                f"amp2=(0x{self.amp2_ch0:02x},0x{self.amp2_ch1:02x})"
            )
        # Each byte packs (upper nibble tag, lower nibble tag); check lower-nibble slot indices
        # don't collide across the 4-channel aggregated capture stream.
        low_slots = [v & 0x07 for v in vals]
        if len(set(low_slots)) != 4:
            return False, f"iv-sense lower slot-id collision: {low_slots}"
        return True, "4 distinct non-colliding IV-sense slots"


@dataclass(frozen=True)
class BridgeConfig:
    """Safety & timing parameters for the userspace P4 deadman bridge."""
    card: str = CARD_DEFAULT
    ctl_left: str = CTL_LEFT
    ctl_right: str = CTL_RIGHT
    heartbeat_interval_ms: float = DEFAULT_HEARTBEAT_MS
    max_frame_age_ms: float = 150.0
    t_unlock_ceiling_c: float = 70.0   # model/xps17-9720.ini: t_on = 100 - 10 - 20 = 70 C
    t_hysteresis_c: float = 3.0        # model/xps17-9720.ini: t_hysteresis_c = 3 C
    t_ambient_min_c: float = 5.0
    x_max_mm: float = 0.50             # XPS 17 micro-woofer physical Xmax ceiling
    excursion_trip_ratio: float = 0.90
    excursion_release_ratio: float = 0.80
    unlock_holdoff_s: float = 10.0     # model/xps17-9720.ini: unlock_holdoff_s = 10 s
    max_trips_per_window: int = 3      # model/xps17-9720.ini: max_locks = 3 per 10 min
    trip_window_s: float = 600.0
    lockout_duration_s: float = 600.0
    nominal_rate_hz: float = 48000.0
    rate_tolerance_frac: float = 0.05  # 45600..50400 Hz

    def __post_init__(self) -> None:
        if not (10.0 <= self.heartbeat_interval_ms <= MAX_HEARTBEAT_MS):
            raise ValueError(
                f"heartbeat_interval_ms must be in [10, {MAX_HEARTBEAT_MS}] ms to satisfy "
                f"{KERNEL_TIMEOUT_MS} ms kernel watchdog, got {self.heartbeat_interval_ms}"
            )
        if not (self.heartbeat_interval_ms <= self.max_frame_age_ms < KERNEL_TIMEOUT_MS):
            raise ValueError(
                f"max_frame_age_ms ({self.max_frame_age_ms}) must be >= heartbeat_interval_ms "
                f"({self.heartbeat_interval_ms}) and < kernel timeout ({KERNEL_TIMEOUT_MS})"
            )
        if self.t_hysteresis_c <= 0.0 or self.t_unlock_ceiling_c - self.t_hysteresis_c <= self.t_ambient_min_c:
            raise ValueError("Invalid thermal ceiling / hysteresis bounds")
        if not (0.0 < self.excursion_release_ratio < self.excursion_trip_ratio <= 1.0):
            raise ValueError("Require 0 < excursion_release_ratio < excursion_trip_ratio <= 1.0")
        if self.x_max_mm <= 0.0:
            raise ValueError("x_max_mm must be positive")
        if self.unlock_holdoff_s < 0.0:
            raise ValueError("unlock_holdoff_s must be non-negative")


@dataclass(frozen=True)
class GuardTelemetry:
    """Single-period safety telemetry snapshot from `omarchy-speaker-guard`."""
    timestamp_s: float
    calibration_trusted: bool
    iv_slots: IvSenseSlots
    plausible: bool
    xrun_count: int
    sample_rate_hz: float
    drc_switch_off: bool
    stream_active: bool
    t_coil_c: Mapping[str, float]
    excursion_peak_mm: Mapping[str, float]
    t_steady_elevated_c: Mapping[str, float] | None = None
    amp_present: tuple[bool, bool] = (True, True)
    guard_atten_db: Mapping[str, float] | None = None

    @classmethod
    def from_speaker_guard_output(
        cls,
        *,
        timestamp_s: float,
        guard_output,
        calibration_trusted: bool,
        iv_slots: IvSenseSlots,
        xrun_count: int,
        sample_rate_hz: float,
        drc_switch_off: bool,
        stream_active: bool,
        excursion_peak_mm: Mapping[str, float],
        t_steady_elevated_c: Mapping[str, float] | None = None,
    ) -> "GuardTelemetry":
        """Adapt a `speaker_guard.guard.Output` object directly into `GuardTelemetry`."""
        return cls(
            timestamp_s=timestamp_s,
            calibration_trusted=calibration_trusted,
            iv_slots=iv_slots,
            plausible=(guard_output.health == "healthy" and not guard_output.reasons),
            xrun_count=xrun_count,
            sample_rate_hz=sample_rate_hz,
            drc_switch_off=drc_switch_off,
            stream_active=stream_active,
            t_coil_c=dict(guard_output.temps),
            excursion_peak_mm=dict(excursion_peak_mm),
            t_steady_elevated_c=dict(t_steady_elevated_c) if t_steady_elevated_c is not None else None,
            guard_atten_db=dict(guard_output.atten_db),
        )


@dataclass(frozen=True)
class BridgeDecision:
    """Structured result emitted on every `evaluate_and_drive` call."""
    state: BridgeState
    unlocked: bool
    kicked: bool
    explicit_lock_sent: bool
    reasons: tuple[str, ...]
    max_coil_c: float | None
    max_excursion_ratio: float | None


# ---- Pluggable ALSA Control Backends -----------------------------------------
class AlsaControlBackend(abc.ABC):
    """Abstract control write/read interface for P4 kernel deadman controls."""

    @abc.abstractmethod
    def write_u32(self, control_name: str, value: int) -> bool:
        """Write unsigned 32-bit value (`GUARD_MAGIC` or `0`) to ALSA control."""

    @abc.abstractmethod
    def read_u32(self, control_name: str) -> int | None:
        """Read current unlock state/value from ALSA control, or None on error."""


class AmixerControlBackend(AlsaControlBackend):
    """Production ALSA control backend invoking `amixer -c <card> cset` (never called in unit tests)."""

    def __init__(self, card: str = CARD_DEFAULT, amixer_bin: str = "amixer", timeout_s: float = 1.0) -> None:
        self.card = card
        self.amixer_bin = amixer_bin
        self.timeout_s = timeout_s

    def write_u32(self, control_name: str, value: int) -> bool:
        if not (0 <= value <= 0xFFFFFFFF):
            return False
        try:
            res = subprocess.run(
                [self.amixer_bin, "-c", self.card, "-q", "cset", f"name={control_name}", str(value)],
                capture_output=True,
                timeout=self.timeout_s,
                check=False,
            )
            return res.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def read_u32(self, control_name: str) -> int | None:
        try:
            res = subprocess.run(
                [self.amixer_bin, "-c", self.card, "cget", f"name={control_name}"],
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if res.returncode != 0:
            return None
        for line in res.stdout.splitlines():
            if ": values=" in line:
                raw = line.split("=", 1)[1].strip().split(",")[0]
                try:
                    return int(raw, 0)
                except ValueError:
                    return None
        return None


@dataclass
class _MockKernelAmpGuard:
    """Exact Python mirror of `struct rt1316_xps17_guard_state` (rt1316-xps17-policy.h)."""
    unlocked: bool = False
    generation: int = 0
    last_kick_ms: float = 0.0
    expire_ms: float = 0.0
    stream_active: bool = True
    suspended: bool = False

    def force_lock(self) -> bool:
        was = self.unlocked
        self.unlocked = False
        self.expire_ms = 0.0
        if was:
            self.generation += 1
        return was

    def kick(self, magic: int, now_ms: float) -> bool:
        if magic != GUARD_MAGIC or self.suspended or not self.stream_active:
            self.force_lock()
            return False
        if not self.unlocked:
            self.unlocked = True
            self.generation += 1
        self.last_kick_ms = now_ms
        self.expire_ms = now_ms + KERNEL_TIMEOUT_MS
        return True

    def tick(self, now_ms: float) -> bool:
        if not self.unlocked:
            return False
        if self.suspended or not self.stream_active or now_ms >= self.expire_ms or now_ms < self.last_kick_ms:
            return self.force_lock()
        return False

    def target_regs(self) -> tuple[int, int, int]:
        allow = self.unlocked and self.stream_active and not self.suspended
        if allow:
            return (HV_GAIN_UNLOCKED, DIG_GAIN_UNLOCKED, DIG_GAIN_UNLOCKED)
        return (HV_GAIN_STOCK, DIG_GAIN_STOCK, DIG_GAIN_STOCK)


class MockAlsaControlBackend(AlsaControlBackend):
    """Deterministic offline mock simulating BOTH kernel P4 250 ms deadman watchdogs."""

    def __init__(
        self,
        controls: Sequence[str] = (CTL_LEFT, CTL_RIGHT),
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._now_s = 0.0
        self._clock = clock
        self.guards: dict[str, _MockKernelAmpGuard] = {c: _MockKernelAmpGuard() for c in controls}
        self.write_log: list[tuple[float, str, int]] = []
        self.fail_controls: set[str] = set()

    def now_s(self) -> float:
        return self._clock() if self._clock is not None else self._now_s

    def advance_to(self, now_s: float) -> dict[str, bool]:
        """Advance simulated kernel monotonic clock and tick 250 ms deadman watchdogs."""
        self._now_s = now_s
        now_ms = now_s * 1000.0
        return {name: g.tick(now_ms) for name, g in self.guards.items()}

    def set_stream_active(self, active: bool) -> None:
        for g in self.guards.values():
            g.stream_active = active
            if not active:
                g.force_lock()

    def set_suspended(self, suspended: bool) -> None:
        for g in self.guards.values():
            g.suspended = suspended
            if suspended:
                g.force_lock()

    def write_u32(self, control_name: str, value: int) -> bool:
        t = self.now_s()
        self.advance_to(t)
        if control_name not in self.guards or control_name in self.fail_controls:
            return False
        self.write_log.append((t, control_name, value))
        self.guards[control_name].kick(value, t * 1000.0)
        return True

    def read_u32(self, control_name: str) -> int | None:
        t = self.now_s()
        self.advance_to(t)
        g = self.guards.get(control_name)
        if g is None or control_name in self.fail_controls:
            return None
        return 1 if g.unlocked else 0

    def all_unlocked(self) -> bool:
        self.advance_to(self.now_s())
        return all(g.unlocked for g in self.guards.values())

    def all_locked(self) -> bool:
        self.advance_to(self.now_s())
        return all(not g.unlocked for g in self.guards.values())

    def target_regs(self, control_name: str) -> tuple[int, int, int]:
        self.advance_to(self.now_s())
        return self.guards[control_name].target_regs()


# ---- Userspace Safety State Machine ------------------------------------------
class P4DeadmanBridge:
    """Fail-closed userspace state machine bridging speaker-guard telemetry to kernel P4."""

    def __init__(
        self,
        backend: AlsaControlBackend,
        config: BridgeConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.backend = backend
        self.cfg = config or BridgeConfig()
        self._clock = clock
        self.state = BridgeState.LOCKED
        self._healthy_since_s: float | None = None
        self._last_kick_s: float | None = None
        self._trips_s: list[float] = []
        self._lockout_until_s: float = -math.inf
        self._prev_sig_handlers: dict[int, object] = {}
        self.events: list[str] = []

    @property
    def controls(self) -> tuple[str, str]:
        return (self.cfg.ctl_left, self.cfg.ctl_right)

    def _emit_lock_both(self, reason: str) -> bool:
        """Send explicit 0 (instant kernel lock) to BOTH amplifier P4 controls."""
        ok_l = self.backend.write_u32(self.cfg.ctl_left, GUARD_LOCK_VAL)
        ok_r = self.backend.write_u32(self.cfg.ctl_right, GUARD_LOCK_VAL)
        self._last_kick_s = None
        self.events.append(f"lock({reason}): left={ok_l} right={ok_r}")
        return ok_l and ok_r

    def _emit_unlock_both(self, now_s: float) -> bool:
        """Send `0xdec1be15` symmetrically to BOTH amps; fail-closed if either write fails."""
        ok_l = self.backend.write_u32(self.cfg.ctl_left, GUARD_MAGIC)
        if not ok_l:
            self._emit_lock_both("left_write_failed")
            return False
        ok_r = self.backend.write_u32(self.cfg.ctl_right, GUARD_MAGIC)
        if not ok_r:
            # Fail-closed symmetric rollback: NEVER leave left amp unlocked if right failed!
            self._emit_lock_both("right_write_failed_rollback_left")
            return False
        self._last_kick_s = now_s
        return True

    def _record_trip(self, now_s: float, reason: str) -> None:
        window = self.cfg.trip_window_s
        self._trips_s = [t for t in self._trips_s if now_s - t < window] + [now_s]
        if len(self._trips_s) >= self.cfg.max_trips_per_window:
            self._lockout_until_s = now_s + self.cfg.lockout_duration_s
            self.state = BridgeState.LOCKOUT
            self.events.append(
                f"lockout({reason}): {len(self._trips_s)} trips in {window:.0f}s -> "
                f"held locked for {self.cfg.lockout_duration_s:.0f}s"
            )
        else:
            self.state = BridgeState.LOCKED

    def _validate_telemetry(
        self,
        now_s: float,
        tel: GuardTelemetry | None,
    ) -> tuple[list[str], float | None, float | None]:
        """Validate ALL 7 safety invariants; returns (reasons, max_coil_c, max_excursion_ratio)."""
        reasons: list[str] = []
        if tel is None:
            return ["no_telemetry"], None, None

        # Invariant 1: trusted calibration (zero PLACEHOLDER / PROVISIONAL)
        if not tel.calibration_trusted:
            reasons.append("calibration_untrusted")

        # Invariant 2: non-colliding 4-channel IV-sense slots across both amps
        slots_ok, slots_msg = tel.iv_slots.non_colliding()
        if not slots_ok:
            reasons.append(f"iv_slot_invalid:{slots_msg}")

        # Invariant 3: fresh, plausible IV-sense frame for BOTH amps, zero XRUN, valid sample rate
        if tel.amp_present != (True, True):
            reasons.append(f"amp_missing:{tel.amp_present}")
        age_ms = (now_s - tel.timestamp_s) * 1000.0
        if not math.isfinite(age_ms) or age_ms < -5.0:
            reasons.append(f"clock_skew_ms:{age_ms:.1f}")
        elif age_ms > self.cfg.max_frame_age_ms:
            reasons.append(f"iv_sense_stale:{age_ms:.1f}ms>{self.cfg.max_frame_age_ms:.1f}ms")
        if not tel.plausible:
            reasons.append("iv_sense_implausible")
        if tel.xrun_count != 0:
            reasons.append(f"iv_sense_xrun:{tel.xrun_count}")
        min_rate = self.cfg.nominal_rate_hz * (1.0 - self.cfg.rate_tolerance_frac)
        max_rate = self.cfg.nominal_rate_hz * (1.0 + self.cfg.rate_tolerance_frac)
        if not (math.isfinite(tel.sample_rate_hz) and min_rate <= tel.sample_rate_hz <= max_rate):
            reasons.append(f"sample_rate_out_of_range:{tel.sample_rate_hz}")

        # Invariant 4: IPC4 DRC fence intact
        if not tel.drc_switch_off:
            reasons.append("drc_switch_not_off")

        # Invariant 7: stream active
        if not tel.stream_active:
            reasons.append("stream_idle")

        # Optional guard attenuation check: never elevate hardware gain while thermal guard is attenuating
        if tel.guard_atten_db is not None and any(v < -1e-6 for v in tel.guard_atten_db.values()):
            reasons.append("guard_attenuating")

        # Invariant 5: coil temperatures finite & present for all 4 drivers, checked with hysteresis
        max_coil: float | None = None
        missing_t = [d for d in REQUIRED_DRIVERS if d not in tel.t_coil_c]
        if missing_t:
            reasons.append(f"missing_coil_temp:{','.join(missing_t)}")
        else:
            temps = [tel.t_coil_c[d] for d in REQUIRED_DRIVERS]
            if any(not math.isfinite(t) or t < self.cfg.t_ambient_min_c for t in temps):
                reasons.append("invalid_coil_temp")
            else:
                max_coil = max(temps)
                if self.state == BridgeState.UNLOCKED:
                    if max_coil >= self.cfg.t_unlock_ceiling_c:
                        reasons.append(f"thermal_trip:{max_coil:.2f}C>={self.cfg.t_unlock_ceiling_c:.1f}C")
                else:
                    re_enable_c = self.cfg.t_unlock_ceiling_c - self.cfg.t_hysteresis_c
                    if max_coil > re_enable_c:
                        reasons.append(f"thermal_hysteresis:{max_coil:.2f}C>{re_enable_c:.1f}C")

        if tel.t_steady_elevated_c is not None and self.state != BridgeState.UNLOCKED:
            re_enable_c = self.cfg.t_unlock_ceiling_c - self.cfg.t_hysteresis_c
            for drv, t_ss in tel.t_steady_elevated_c.items():
                if not math.isfinite(t_ss) or t_ss > re_enable_c:
                    reasons.append(f"predicted_steady_hot:{drv}={t_ss:.1f}C")

        # Invariant 6: woofer excursion finite & below trip/release threshold with hysteresis
        max_exc_ratio: float | None = None
        missing_x = [d for d in WOOFER_DRIVERS if d not in tel.excursion_peak_mm]
        if missing_x:
            reasons.append(f"missing_excursion:{','.join(missing_x)}")
        else:
            excs = [tel.excursion_peak_mm[d] for d in WOOFER_DRIVERS]
            if any(not math.isfinite(x) or x < 0.0 for x in excs):
                reasons.append("invalid_excursion")
            else:
                max_exc_ratio = max(excs) / self.cfg.x_max_mm
                if self.state == BridgeState.UNLOCKED:
                    if max_exc_ratio >= self.cfg.excursion_trip_ratio:
                        reasons.append(
                            f"excursion_trip:{max_exc_ratio:.3f}>={self.cfg.excursion_trip_ratio:.2f}"
                        )
                else:
                    if max_exc_ratio > self.cfg.excursion_release_ratio:
                        reasons.append(
                            f"excursion_hysteresis:{max_exc_ratio:.3f}>{self.cfg.excursion_release_ratio:.2f}"
                        )

        return reasons, max_coil, max_exc_ratio

    def evaluate_and_drive(self, now_s: float, telemetry: GuardTelemetry | None) -> BridgeDecision:
        """Evaluate one control period and drive kernel P4 ALSA controls safely."""
        if self.state == BridgeState.SHUTDOWN:
            return BridgeDecision(
                state=BridgeState.SHUTDOWN,
                unlocked=False,
                kicked=False,
                explicit_lock_sent=False,
                reasons=("shutdown",),
                max_coil_c=None,
                max_excursion_ratio=None,
            )

        reasons, max_coil, max_exc = self._validate_telemetry(now_s, telemetry)

        # Check active anti-flapping lockout timer
        if self.state == BridgeState.LOCKOUT:
            if now_s < self._lockout_until_s:
                reasons.append(f"lockout_active:{self._lockout_until_s - now_s:.1f}s")
            else:
                self.state = BridgeState.LOCKED
                self.events.append("lockout_expired")

        # Any invariant violation -> immediate fail-closed LOCK!
        if reasons:
            was_unlocked = self.state == BridgeState.UNLOCKED
            self._healthy_since_s = None
            # Count trips only when dropping out of UNLOCKED due to a fault (not clean stream idle)
            if was_unlocked:
                is_fault_trip = any(r != "stream_idle" for r in reasons)
                if is_fault_trip:
                    self._record_trip(now_s, ";".join(reasons))
                else:
                    self.state = BridgeState.LOCKED
                self._emit_lock_both(";".join(reasons))
                return BridgeDecision(
                    state=self.state,
                    unlocked=False,
                    kicked=False,
                    explicit_lock_sent=True,
                    reasons=tuple(reasons),
                    max_coil_c=max_coil,
                    max_excursion_ratio=max_exc,
                )
            return BridgeDecision(
                state=self.state,
                unlocked=False,
                kicked=False,
                explicit_lock_sent=False,
                reasons=tuple(reasons),
                max_coil_c=max_coil,
                max_excursion_ratio=max_exc,
            )

        # All invariants pass! Track continuous healthy holdoff before initial/re-unlock.
        if self._healthy_since_s is None:
            self._healthy_since_s = now_s

        if self.state != BridgeState.UNLOCKED:
            elapsed = now_s - self._healthy_since_s
            if elapsed + 1e-9 < self.cfg.unlock_holdoff_s:
                return BridgeDecision(
                    state=self.state,
                    unlocked=False,
                    kicked=False,
                    explicit_lock_sent=False,
                    reasons=(f"unlock_holdoff:{elapsed:.2f}s/{self.cfg.unlock_holdoff_s:.1f}s",),
                    max_coil_c=max_coil,
                    max_excursion_ratio=max_exc,
                )
            # Transition LOCKED -> UNLOCKED and send first magic heartbeat immediately
            if not self._emit_unlock_both(now_s):
                self._healthy_since_s = None
                self._record_trip(now_s, "alsa_write_failed")
                return BridgeDecision(
                    state=self.state,
                    unlocked=False,
                    kicked=False,
                    explicit_lock_sent=True,
                    reasons=("alsa_write_failed",),
                    max_coil_c=max_coil,
                    max_excursion_ratio=max_exc,
                )
            self.state = BridgeState.UNLOCKED
            self.events.append(f"unlock(t={now_s:.3f},coil={max_coil:.1f}C)")
            return BridgeDecision(
                state=self.state,
                unlocked=True,
                kicked=True,
                explicit_lock_sent=False,
                reasons=(),
                max_coil_c=max_coil,
                max_excursion_ratio=max_exc,
            )

        # Already UNLOCKED: send heartbeat at configured cadence (<= 100 ms)
        interval_s = self.cfg.heartbeat_interval_ms / 1000.0
        due = self._last_kick_s is None or (now_s - self._last_kick_s) >= interval_s - 1e-9
        if not due:
            return BridgeDecision(
                state=self.state,
                unlocked=True,
                kicked=False,
                explicit_lock_sent=False,
                reasons=(),
                max_coil_c=max_coil,
                max_excursion_ratio=max_exc,
            )

        if not self._emit_unlock_both(now_s):
            self._healthy_since_s = None
            self._record_trip(now_s, "alsa_heartbeat_write_failed")
            return BridgeDecision(
                state=self.state,
                unlocked=False,
                kicked=False,
                explicit_lock_sent=True,
                reasons=("alsa_heartbeat_write_failed",),
                max_coil_c=max_coil,
                max_excursion_ratio=max_exc,
            )
        return BridgeDecision(
            state=self.state,
            unlocked=True,
            kicked=True,
            explicit_lock_sent=False,
            reasons=(),
            max_coil_c=max_coil,
            max_excursion_ratio=max_exc,
        )

    def on_stall(self, now_s: float, *, send_explicit_lock: bool = True) -> BridgeDecision:
        """Handle IV-sense stream stall/timeout: stop kicking and optionally force explicit lock."""
        was_unlocked = self.state == BridgeState.UNLOCKED
        self._healthy_since_s = None
        self._last_kick_s = None
        if was_unlocked:
            self._record_trip(now_s, "iv_sense_stall")
        else:
            self.state = BridgeState.LOCKED
        locked_now = False
        if send_explicit_lock:
            self._emit_lock_both("iv_sense_stall")
            locked_now = True
        return BridgeDecision(
            state=self.state,
            unlocked=False,
            kicked=False,
            explicit_lock_sent=locked_now,
            reasons=("iv_sense_stall",),
            max_coil_c=None,
            max_excursion_ratio=None,
        )

    def shutdown(self, reason: str = "shutdown") -> None:
        """Idempotent immediate lock on clean exit, SIGTERM, or SIGINT."""
        if self.state == BridgeState.SHUTDOWN:
            return
        self.state = BridgeState.SHUTDOWN
        self._healthy_since_s = None
        self._emit_lock_both(reason)

    def install_signal_handlers(self, signals: Sequence[int] = (signal.SIGTERM, signal.SIGINT)) -> None:
        """Register SIGTERM/SIGINT handlers that immediately send explicit lock `0`."""
        def _handler(signum: int, _frame) -> None:
            sig_name = signal.Signals(signum).name
            self.shutdown(reason=f"signal:{sig_name}")

        for sig in signals:
            self._prev_sig_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, _handler)

    def restore_signal_handlers(self) -> None:
        for sig, prev in list(self._prev_sig_handlers.items()):
            signal.signal(sig, prev)
        self._prev_sig_handlers.clear()

    def __enter__(self) -> "P4DeadmanBridge":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.shutdown(reason="context_exit" if exc_type is None else f"exception:{exc_type.__name__}")

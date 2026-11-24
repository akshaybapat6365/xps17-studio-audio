#!/usr/bin/env python3
"""Comprehensive offline unit tests for P4DeadmanBridge (Mock ALSA backend + 250ms kernel watchdog)."""
from __future__ import annotations

import configparser
import math
import os
import signal
import sys
import unittest
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parent
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

import p4_deadman_bridge as pb


def make_telemetry(
    now_s: float,
    *,
    frame_age_s: float = 0.010,
    calibration_trusted: bool = True,
    iv_slots: pb.IvSenseSlots | None = None,
    plausible: bool = True,
    xrun_count: int = 0,
    sample_rate_hz: float = 48000.0,
    drc_switch_off: bool = True,
    stream_active: bool = True,
    t_coil: float = 55.0,
    t_coil_overrides: dict[str, float] | None = None,
    excursion_mm: float = 0.20,
    t_steady_c: float | None = None,
    amp_present: tuple[bool, bool] = (True, True),
    guard_atten_db: dict[str, float] | None = None,
) -> pb.GuardTelemetry:
    temps = {d: t_coil for d in pb.REQUIRED_DRIVERS}
    if t_coil_overrides:
        temps.update(t_coil_overrides)
    excs = {d: excursion_mm for d in pb.WOOFER_DRIVERS}
    steady = None if t_steady_c is None else {d: t_steady_c for d in pb.REQUIRED_DRIVERS}
    return pb.GuardTelemetry(
        timestamp_s=now_s - frame_age_s,
        calibration_trusted=calibration_trusted,
        iv_slots=iv_slots or pb.IvSenseSlots(),
        plausible=plausible,
        xrun_count=xrun_count,
        sample_rate_hz=sample_rate_hz,
        drc_switch_off=drc_switch_off,
        stream_active=stream_active,
        t_coil_c=temps,
        excursion_peak_mm=excs,
        t_steady_elevated_c=steady,
        amp_present=amp_present,
        guard_atten_db=guard_atten_db,
    )


class TestP4DeadmanBridge(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 100.0
        self.backend = pb.MockAlsaControlBackend(clock=lambda: self.now)
        self.cfg = pb.BridgeConfig(
            heartbeat_interval_ms=50.0,
            max_frame_age_ms=150.0,
            t_unlock_ceiling_c=70.0,
            t_hysteresis_c=3.0,
            unlock_holdoff_s=1.0,  # Short holdoff in tests; full 10s default verified separately
        )
        self.bridge = pb.P4DeadmanBridge(self.backend, self.cfg, clock=lambda: self.now)

    def _run_healthy_until_unlocked(self, step_s: float = 0.05) -> pb.BridgeDecision:
        """Feed healthy 50 ms frames until the holdoff finishes and bridge unlocks."""
        n_steps = int(round(self.cfg.unlock_holdoff_s / step_s)) + 1
        dec = None
        for _ in range(n_steps):
            dec = self.bridge.evaluate_and_drive(self.now, make_telemetry(self.now))
            if dec.unlocked:
                return dec
            self.now += step_s
            self.backend.advance_to(self.now)
        self.assertIsNotNone(dec)
        return dec

    def test_default_config_and_validation_bounds(self) -> None:
        default_cfg = pb.BridgeConfig()
        self.assertEqual(default_cfg.heartbeat_interval_ms, 50.0)
        self.assertLessEqual(default_cfg.heartbeat_interval_ms, 100.0)
        self.assertLess(default_cfg.max_frame_age_ms, pb.KERNEL_TIMEOUT_MS)
        self.assertEqual(default_cfg.ctl_left, "rt1316-1 Guard Drive Unlock")
        self.assertEqual(default_cfg.ctl_right, "rt1316-2 Guard Drive Unlock")
        self.assertEqual(default_cfg.unlock_holdoff_s, 10.0)

        # Reject heartbeat > 100 ms (violates 250 ms watchdog margin)
        with self.assertRaises(ValueError):
            pb.BridgeConfig(heartbeat_interval_ms=120.0)
        # Reject frame age >= 250 ms
        with self.assertRaises(ValueError):
            pb.BridgeConfig(max_frame_age_ms=250.0)

    def test_starts_locked_respects_holdoff_and_heartbeat_cadence(self) -> None:
        self.assertTrue(self.backend.all_locked())
        self.assertEqual(self.backend.target_regs(pb.CTL_LEFT), (0x01, 0xBF, 0xBF))

        # During holdoff (< 1.0 s), must stay strictly LOCKED with zero magic writes
        for i in range(20):  # 0.00 .. 0.95 s
            dec = self.bridge.evaluate_and_drive(self.now, make_telemetry(self.now))
            self.assertFalse(dec.unlocked, f"Unlocked prematurely at step {i}")
            self.assertFalse(dec.kicked)
            self.assertTrue(self.backend.all_locked())
            self.now += 0.05
            self.backend.advance_to(self.now)

        # At t = +1.00 s holdoff completes -> unlocks and kicks BOTH controls with 0xdec1be15
        dec = self.bridge.evaluate_and_drive(self.now, make_telemetry(self.now))
        self.assertTrue(dec.unlocked)
        self.assertTrue(dec.kicked)
        self.assertTrue(self.backend.all_unlocked())
        self.assertEqual(self.backend.target_regs(pb.CTL_LEFT), (0x05, 0xA9, 0xA9))
        self.assertEqual(self.backend.target_regs(pb.CTL_RIGHT), (0x05, 0xA9, 0xA9))

        # Run 2.0 seconds of 50 ms periods: verify cadence <= 100 ms keeps both amps unlocked continuously
        kick_times_left = []
        for _ in range(40):
            self.now += 0.05
            self.backend.advance_to(self.now)
            dec = self.bridge.evaluate_and_drive(self.now, make_telemetry(self.now))
            self.assertTrue(dec.unlocked)
            self.assertTrue(dec.kicked)
            self.assertTrue(self.backend.all_unlocked())
            kick_times_left.append(self.now)

        # Verify every single heartbeat delta <= 100 ms and every magic == 0xdec1be15
        for a, b in zip(kick_times_left, kick_times_left[1:]):
            self.assertLessEqual((b - a) * 1000.0, 100.0 + 1e-6)
        self.assertTrue(all(v == 0xDEC1BE15 for _, _, v in self.backend.write_log))
        self.assertEqual(
            {ctl for _, ctl, _ in self.backend.write_log},
            {"rt1316-1 Guard Drive Unlock", "rt1316-2 Guard Drive Unlock"},
        )

    def test_refuses_unlock_on_untrusted_calibration_or_slot_collision_or_drc_on(self) -> None:
        # 1) Untrusted calibration
        for _ in range(30):
            dec = self.bridge.evaluate_and_drive(self.now, make_telemetry(self.now, calibration_trusted=False))
            self.assertFalse(dec.unlocked)
            self.assertIn("calibration_untrusted", dec.reasons)
            self.now += 0.05
        self.assertEqual(len(self.backend.write_log), 0)

        # 2) Colliding IV-sense slots (Linux stock bug: both amps 0xA0/0xB1)
        colliding = pb.IvSenseSlots(0xA0, 0xB1, 0xA0, 0xB1)
        for _ in range(30):
            dec = self.bridge.evaluate_and_drive(self.now, make_telemetry(self.now, iv_slots=colliding))
            self.assertFalse(dec.unlocked)
            self.assertTrue(any(r.startswith("iv_slot_invalid") for r in dec.reasons))
            self.now += 0.05
        self.assertEqual(len(self.backend.write_log), 0)

        # 3) IPC4 DRC switch ON / unverified
        for _ in range(30):
            dec = self.bridge.evaluate_and_drive(self.now, make_telemetry(self.now, drc_switch_off=False))
            self.assertFalse(dec.unlocked)
            self.assertIn("drc_switch_not_off", dec.reasons)
            self.now += 0.05
        self.assertEqual(len(self.backend.write_log), 0)

    def test_thermal_trip_immediate_lock_and_hysteresis_reunlock(self) -> None:
        self.assertTrue(self._run_healthy_until_unlocked().unlocked)
        self.assertTrue(self.backend.all_unlocked())

        # Coil stays below ceiling (69.5 C < 70.0 C) -> stays unlocked
        self.now += 0.05
        dec = self.bridge.evaluate_and_drive(self.now, make_telemetry(self.now, t_coil=69.5))
        self.assertTrue(dec.unlocked)

        # Thermal trip on right_tweeter hitting 70.1 C -> IMMEDIATE explicit lock (0) on BOTH amps!
        self.now += 0.05
        dec = self.bridge.evaluate_and_drive(
            self.now,
            make_telemetry(self.now, t_coil=60.0, t_coil_overrides={"right_tweeter": 70.1}),
        )
        self.assertFalse(dec.unlocked)
        self.assertTrue(dec.explicit_lock_sent)
        self.assertTrue(self.backend.all_locked())
        self.assertEqual(self.backend.write_log[-2][2], 0)
        self.assertEqual(self.backend.write_log[-1][2], 0)
        self.assertEqual(self.backend.target_regs(pb.CTL_LEFT), (0x01, 0xBF, 0xBF))

        # Inside hysteresis band (68.5 C > 70 - 3 = 67.0 C): MUST remain locked even after holdoff!
        for _ in range(30):
            self.now += 0.05
            dec = self.bridge.evaluate_and_drive(self.now, make_telemetry(self.now, t_coil=68.5))
            self.assertFalse(dec.unlocked)
            self.assertTrue(any(r.startswith("thermal_hysteresis") for r in dec.reasons))

        # Cool below hysteresis threshold (66.5 C <= 67.0 C) -> re-unlocks after holdoff
        self.now += 0.05
        dec = self._run_healthy_until_unlocked()
        self.assertTrue(dec.unlocked)
        self.assertTrue(self.backend.all_unlocked())

    def test_excursion_trip_and_hysteresis(self) -> None:
        self.assertTrue(self._run_healthy_until_unlocked().unlocked)

        # Excursion hits 0.46 mm / 0.50 mm = 0.92 >= 0.90 -> IMMEDIATE explicit lock!
        self.now += 0.05
        dec = self.bridge.evaluate_and_drive(self.now, make_telemetry(self.now, excursion_mm=0.46))
        self.assertFalse(dec.unlocked)
        self.assertTrue(dec.explicit_lock_sent)
        self.assertTrue(self.backend.all_locked())

        # Inside excursion hysteresis (0.42 mm = 0.84 > 0.80) -> stays locked
        for _ in range(30):
            self.now += 0.05
            dec = self.bridge.evaluate_and_drive(self.now, make_telemetry(self.now, excursion_mm=0.42))
            self.assertFalse(dec.unlocked)

        # Drops to 0.35 mm (0.70 <= 0.80) -> re-unlocks after holdoff
        self.now += 0.05
        self.assertTrue(self._run_healthy_until_unlocked().unlocked)

    def test_xrun_nan_and_stream_idle_immediate_lock(self) -> None:
        # 1) XRUN triggers immediate lock
        self.assertTrue(self._run_healthy_until_unlocked().unlocked)
        self.now += 0.05
        dec = self.bridge.evaluate_and_drive(self.now, make_telemetry(self.now, xrun_count=1))
        self.assertFalse(dec.unlocked)
        self.assertTrue(dec.explicit_lock_sent)
        self.assertTrue(self.backend.all_locked())

        # 2) NaN coil temperature triggers immediate lock
        self.now += 0.05
        self.assertTrue(self._run_healthy_until_unlocked().unlocked)
        self.now += 0.05
        dec = self.bridge.evaluate_and_drive(
            self.now,
            make_telemetry(self.now, t_coil_overrides={"left_woofer": math.nan}),
        )
        self.assertFalse(dec.unlocked)
        self.assertTrue(dec.explicit_lock_sent)
        self.assertTrue(self.backend.all_locked())

        # 3) Stream idle triggers immediate lock (and does NOT count as a fault trip)
        self.now += 0.05
        self.assertTrue(self._run_healthy_until_unlocked().unlocked)
        self.now += 0.05
        dec = self.bridge.evaluate_and_drive(self.now, make_telemetry(self.now, stream_active=False))
        self.assertFalse(dec.unlocked)
        self.assertTrue(dec.explicit_lock_sent)
        self.assertEqual(dec.state, pb.BridgeState.LOCKED)
        self.assertTrue(self.backend.all_locked())

    def test_stall_stops_kicking_and_kernel_250ms_watchdog_autonomously_locks(self) -> None:
        """Prove BOTH userspace stall handling AND kernel 250ms deadman expiry when kicking ceases."""
        self.assertTrue(self._run_healthy_until_unlocked().unlocked)
        t_last_kick = self.now
        self.assertTrue(self.backend.all_unlocked())

        # Simulate stall where userspace stops kicking WITHOUT sending explicit 0 (e.g. hung/SIGKILL)
        dec = self.bridge.on_stall(self.now + 0.16, send_explicit_lock=False)
        self.assertFalse(dec.unlocked)
        self.assertFalse(dec.kicked)

        # At t_last_kick + 200 ms (< 250 ms): kernel watchdog has NOT expired yet
        self.now = t_last_kick + 0.200
        expired_200 = self.backend.advance_to(self.now)
        self.assertEqual(expired_200, {pb.CTL_LEFT: False, pb.CTL_RIGHT: False})
        self.assertTrue(self.backend.all_unlocked())

        # At t_last_kick + 250 ms: kernel 250ms deadman watchdog AUTONOMOUSLY locks BOTH amps!
        self.now = t_last_kick + 0.250
        expired_250 = self.backend.advance_to(self.now)
        self.assertEqual(expired_250, {pb.CTL_LEFT: True, pb.CTL_RIGHT: True})
        self.assertTrue(self.backend.all_locked())
        self.assertEqual(self.backend.target_regs(pb.CTL_LEFT), (0x01, 0xBF, 0xBF))
        self.assertEqual(self.backend.target_regs(pb.CTL_RIGHT), (0x01, 0xBF, 0xBF))

        # Also prove stale-frame detection inside evaluate_and_drive immediately sends explicit 0
        self.now += 0.05
        self.assertTrue(self._run_healthy_until_unlocked().unlocked)
        self.now += 0.05
        dec_stale = self.bridge.evaluate_and_drive(self.now, make_telemetry(self.now, frame_age_s=0.160))
        self.assertFalse(dec_stale.unlocked)
        self.assertTrue(dec_stale.explicit_lock_sent)
        self.assertTrue(self.backend.all_locked())

    def test_asymmetric_alsa_write_failure_rolls_back_symmetrically(self) -> None:
        """If rt1316-2 write fails after rt1316-1 succeeded, rt1316-1 MUST be immediately re-locked."""
        self.backend.fail_controls.add(pb.CTL_RIGHT)
        for _ in range(21):
            dec = self.bridge.evaluate_and_drive(self.now, make_telemetry(self.now))
            self.now += 0.05
        self.assertFalse(dec.unlocked)
        self.assertTrue(dec.explicit_lock_sent)
        # Left amp MUST have been rolled back to locked!
        self.assertFalse(self.backend.guards[pb.CTL_LEFT].unlocked)
        self.assertTrue(self.backend.all_locked())
        self.assertEqual(self.backend.write_log[-1], (self.now - 0.05, pb.CTL_LEFT, 0))

    def test_flapping_protection_triggers_lockout(self) -> None:
        """3 fault trips within 600 s enforce a 600 s lockout."""
        for trip_idx in range(3):
            self.assertTrue(self._run_healthy_until_unlocked().unlocked, f"unlock #{trip_idx}")
            self.now += 0.05
            dec = self.bridge.evaluate_and_drive(self.now, make_telemetry(self.now, t_coil=72.0))
            self.assertFalse(dec.unlocked)
            self.now += 0.05

        self.assertEqual(self.bridge.state, pb.BridgeState.LOCKOUT)
        # Even cool, healthy telemetry cannot unlock during lockout
        for _ in range(30):
            dec = self.bridge.evaluate_and_drive(self.now, make_telemetry(self.now, t_coil=55.0))
            self.assertFalse(dec.unlocked)
            self.assertEqual(self.bridge.state, pb.BridgeState.LOCKOUT)
            self.now += 0.05

        # After 600 s lockout expires, normal holdoff + unlock succeeds
        self.now += 605.0
        self.backend.advance_to(self.now)
        self.assertTrue(self._run_healthy_until_unlocked().unlocked)

    def test_sigterm_and_context_manager_emit_explicit_lock(self) -> None:
        # 1) Real SIGTERM delivery triggers immediate explicit lock (0) on BOTH controls
        self.assertTrue(self._run_healthy_until_unlocked().unlocked)
        self.assertTrue(self.backend.all_unlocked())
        self.bridge.install_signal_handlers((signal.SIGTERM,))
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        finally:
            self.bridge.restore_signal_handlers()

        self.assertEqual(self.bridge.state, pb.BridgeState.SHUTDOWN)
        self.assertTrue(self.backend.all_locked())
        self.assertEqual(self.backend.write_log[-2][1:], (pb.CTL_LEFT, 0))
        self.assertEqual(self.backend.write_log[-1][1:], (pb.CTL_RIGHT, 0))

        # Subsequent evaluate_and_drive after SHUTDOWN is a permanent locked no-op
        dec = self.bridge.evaluate_and_drive(self.now + 0.05, make_telemetry(self.now + 0.05))
        self.assertFalse(dec.unlocked)
        self.assertEqual(dec.state, pb.BridgeState.SHUTDOWN)

        # 2) Context manager __exit__ emits explicit lock
        backend2 = pb.MockAlsaControlBackend(clock=lambda: self.now)
        with pb.P4DeadmanBridge(backend2, self.cfg, clock=lambda: self.now) as b2:
            for _ in range(25):
                b2.evaluate_and_drive(self.now, make_telemetry(self.now))
                self.now += 0.05
            self.assertTrue(backend2.all_unlocked())
        self.assertTrue(backend2.all_locked())
        self.assertEqual(backend2.write_log[-1][2], 0)

    def test_systemd_dropin_and_speaker_guard_adapter(self) -> None:
        dropin = BRIDGE_DIR / "omarchy-speaker-guard-p4.conf"
        self.assertTrue(dropin.is_file())
        raw = dropin.read_text(encoding="utf-8")
        self.assertIn("SPK_GUARD_P4_MAGIC=0xdec1be15", raw)
        self.assertIn("SPK_GUARD_P4_HEARTBEAT_MS=50", raw)
        self.assertIn('ExecStopPost=-/usr/bin/amixer -c sofsoundwire -q cset "name=rt1316-1 Guard Drive Unlock" 0', raw)
        self.assertIn('ExecStopPost=-/usr/bin/amixer -c sofsoundwire -q cset "name=rt1316-2 Guard Drive Unlock" 0', raw)

        # Verify adapter directly consumes a speaker_guard.guard.Output-like object
        class _FakeGuardOutput:
            health = "healthy"
            reasons = ()
            temps = {d: 58.0 for d in pb.REQUIRED_DRIVERS}
            atten_db = {"left": 0.0, "right": 0.0}

        tel = pb.GuardTelemetry.from_speaker_guard_output(
            timestamp_s=self.now - 0.01,
            guard_output=_FakeGuardOutput(),
            calibration_trusted=True,
            iv_slots=pb.IvSenseSlots(),
            xrun_count=0,
            sample_rate_hz=48000.0,
            drc_switch_off=True,
            stream_active=True,
            excursion_peak_mm={"left_woofer": 0.2, "right_woofer": 0.2},
        )
        self.assertTrue(tel.plausible)
        self.assertEqual(tel.t_coil_c["left_woofer"], 58.0)

    def test_real_speaker_guard_two_node_integration(self) -> None:
        """Read-only end-to-end test coupling real speaker_guard.Guard to P4DeadmanBridge."""
        sg_root = Path("/home/akshaybapat/project/xps17-speaker-stack/sense/design/guard")
        model_ini = Path("/home/akshaybapat/project/xps17-speaker-stack/sense/design/model/xps17-9720.ini")
        if not (sg_root.is_dir() and model_ini.is_file()):
            self.skipTest("speaker_guard reference tree not present")

        sys.path.insert(0, str(sg_root))
        try:
            from speaker_guard import model as sg_model
            from speaker_guard.guard import Guard
            from speaker_guard.sense import Block
        finally:
            sys.path.pop(0)

        # Raw model has PLACEHOLDERs -> bridge MUST refuse unlock
        raw_m = sg_model.load(model_ini)
        self.assertFalse(raw_m.elevation_allowed()[0])

        # Trusted copy (simulation only) driving real two-node thermal estimator
        m = raw_m.trusted()
        self.assertTrue(m.elevation_allowed()[0])
        g = Guard(m, "elevated", now=self.now, state=None, first_start=True)

        # Feed cool, plausible blocks until bridge unlocks
        for step in range(25):
            t = self.now + step * 0.05
            self.backend.advance_to(t)
            # Small quiet playback (below reference_on_dbfs = -50 dBFS) with noise-floor sense
            blk = Block(
                t=t,
                frames=2400,
                rms=(10.0, 10.0, 10.0, 10.0),
                peak=(50, 50, 50, 50),
                digest=step + 1,
                ref_rms={"left": 0.001, "right": 0.001},
                rate_hz=48000.0,
            )
            g_out = g.step(
                blk,
                applied_db={"left": 0.0, "right": 0.0},
                gate_open={"left": self.backend.guards[pb.CTL_LEFT].unlocked,
                           "right": self.backend.guards[pb.CTL_RIGHT].unlocked},
            )
            tel = pb.GuardTelemetry.from_speaker_guard_output(
                timestamp_s=t,
                guard_output=g_out,
                calibration_trusted=m.elevation_allowed()[0],
                iv_slots=pb.IvSenseSlots(),
                xrun_count=0,
                sample_rate_hz=48000.0,
                drc_switch_off=True,
                stream_active=True,
                excursion_peak_mm={"left_woofer": 0.15, "right_woofer": 0.15},
            )
            dec = self.bridge.evaluate_and_drive(t, tel)
        self.assertTrue(dec.unlocked)
        self.assertTrue(self.backend.all_unlocked())

        # Now inject an implausible full-scale sense fault into real Guard -> bridge MUST lock immediately
        t_fault = self.now + 25 * 0.05
        self.backend.advance_to(t_fault)
        bad_blk = Block(
            t=t_fault,
            frames=2400,
            rms=(9000000.0, 10.0, 10.0, 10.0),
            peak=(8388607, 50, 50, 50),
            digest=9999,
            ref_rms={"left": 0.001, "right": 0.001},
            rate_hz=48000.0,
        )
        g_fault = g.step(bad_blk, applied_db={"left": 0.0, "right": 0.0}, gate_open={"left": True, "right": True})
        self.assertEqual(g_fault.health, "fault")
        tel_fault = pb.GuardTelemetry.from_speaker_guard_output(
            timestamp_s=t_fault,
            guard_output=g_fault,
            calibration_trusted=m.elevation_allowed()[0],
            iv_slots=pb.IvSenseSlots(),
            xrun_count=0,
            sample_rate_hz=48000.0,
            drc_switch_off=True,
            stream_active=True,
            excursion_peak_mm={"left_woofer": 0.15, "right_woofer": 0.15},
        )
        dec_fault = self.bridge.evaluate_and_drive(t_fault, tel_fault)
        self.assertFalse(dec_fault.unlocked)
        self.assertTrue(dec_fault.explicit_lock_sent)
        self.assertTrue(self.backend.all_locked())


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Comprehensive unit & integration tests for the staged XPS 17 Studio DSP controller,
PipeWire filter-chain drop-in, speaker-profile CLI, and Omarchy menu patch.

Strictly offline and read-only on the live system: never modifies live configs,
never restarts PipeWire/WirePlumber/EasyEffects, and never plays audio.
"""
from __future__ import annotations

import importlib.util
import json
import math
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from collections import defaultdict, deque
from pathlib import Path
from unittest import mock


STAGED_ROOT = Path(__file__).resolve().parent.parent
CONTROLLER_DIR = STAGED_ROOT / "controller"
PIPEWIRE_CONF = STAGED_ROOT / "pipewire" / "92-xps17-studio.conf"
SPEAKER_PROFILE_BIN = STAGED_ROOT / "bin" / "speaker-profile"
SPEAKER_PROFILE_DIFF = STAGED_ROOT / "bin" / "speaker-profile.diff"
AUDIO_PATCH_DIFF = CONTROLLER_DIR / "omarchy_audio_studio_patch.diff"
MENU_SNIPPET = STAGED_ROOT / "menu" / "omarchy-menu-speakers-snippet.jsonc"
MENU_DIFF = STAGED_ROOT / "menu" / "omarchy-menu.jsonc.diff"

LIVE_OMARCHY_AUDIO = Path("/home/akshaybapat/.local/lib/omarchy_audio")
LIVE_PIPEWIRE_DIR = Path("/home/akshaybapat/.config/pipewire")
LIVE_SPEAKER_PROFILE = Path("/home/akshaybapat/.local/bin/speaker-profile")
LIVE_MENU_JSONC = Path("/home/akshaybapat/.config/omarchy/extensions/omarchy-menu.jsonc")


def _load_staged_omarchy_audio() -> tuple[types.ModuleType, types.ModuleType]:
    """Load staged model.py and runtime.py inside an isolated synthetic `omarchy_audio`
    package (borrowing only untouched read-only server.py from ~/.local/lib/omarchy_audio)."""
    pkg = types.ModuleType("omarchy_audio")
    pkg.__path__ = [str(CONTROLLER_DIR), str(LIVE_OMARCHY_AUDIO)]
    sys.modules["omarchy_audio"] = pkg

    def load_sub(name: str, path: Path) -> types.ModuleType:
        full_name = f"omarchy_audio.{name}"
        spec = importlib.util.spec_from_file_location(full_name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = module
        spec.loader.exec_module(module)
        setattr(pkg, name, module)
        return module

    server_src = LIVE_OMARCHY_AUDIO / "server.py"
    if server_src.is_file():
        load_sub("server", server_src)
    else:
        srv_mod = types.ModuleType("omarchy_audio.server")
        class _StubLocalServer:
            def __init__(self, path: Path) -> None:
                self.path = path
            def request(self, command: str) -> str:
                raise OSError("EasyEffects unavailable in offline CI")
        srv_mod.LocalServer = _StubLocalServer  # type: ignore[attr-defined]
        sys.modules["omarchy_audio.server"] = srv_mod
        setattr(pkg, "server", srv_mod)
    model_mod = load_sub("model", CONTROLLER_DIR / "model.py")
    runtime_mod = load_sub("runtime", CONTROLLER_DIR / "runtime.py")
    return model_mod, runtime_mod


model, runtime = _load_staged_omarchy_audio()


PHYSICAL_SPEAKER = "alsa_output.pci-0000_00_1f.3-platform-sof_sdw.HiFi__Speaker__sink"
BLUETOOTH_SINK = "bluez_output.AA_BB_CC_DD_EE_FF.1"

EXPECTED_STUDIO_CONTROLS = {
    "Mode",
    "Volume (dB)",
    "Bass Depth",
    "Physical Bass Hz",
    "Warmth Body",
    "Stage Depth",
    "Spatial XTC",
    "Drive (dB)",
    "Ceiling (dBFS)",
    "Excursion Guard",
}


def _make_sink(index: int, name: str, serial: int, volume: int = 65536, mute: bool = False, extra_props: dict | None = None) -> dict:
    props = {"object.serial": str(serial)}
    if extra_props:
        props.update(extra_props)
    return {
        "index": index,
        "name": name,
        "mute": mute,
        "volume": {
            "front-left": {"value": volume},
            "front-right": {"value": volume},
        },
        "properties": props,
    }


def _full_inventory() -> tuple[dict, ...]:
    return (
        _make_sink(
            1,
            PHYSICAL_SPEAKER,
            101,
            extra_props={
                "alsa.card_name": runtime.INTERNAL_AUDIO_CARD,
                "device.profile.name": runtime.SPEAKER_PROFILE_NAME,
            },
        ),
        _make_sink(2, "omarchy_speaker_engine", 201),
        _make_sink(3, "omarchy_speaker_studio", 301),
        _make_sink(4, "omarchy_speaker_studio_spatial", 302),
        _make_sink(5, "omarchy_speaker_studio_ref", 303),
        _make_sink(6, "omarchy_speaker_studio_voice", 304),
        _make_sink(7, BLUETOOTH_SINK, 701),
    )


def _strip_jsonc_comments(text: str) -> str:
    out: list[str] = []
    in_str = False
    esc = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
        else:
            if ch == '"':
                in_str = True
                out.append(ch)
                i += 1
            elif ch == "/" and i + 1 < n and text[i + 1] == "/":
                while i < n and text[i] != "\n":
                    i += 1
            else:
                out.append(ch)
                i += 1
    return "".join(out)


class TestStudioVolumeMapping(unittest.TestCase):
    """Test continuous PipeWire cubic volume -> studio:Volume (dB) mapping across 0..120%."""

    def test_key_volume_points_and_clamping(self) -> None:
        cases = [
            (0, -45.0),
            (10, -45.0),  # 60*log10(0.10) = -60.0 -> clamped to -45.0
            (25, round(60.0 * math.log10(0.25), 3)),  # -36.124
            (50, round(60.0 * math.log10(0.50), 3)),  # -18.062
            (75, round(60.0 * math.log10(0.75), 3)),  # -7.496
            (80, round(60.0 * math.log10(0.80), 3)),  # -5.815
            (100, 0.0),
            (120, 0.0),  # +4.751 -> clamped to 0.0
        ]
        for pct, expected in cases:
            with self.subTest(volume_pct=pct):
                got = model.studio_volume_db(pct)
                self.assertAlmostEqual(got, expected, places=3)
                self.assertGreaterEqual(got, -45.0)
                self.assertLessEqual(got, 0.0)
        # Ensure 100% and 120% are positive +0.0 (never IEEE -0.0)
        self.assertFalse(math.copysign(1.0, model.studio_volume_db(100)) < 0)
        self.assertFalse(math.copysign(1.0, model.studio_volume_db(120)) < 0)

    def test_negative_and_full_sweep_monotonic_and_bounded(self) -> None:
        self.assertEqual(model.studio_volume_db(-10), -45.0)
        prev = -45.0
        for pct in range(0, 151):
            val = model.studio_volume_db(pct)
            self.assertGreaterEqual(val, -45.0)
            self.assertLessEqual(val, 0.0)
            self.assertGreaterEqual(val, prev)
            prev = val
        # Strictly increasing throughout the unclamped active band (18%..100%)
        self.assertLess(model.studio_volume_db(25), model.studio_volume_db(50))
        self.assertLess(model.studio_volume_db(50), model.studio_volume_db(75))
        self.assertLess(model.studio_volume_db(75), model.studio_volume_db(100))

    def test_spa_json_payload_and_port_name(self) -> None:
        changes = model.studio_tracking_changes(50)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].plugin, "studio")
        self.assertEqual(changes[0].name, "Volume (dB)")
        self.assertEqual(model.engine_param_key(changes[0]), "studio:Volume (dB)")
        spa = model.engine_props(changes)
        self.assertEqual(spa, '{ params = [ "studio:Volume (dB)" -18.062 ] }')


class TestLoudnessAuthorityPreservation(unittest.TestCase):
    """Verify LOUDNESS_DRIVE_DB authority is never weakened or pulled down."""

    def test_loudness_drive_constant_untouched(self) -> None:
        self.assertEqual(runtime.LOUDNESS_DRIVE_DB, 7.0)

    def test_studio_tracking_never_touches_drive_or_ceiling(self) -> None:
        for pct in (0, 10, 25, 50, 75, 80, 95, 100, 120, 150):
            changes = model.studio_tracking_changes(pct)
            keys = {(c.plugin, c.name) for c in changes}
            self.assertEqual(keys, {("studio", "Volume (dB)")})
            for c in changes:
                self.assertNotIn("Drive", c.name)
                self.assertNotIn("Ceiling", c.name)

    def test_easyeffects_loudness_drive_changes_preserved(self) -> None:
        class FakeClient:
            def __init__(self, current_gain: str) -> None:
                self.current_gain = current_gain
                self.requests: list[str] = []

            def request(self, command: str) -> str:
                self.requests.append(command)
                return self.current_gain

        c_low = FakeClient("0.0")
        self.assertEqual(
            runtime.loudness_drive_changes(c_low),
            (model.PropertyChange("limiter", "inputGain", 7.0),),
        )
        c_ok = FakeClient("7.0")
        self.assertEqual(runtime.loudness_drive_changes(c_ok), ())

    def test_cap_speaker_gain_protects_all_studio_sinks_and_ignores_bt(self) -> None:
        inv = _full_inventory()
        streams = (
            # App playing at 140% into Studio Deep -> MUST be capped to 100%
            {
                "index": 41,
                "sink": 3,
                "volume": {"front-left": {"value": 91750}, "front-right": {"value": 91750}},
                "properties": {"application.name": "Chromium", "node.name": "Chromium"},
            },
            # Studio Deep's own playback stream -> left to hold_engine_route, skipped here
            {
                "index": 42,
                "sink": 1,
                "volume": {"front-left": {"value": 80000}, "front-right": {"value": 80000}},
                "properties": {"node.name": "omarchy_speaker_studio_output"},
            },
            # App playing at 140% into Bluetooth -> MUST NOT be touched
            {
                "index": 43,
                "sink": 7,
                "volume": {"front-left": {"value": 91750}, "front-right": {"value": 91750}},
                "properties": {"application.name": "mpv", "node.name": "mpv"},
            },
        )
        cmds: list[tuple[str, ...]] = []
        with (
            mock.patch.object(runtime, "audio_lock"),
            mock.patch.object(runtime, "sink_inventory", return_value=inv),
            mock.patch.object(runtime, "default_sink", return_value="omarchy_speaker_studio"),
            mock.patch.object(runtime, "sink_inputs", return_value=streams),
            mock.patch.object(runtime, "run_text", side_effect=lambda c: cmds.append(c) or ""),
        ):
            capped = runtime.cap_speaker_gain()

        self.assertEqual(len(capped), 1)
        self.assertIn("Chromium", capped[0])
        self.assertIn("omarchy_speaker_studio", capped[0])
        self.assertEqual(
            cmds,
            [("/usr/bin/pactl", "set-sink-input-volume", "41", "65536", "65536")],
        )


class TestStudioSinkDiscoveryAndSwitching(unittest.TestCase):
    """Test active sink discovery, live switching, routing, and zero Warm · Deep regression."""

    def test_selected_engine_and_tracks_volume_for_all_profiles(self) -> None:
        for prof in ("studio-deep", "studio-spatial", "studio-ref", "studio-voice", "warm-deep", "warm-deep-subtle"):
            with mock.patch.object(runtime, "saved_profile", return_value=prof):
                self.assertEqual(runtime.selected_engine(), runtime.FILTER_CHAIN_ENGINE, msg=prof)
                self.assertTrue(runtime.tracks_volume(), msg=prof)
        for prof in ("warm", "warm-rich", "balanced", "voice", "flat", "baseline", "off"):
            with mock.patch.object(runtime, "saved_profile", return_value=prof):
                self.assertEqual(runtime.selected_engine(), runtime.EASY_EFFECTS_ENGINE, msg=prof)

    def test_resolve_active_engine_node_across_all_five_sinks(self) -> None:
        # Saved profile selects its Studio sink when bare speaker or BT is default
        for prof, node in runtime.STUDIO_PROFILES.items():
            self.assertEqual(runtime.resolve_active_engine_node(PHYSICAL_SPEAKER, prof), node)
            self.assertEqual(runtime.resolve_active_engine_node(BLUETOOTH_SINK, prof), node)

        # Live default sink overrides saved profile when user switches directly in PipeWire
        for node in (
            "omarchy_speaker_studio",
            "omarchy_speaker_studio_spatial",
            "omarchy_speaker_studio_ref",
            "omarchy_speaker_studio_voice",
            "omarchy_speaker_engine",
        ):
            self.assertEqual(runtime.resolve_active_engine_node(node, "warm-deep"), node)
            self.assertEqual(runtime.resolve_active_engine_node(node, "studio-deep"), node)

    def test_apply_engine_profile_tracks_volume_across_all_four_studio_sinks(self) -> None:
        profile_hp = {
            "studio-deep": ("omarchy_speaker_studio", 110, 82.0, 7.0),
            "studio-spatial": ("omarchy_speaker_studio_spatial", 111, 82.0, 7.0),
            "studio-ref": ("omarchy_speaker_studio_ref", 112, 82.0, 6.0),
            "studio-voice": ("omarchy_speaker_studio_voice", 113, 95.0, 7.5),
        }
        for prof, (node_name, node_id, hp_hz, drive_db) in profile_hp.items():
            for vol_pct in (0, 25, 50, 75, 100, 120):
                expected_db = model.studio_volume_db(vol_pct)
                state = {
                    "studio:Volume (dB)": -12.0,
                    "studio:Drive (dB)": drive_db,
                    "studio:Physical Bass Hz": hp_hz,
                }
                pw_calls: list[tuple[str, ...]] = []

                def fake_run_text(cmd: tuple[str, ...], _state=state, _calls=pw_calls, _id=node_id, _name=node_name) -> str:
                    _calls.append(cmd)
                    if cmd[:2] == ("/usr/bin/pw-dump", _name):
                        return json.dumps([
                            {
                                "id": _id,
                                "info": {
                                    "props": {"node.name": _name, "object.serial": str(_id * 10)},
                                    "params": {
                                        "Props": [
                                            {"params": [x for k, v in _state.items() for x in (k, v)]}
                                        ]
                                    },
                                },
                            }
                        ])
                    if cmd[:4] == ("/usr/bin/pw-cli", "set-param", str(_id), "Props"):
                        # Parse `{ params = [ "studio:Volume (dB)" <val> ] }`
                        payload = cmd[4]
                        self.assertIn('"studio:Volume (dB)"', payload)
                        self.assertNotIn("Drive (dB)", payload)
                        val_str = payload.split('"studio:Volume (dB)"')[1].split("]")[0].strip()
                        _state["studio:Volume (dB)"] = float(val_str)
                        return ""
                    raise AssertionError(f"Unexpected command: {cmd}")

                with (
                    mock.patch.object(runtime, "audio_lock"),
                    mock.patch.object(runtime, "saved_profile", return_value=prof),
                    mock.patch.object(runtime, "hold_engine_route", return_value=node_name),
                    mock.patch.object(runtime, "physical_sink_or_none", return_value=PHYSICAL_SPEAKER),
                    mock.patch.object(runtime, "physical_volume", return_value=vol_pct),
                    mock.patch.object(runtime, "selected_cutoff", return_value=90),
                    mock.patch.object(runtime, "run_text", side_effect=fake_run_text),
                ):
                    result = runtime.apply_profile(None, False)

                self.assertTrue(result.ready)
                self.assertTrue(result.speaker_active)
                self.assertEqual(result.default_sink, node_name)
                self.assertEqual(result.preset, f"studio:{prof}")
                self.assertAlmostEqual(state["studio:Volume (dB)"], expected_db, places=3)
                # Drive (dB) MUST remain untouched at the profile's mastering value
                self.assertEqual(state["studio:Drive (dB)"], drive_db)

    def test_frozen_contour_bug_fixed_when_default_sink_switched_directly(self) -> None:
        """Even if saved_profile is still `warm-deep`, selecting `omarchy_speaker_studio_spatial`
        as default sink in PipeWire drives `studio:Volume (dB)` live on that node!"""
        state = {"studio:Volume (dB)": -12.0, "studio:Drive (dB)": 7.0, "studio:Physical Bass Hz": 82.0}

        def fake_run_text(cmd: tuple[str, ...]) -> str:
            if cmd == ("/usr/bin/pw-dump", "omarchy_speaker_studio_spatial"):
                return json.dumps([
                    {
                        "id": 222,
                        "info": {
                            "props": {"node.name": "omarchy_speaker_studio_spatial", "object.serial": "9222"},
                            "params": {"Props": [{"params": [x for k, v in state.items() for x in (k, v)]}]},
                        },
                    }
                ])
            if cmd[:4] == ("/usr/bin/pw-cli", "set-param", "222", "Props"):
                val = float(cmd[4].split('"studio:Volume (dB)"')[1].split("]")[0].strip())
                state["studio:Volume (dB)"] = val
                return ""
            raise AssertionError(f"Unexpected command: {cmd}")

        with (
            mock.patch.object(runtime, "audio_lock"),
            mock.patch.object(runtime, "saved_profile", return_value="warm-deep"),
            mock.patch.object(runtime, "hold_engine_route", return_value="omarchy_speaker_studio_spatial"),
            mock.patch.object(runtime, "physical_sink_or_none", return_value=PHYSICAL_SPEAKER),
            mock.patch.object(runtime, "physical_volume", return_value=35),
            mock.patch.object(runtime, "selected_cutoff", return_value=90),
            mock.patch.object(runtime, "run_text", side_effect=fake_run_text),
        ):
            result = runtime.apply_profile(None, False)

        self.assertEqual(result.preset, "studio:studio-spatial")
        self.assertAlmostEqual(state["studio:Volume (dB)"], model.studio_volume_db(35), places=3)
        self.assertEqual(state["studio:Drive (dB)"], 7.0)

    def test_zero_regression_warm_deep_engine_path(self) -> None:
        """Existing `warm-deep` on `omarchy_speaker_engine` keeps exact contour/ceiling/deep
        gain-safe two-batch behavior and readback verification."""
        state = {"contour:Add": 0.0, "lim:th": 0.354813, "deep:Add": 0.2818, "hp:f": 180.0}
        set_calls: list[str] = []

        def fake_run_text(cmd: tuple[str, ...]) -> str:
            if cmd == ("/usr/bin/pw-dump", "omarchy_speaker_engine"):
                return json.dumps([
                    {
                        "id": 99,
                        "info": {
                            "props": {"node.name": "omarchy_speaker_engine", "object.serial": "9099"},
                            "params": {"Props": [{"params": [x for k, v in state.items() for x in (k, v)]}]},
                        },
                    }
                ])
            if cmd[:4] == ("/usr/bin/pw-cli", "set-param", "99", "Props"):
                set_calls.append(cmd[4])
                # Apply SPA-JSON updates to state
                inner = cmd[4].split("[", 1)[1].rsplit("]", 1)[0].strip()
                tokens = inner.split()
                for k, v in zip(tokens[::2], tokens[1::2]):
                    state[k.strip('"')] = float(v)
                return ""
            raise AssertionError(f"Unexpected command: {cmd}")

        with (
            mock.patch.object(runtime, "audio_lock"),
            mock.patch.object(runtime, "saved_profile", return_value="warm-deep"),
            mock.patch.object(runtime, "hold_engine_route", return_value="omarchy_speaker_engine"),
            mock.patch.object(runtime, "physical_sink_or_none", return_value=PHYSICAL_SPEAKER),
            mock.patch.object(runtime, "physical_volume", return_value=50),
            mock.patch.object(runtime, "selected_cutoff", return_value=90),
            mock.patch.object(runtime, "run_text", side_effect=fake_run_text),
        ):
            result = runtime.apply_profile(None, False)

        expected = {f"{c.plugin}:{c.name}": float(c.value) for c in model.engine_tracking_changes(50, 1.0)}
        self.assertEqual(result.preset, "engine:warm-deep")
        self.assertTrue(result.ready)
        self.assertTrue(result.speaker_active)
        self.assertTrue(len(set_calls) >= 1)
        for key, val in expected.items():
            self.assertAlmostEqual(state[key], val, places=5)

    def test_hold_engine_route_takes_studio_default_and_moves_stray_streams(self) -> None:
        inv = _full_inventory()
        streams = (
            # App left on old omarchy_speaker_engine after switching to studio-deep -> MUST move
            {"index": 51, "sink": 2, "volume": {"fl": {"value": 65536}}, "properties": {"application.name": "Spotify"}},
            # App stuck on bare speaker -> MUST move
            {"index": 52, "sink": 1, "volume": {"fl": {"value": 65536}}, "properties": {"application.name": "Firefox"}},
            # Studio output stream accidentally raised above 100% -> MUST be held back to 100%
            {"index": 53, "sink": 1, "volume": {"fl": {"value": 80000}}, "mute": False, "properties": {"node.name": "omarchy_speaker_studio_output"}},
        )
        default_state = {"sink": PHYSICAL_SPEAKER}
        cmds: list[tuple[str, ...]] = []

        def fake_run_text(cmd: tuple[str, ...]) -> str:
            cmds.append(cmd)
            if cmd[:2] == ("/usr/bin/pactl", "set-default-sink"):
                default_state["sink"] = cmd[2]
                return ""
            return ""

        with (
            mock.patch.object(runtime, "sink_inventory", return_value=inv),
            mock.patch.object(runtime, "default_sink", side_effect=lambda: default_state["sink"]),
            mock.patch.object(runtime, "configured_default_sink", return_value=PHYSICAL_SPEAKER),
            mock.patch.object(runtime, "saved_profile", return_value="studio-deep"),
            mock.patch.object(runtime, "sink_inputs", return_value=streams),
            mock.patch.object(runtime, "run_text", side_effect=fake_run_text),
        ):
            active = runtime.hold_engine_route(False)

        self.assertEqual(active, "omarchy_speaker_studio")
        self.assertIn(("/usr/bin/pactl", "set-default-sink", "omarchy_speaker_studio"), cmds)
        self.assertIn(("/usr/bin/pactl", "move-sink-input", "51", "@DEFAULT_SINK@"), cmds)
        self.assertIn(("/usr/bin/pactl", "move-sink-input", "52", "@DEFAULT_SINK@"), cmds)
        self.assertIn(("/usr/bin/pactl", "set-sink-input-volume", "53", "100%"), cmds)

    def test_hand_back_to_easy_effects_from_studio_pins_streams_and_restores_speaker(self) -> None:
        inv = _full_inventory()
        streams = (
            {"index": 61, "sink": 4, "properties": {"application.name": "Chromium", "object.id": "901"}},
        )
        cmds: list[tuple[str, ...]] = []
        saved: list[str] = []

        with (
            mock.patch.object(runtime, "audio_lock"),
            mock.patch.object(runtime, "sink_inventory", return_value=inv),
            mock.patch.object(runtime, "sink_inputs", return_value=streams),
            mock.patch.object(runtime, "default_sink", return_value="omarchy_speaker_studio_spatial"),
            mock.patch.object(runtime, "configured_default_sink", return_value="omarchy_speaker_studio_spatial"),
            mock.patch.object(runtime, "save_profile", side_effect=saved.append),
            mock.patch.object(runtime, "run_text", side_effect=lambda c: cmds.append(c) or ""),
        ):
            pinned = runtime.hand_back_to_easy_effects("warm-rich")

        self.assertEqual(pinned, 1)
        self.assertEqual(saved, ["warm-rich"])
        self.assertIn(("/usr/bin/pw-metadata", "-n", "default", "901", "target.object", "302", "Spa:Id"), cmds)
        self.assertIn(("/usr/bin/pactl", "set-default-sink", PHYSICAL_SPEAKER), cmds)

    def test_engine_identity_changes_when_switching_studio_sinks(self) -> None:
        def fake_node(name: str = runtime.ENGINE_NODE):
            serials = {
                "omarchy_speaker_studio": (10, "1001", {}),
                "omarchy_speaker_studio_spatial": (11, "1002", {}),
            }
            return serials[name]

        with (
            mock.patch.object(runtime, "saved_profile", return_value="studio-deep"),
            mock.patch.object(runtime, "engine_node", side_effect=fake_node),
        ):
            with mock.patch.object(runtime, "default_sink", return_value="omarchy_speaker_studio"):
                id1 = runtime.engine_identity()
            with mock.patch.object(runtime, "default_sink", return_value="omarchy_speaker_studio_spatial"):
                id2 = runtime.engine_identity()

        self.assertEqual(id1, "omarchy_speaker_studio:1001")
        self.assertEqual(id2, "omarchy_speaker_studio_spatial:1002")
        self.assertNotEqual(id1, id2)

    def test_read_engine_status_reads_physical_bass_hz_for_studio(self) -> None:
        with (
            mock.patch.object(runtime, "physical_sink", return_value=PHYSICAL_SPEAKER),
            mock.patch.object(runtime, "physical_volume", return_value=60),
            mock.patch.object(runtime, "default_sink", return_value="omarchy_speaker_studio_voice"),
            mock.patch.object(runtime, "saved_profile", return_value="studio-voice"),
            mock.patch.object(runtime, "selected_cutoff", return_value=90),
            mock.patch.object(
                runtime,
                "engine_node",
                return_value=(113, "1130", {"studio:Physical Bass Hz": 95.0, "studio:Volume (dB)": -13.311}),
            ),
        ):
            status = runtime.read_engine_status()

        self.assertEqual(status.preset, "studio:studio-voice")
        self.assertEqual(status.live_cutoff_hz, 95.0)
        self.assertTrue(status.speaker_active)
        self.assertTrue(status.socket_ready)

    def test_repair_easy_effects_default_fixes_stale_studio_sink_not_bluetooth(self) -> None:
        with (
            mock.patch.object(runtime, "run_text") as run_mock,
            mock.patch.object(runtime, "default_sink", return_value=PHYSICAL_SPEAKER),
        ):
            repaired = runtime.repair_easy_effects_default("omarchy_speaker_studio", PHYSICAL_SPEAKER, False)
            self.assertEqual(repaired, PHYSICAL_SPEAKER)
            run_mock.assert_called_once_with(("/usr/bin/pactl", "set-default-sink", PHYSICAL_SPEAKER))

        with mock.patch.object(runtime, "run_text") as run_mock:
            kept = runtime.repair_easy_effects_default(BLUETOOTH_SINK, PHYSICAL_SPEAKER, False)
            self.assertEqual(kept, BLUETOOTH_SINK)
            run_mock.assert_not_called()


class TestStagedArtifactsIntegrity(unittest.TestCase):
    """Offline validation of staged PipeWire conf, unified diffs, bash syntax, and JSONC."""

    def test_pipewire_conf_merges_cleanly_with_pw_config_and_matches_contract(self) -> None:
        self.assertTrue(PIPEWIRE_CONF.is_file())
        if not (LIVE_PIPEWIRE_DIR / "omarchy-speaker-engine.conf").is_file() or shutil.which("pw-config") is None:
            self.skipTest("Host PipeWire base config or pw-config unavailable (CI environment)")
        with tempfile.TemporaryDirectory() as tmp:
            cfg_root = Path(tmp) / "pipewire"
            dropin_dir = cfg_root / "omarchy-speaker-engine.conf.d"
            dropin_dir.mkdir(parents=True)
            shutil.copy2(LIVE_PIPEWIRE_DIR / "omarchy-speaker-engine.conf", cfg_root / "omarchy-speaker-engine.conf")
            for live_dropin in (LIVE_PIPEWIRE_DIR / "omarchy-speaker-engine.conf.d").glob("*.conf"):
                shutil.copy2(live_dropin, dropin_dir / live_dropin.name)
            shutil.copy2(PIPEWIRE_CONF, dropin_dir / "92-xps17-studio.conf")

            env = dict(**{"XDG_CONFIG_HOME": tmp, "PATH": "/usr/bin:/bin"})
            merged_raw = subprocess.check_output(
                ["pw-config", "-N", "-r", "-n", "omarchy-speaker-engine.conf", "merge", "context.modules"],
                env=env,
                text=True,
            )
            modules = json.loads(merged_raw)

        capture_names: list[str] = []
        playback_names: list[str] = []
        studio_mods: dict[str, dict] = {}
        for mod in modules:
            if mod.get("name") != "libpipewire-module-filter-chain":
                continue
            args = mod["args"]
            cn = args["capture.props"]["node.name"]
            pn = args["playback.props"]["node.name"]
            capture_names.append(cn)
            playback_names.append(pn)
            if cn.startswith("omarchy_speaker_studio"):
                studio_mods[cn] = args

        # Zero collisions across 90-warm-deep + 91-spatial-fdtr + 92-xps17-studio
        self.assertEqual(len(capture_names), len(set(capture_names)), f"Duplicate captures: {capture_names}")
        self.assertEqual(len(playback_names), len(set(playback_names)), f"Duplicate playbacks: {playback_names}")

        expected_profiles = {
            "omarchy_speaker_studio": (
                "Laptop Speakers · Studio Deep",
                "xps17-studio-deep.wav",
                {"Mode": 0.0, "Spatial XTC": 0.0, "Bass Depth": 1.0, "Physical Bass Hz": 82.0, "Warmth Body": 1.0, "Stage Depth": 0.85, "Drive (dB)": 7.0, "Ceiling (dBFS)": -1.0, "Excursion Guard": 1.0, "Volume (dB)": -12.0},
            ),
            "omarchy_speaker_studio_spatial": (
                "Laptop Speakers · Studio Spatial",
                "xps17-studio-spatial.wav",
                {"Mode": 1.0, "Spatial XTC": 0.75, "Bass Depth": 1.0, "Physical Bass Hz": 82.0, "Warmth Body": 1.0, "Stage Depth": 0.85, "Drive (dB)": 7.0, "Ceiling (dBFS)": -1.0, "Excursion Guard": 1.0, "Volume (dB)": -12.0},
            ),
            "omarchy_speaker_studio_ref": (
                "Laptop Speakers · Studio Reference",
                "xps17-studio-reference.wav",
                {"Mode": 2.0, "Spatial XTC": 0.0, "Bass Depth": 0.75, "Physical Bass Hz": 82.0, "Warmth Body": 0.65, "Stage Depth": 0.35, "Drive (dB)": 6.0, "Ceiling (dBFS)": -1.0, "Excursion Guard": 1.0, "Volume (dB)": -12.0},
            ),
            "omarchy_speaker_studio_voice": (
                "Laptop Speakers · Studio Voice",
                "xps17-studio-voice.wav",
                {"Mode": 3.0, "Spatial XTC": 0.0, "Bass Depth": 0.55, "Physical Bass Hz": 95.0, "Warmth Body": 0.70, "Stage Depth": 0.25, "Drive (dB)": 7.5, "Ceiling (dBFS)": -1.0, "Excursion Guard": 1.0, "Volume (dB)": -12.0},
            ),
        }
        self.assertEqual(set(studio_mods.keys()), set(expected_profiles.keys()))

        for sink_name, (desc, kernel_file, expected_ctrl) in expected_profiles.items():
            args = studio_mods[sink_name]
            self.assertEqual(args["node.description"], desc)
            pb = args["playback.props"]
            self.assertIs(pb["node.passive"], True)
            self.assertIs(pb["node.dont-move"], True)
            self.assertIs(pb["state.restore-props"], False)
            self.assertIs(pb["node.dont-fallback"], True)
            self.assertIs(pb["node.linger"], True)
            self.assertEqual(pb["target.object"], PHYSICAL_SPEAKER)

            fg = args["filter.graph"]
            nodes = {nd["name"]: nd for nd in fg["nodes"]}
            # Zero double-convolution invariant: libxps17_studio_dsp.so runs Lead 2's
            # 4096-tap minimum-phase FIR internally in Stage 4, so PipeWire MUST NOT
            # add duplicate `fir_l`/`fir_r` convolver nodes in series.
            self.assertEqual(set(nodes.keys()), {"in_l", "in_r", "studio"})
            self.assertNotIn("~", nodes["studio"]["plugin"])
            self.assertEqual(nodes["studio"]["type"], "ladspa")
            self.assertEqual(nodes["studio"]["label"], "xps17_studio_dsp")
            self.assertEqual(set(nodes["studio"]["control"].keys()), EXPECTED_STUDIO_CONTROLS)
            for k, v in expected_ctrl.items():
                self.assertAlmostEqual(float(nodes["studio"]["control"][k]), v, places=5, msg=f"{sink_name}:{k}")

            # Verify DAG is acyclic AND `in_l`/`in_r` feed `studio` directly
            adj: dict[str, list[str]] = defaultdict(list)
            indeg = {n: 0 for n in nodes}
            for link in fg["links"]:
                src = link["output"].split(":", 1)[0]
                dst = link["input"].split(":", 1)[0]
                adj[src].append(dst)
                indeg[dst] += 1
            order: list[str] = []
            q = deque(n for n, d in indeg.items() if d == 0)
            while q:
                u = q.popleft()
                order.append(u)
                for v in adj[u]:
                    indeg[v] -= 1
                    if indeg[v] == 0:
                        q.append(v)
            self.assertEqual(len(order), len(nodes))
            self.assertLess(order.index("in_l"), order.index("studio"))
            self.assertLess(order.index("in_r"), order.index("studio"))
            self.assertEqual(fg["outputs"], ["studio:Out L", "studio:Out R"])

    def test_speaker_profile_bash_syntax_and_diff_applies_cleanly(self) -> None:
        subprocess.run(["bash", "-n", str(SPEAKER_PROFILE_BIN)], check=True)
        if not LIVE_SPEAKER_PROFILE.is_file() or not SPEAKER_PROFILE_DIFF.is_file():
            return
        # Once deployed live, LIVE_SPEAKER_PROFILE already equals SPEAKER_PROFILE_BIN
        if LIVE_SPEAKER_PROFILE.read_text(encoding="utf-8") == SPEAKER_PROFILE_BIN.read_text(encoding="utf-8"):
            return
        with tempfile.TemporaryDirectory() as tmp:
            copy_bin = Path(tmp) / "speaker-profile"
            shutil.copy2(LIVE_SPEAKER_PROFILE, copy_bin)
            subprocess.run(
                ["patch", "-p1", "-d", tmp, "-i", str(SPEAKER_PROFILE_DIFF)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(copy_bin.read_text(encoding="utf-8"), SPEAKER_PROFILE_BIN.read_text(encoding="utf-8"))

    def test_omarchy_audio_patch_applies_cleanly_to_live_originals(self) -> None:
        if not (LIVE_OMARCHY_AUDIO / "model.py").is_file() or not AUDIO_PATCH_DIFF.is_file():
            return
        if (LIVE_OMARCHY_AUDIO / "model.py").read_text(encoding="utf-8") == (CONTROLLER_DIR / "model.py").read_text(encoding="utf-8"):
            return
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy2(LIVE_OMARCHY_AUDIO / "model.py", Path(tmp) / "model.py")
            shutil.copy2(LIVE_OMARCHY_AUDIO / "runtime.py", Path(tmp) / "runtime.py")
            subprocess.run(
                ["patch", "-p1", "-d", tmp, "-i", str(AUDIO_PATCH_DIFF)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                (Path(tmp) / "model.py").read_text(encoding="utf-8"),
                (CONTROLLER_DIR / "model.py").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                (Path(tmp) / "runtime.py").read_text(encoding="utf-8"),
                (CONTROLLER_DIR / "runtime.py").read_text(encoding="utf-8"),
            )

    def test_menu_jsonc_snippet_and_diff_apply_and_parse(self) -> None:
        snippet = json.loads(_strip_jsonc_comments(MENU_SNIPPET.read_text(encoding="utf-8")))
        for key in (
            "setup.speakers",
            "setup.speakers.studio-deep",
            "setup.speakers.studio-spatial",
            "setup.speakers.studio-ref",
            "setup.speakers.studio-voice",
        ):
            self.assertIn(key, snippet)

        if not LIVE_MENU_JSONC.is_file() or not MENU_DIFF.is_file():
            return
        live_merged = json.loads(_strip_jsonc_comments(LIVE_MENU_JSONC.read_text(encoding="utf-8")))
        if all(live_merged.get(k) == row for k, row in snippet.items()):
            return
        with tempfile.TemporaryDirectory() as tmp:
            copy_menu = Path(tmp) / "omarchy-menu.jsonc"
            shutil.copy2(LIVE_MENU_JSONC, copy_menu)
            subprocess.run(
                ["patch", "-p1", "-d", tmp, "-i", str(MENU_DIFF)],
                check=True,
                capture_output=True,
                text=True,
            )
            merged = json.loads(_strip_jsonc_comments(copy_menu.read_text(encoding="utf-8")))
            for key, row in snippet.items():
                self.assertEqual(merged[key], row)


if __name__ == "__main__":
    unittest.main()

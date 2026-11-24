#!/usr/bin/env python3
"""
Safety, Mic-Privacy, Hardware-Protection & Thermal Auditor
Dell XPS 17 9720 Studio Audio Engineering Stack

Strictly READ-ONLY against live system files and hardware.
NEVER reads /sys/kernel/debug/regmap/*/access or /sys/kernel/debug/regmap/*/range.
NEVER captures audio from any microphone or plays audio to any speaker.

Pure Python stdlib + ctypes.
"""

from __future__ import annotations

import argparse
import ast
import cmath
import ctypes
import hashlib
import json
import math
import os
import re
import struct
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Contracts from COORDINATOR-NOTES.md (Section 1, 2, 3)
# ---------------------------------------------------------------------------

EXPECTED_STUDIO_SINKS: tuple[str, ...] = (
    "omarchy_speaker_studio",
    "omarchy_speaker_studio_spatial",
    "omarchy_speaker_studio_ref",
    "omarchy_speaker_studio_voice",
)

EXPECTED_LADSPA_LABEL = "xps17_studio_dsp"
EXPECTED_LADSPA_UNIQUE_ID = 597201

EXPECTED_AUDIO_INPUT_PORTS: tuple[str, ...] = ("In L", "In R")
EXPECTED_AUDIO_OUTPUT_PORTS: tuple[str, ...] = ("Out L", "Out R")

# Exact 10 control port names & ranges from COORDINATOR-NOTES.md Section 3
EXPECTED_CONTROL_PORTS: tuple[dict[str, Any], ...] = (
    {"name": "Mode",             "min": 0.0,   "max": 3.0,   "default": 0.0},
    {"name": "Volume (dB)",      "min": -45.0, "max": 0.0,   "default": -12.0},
    {"name": "Bass Depth",       "min": 0.0,   "max": 1.5,   "default": 1.0},
    {"name": "Physical Bass Hz", "min": 65.0,  "max": 180.0, "default": 82.0},
    {"name": "Warmth Body",      "min": 0.0,   "max": 1.5,   "default": 1.0},
    {"name": "Stage Depth",      "min": 0.0,   "max": 1.5,   "default": 0.85},
    {"name": "Spatial XTC",      "min": 0.0,   "max": 1.0,   "default": 0.0},
    {"name": "Drive (dB)",       "min": 0.0,   "max": 12.0,  "default": 7.0},
    {"name": "Ceiling (dBFS)",   "min": -9.0,  "max": -0.3,  "default": -1.0},
    {"name": "Excursion Guard",  "min": 0.0,   "max": 1.0,   "default": 1.0},
)
EXPECTED_CONTROL_PORT_NAMES: tuple[str, ...] = tuple(p["name"] for p in EXPECTED_CONTROL_PORTS)
EXPECTED_CONTROL_PORT_MAP: dict[str, dict[str, Any]] = {p["name"]: p for p in EXPECTED_CONTROL_PORTS}

EXPECTED_KERNELS: tuple[str, ...] = (
    "xps17-studio-deep.wav",
    "xps17-studio-spatial.wav",
    "xps17-studio-reference.wav",
    "xps17-studio-voice.wav",
)

FORBIDDEN_ALC1316_REGS: dict[int, str] = {
    0xC000: "ALC1316 protected base control",
    0xC050: "DC detect protection",
    0xC082: "Internal sine generator",
    0xC093: "ALC1316 protected register",
    0xC09C: "ALC1316 protected register",
    0xC09D: "ALC1316 protected register",
    0xC0A3: "ALC1316 protected register",
    0xC102: "HV mute / power stage protection",
    0xC60E: "HV_POW_SPK_PRO thermal/speaker protection",
}

HIGH_GAIN_GATED_REGS: dict[int, int] = {
    0xC60F: 0x05,  # +9.0 dB HV stage gain (must require deadman gate)
    0xD002: 0xA9,  # High digital gain L (must require deadman gate)
    0xD004: 0xA9,  # High digital gain R (must require deadman gate)
}

# LADSPA v1.1 flags
LADSPA_PORT_INPUT = 0x1
LADSPA_PORT_OUTPUT = 0x2
LADSPA_PORT_CONTROL = 0x4
LADSPA_PORT_AUDIO = 0x8
LADSPA_PROPERTY_REALTIME = 0x1
LADSPA_PROPERTY_INPLACE_BROKEN = 0x2
LADSPA_PROPERTY_HARD_RT_CAPABLE = 0x4
LADSPA_HINT_BOUNDED_BELOW = 0x1
LADSPA_HINT_BOUNDED_ABOVE = 0x2

# Thermal & RT CPU thresholds
TEMP_WARN_C = 78.0
TEMP_FAIL_C = 82.0
CPU_WARN_PCT = 1.0
CPU_FAIL_PCT = 1.5

# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

VALID_STATUSES = ("PASS", "WARN", "FAIL", "PENDING")


def make_check(check_id: str, name: str, status: str, message: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    assert status in VALID_STATUSES, f"Invalid status {status}"
    return {
        "id": check_id,
        "name": name,
        "status": status,
        "message": message,
        "evidence": evidence or {},
    }


def summarize_checks(checks: list[dict[str, Any]]) -> tuple[str, dict[str, int]]:
    counts = {s: 0 for s in VALID_STATUSES}
    for c in checks:
        counts[c["status"]] += 1
    if counts["FAIL"] > 0:
        overall = "FAIL"
    elif counts["PENDING"] > 0:
        overall = "PENDING"
    elif counts["WARN"] > 0:
        overall = "WARN"
    else:
        overall = "PASS"
    return overall, counts


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# SPA-JSON / PipeWire .conf Lexer & Parser (pure stdlib)
# ---------------------------------------------------------------------------

class SpaJsonError(ValueError):
    def __init__(self, msg: str, line: int = 0, col: int = 0):
        super().__init__(f"Line {line}, Col {col}: {msg}" if line else msg)
        self.line = line
        self.col = col


class SpaJsonParser:
    """
    Strict SPA-JSON / PipeWire .conf parser.
    Validates balanced quotes, braces {}, brackets [], and builds Python dicts/lists.
    """

    def __init__(self, text: str):
        self.text = text
        self.tokens: list[tuple[str, Any, int, int]] = []
        self.pos = 0
        self._tokenize()

    def _tokenize(self) -> None:
        s = self.text
        n = len(s)
        i = 0
        line = 1
        col = 1
        brace_stack: list[tuple[str, int, int]] = []

        while i < n:
            ch = s[i]
            # Whitespace
            if ch in " \t\r":
                i += 1
                col += 1
                continue
            if ch == "\n":
                i += 1
                line += 1
                col = 1
                continue
            # Line comments: # ... (PipeWire SPA-JSON)
            if ch == "#":
                while i < n and s[i] != "\n":
                    i += 1
                    col += 1
                continue
            # Punctuation
            if ch in "{}[]=:,":
                if ch in "{[":
                    brace_stack.append((ch, line, col))
                elif ch in "}]":
                    expected = "{" if ch == "}" else "["
                    if not brace_stack:
                        raise SpaJsonError(f"Unmatched closing '{ch}'", line, col)
                    open_ch, open_l, open_c = brace_stack.pop()
                    if open_ch != expected:
                        raise SpaJsonError(
                            f"Mismatched closing '{ch}' for '{open_ch}' opened at line {open_l}, col {open_c}",
                            line,
                            col,
                        )
                self.tokens.append((ch, ch, line, col))
                i += 1
                col += 1
                continue
            # Double-quoted string
            if ch == '"':
                start_l, start_c = line, col
                i += 1
                col += 1
                buf: list[str] = []
                closed = False
                while i < n:
                    c = s[i]
                    if c == "\\":
                        if i + 1 >= n:
                            raise SpaJsonError("Unterminated escape in string", line, col)
                        nxt = s[i + 1]
                        esc_map = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}
                        buf.append(esc_map.get(nxt, nxt))
                        i += 2
                        col += 2
                        continue
                    if c == '"':
                        i += 1
                        col += 1
                        closed = True
                        break
                    if c == "\n":
                        line += 1
                        col = 1
                    else:
                        col += 1
                    buf.append(c)
                    i += 1
                if not closed:
                    raise SpaJsonError("Unterminated double-quoted string", start_l, start_c)
                self.tokens.append(("STRING", "".join(buf), start_l, start_c))
                continue

            # Bareword / number / boolean / null
            start_l, start_c = line, col
            start = i
            while i < n and s[i] not in " \t\r\n{}[]=:,#\"":
                i += 1
                col += 1
            raw = s[start:i]
            self.tokens.append(("ATOM", raw, start_l, start_c))

        if brace_stack:
            open_ch, open_l, open_c = brace_stack[-1]
            raise SpaJsonError(f"Unclosed '{open_ch}' opened at line {open_l}, col {open_c}", open_l, open_c)

    def _peek(self) -> tuple[str, Any, int, int] | None:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def _consume(self, expected_kind: str | None = None) -> tuple[str, Any, int, int]:
        tok = self._peek()
        if tok is None:
            raise SpaJsonError(f"Unexpected EOF (expected {expected_kind or 'token'})")
        if expected_kind is not None and tok[0] != expected_kind:
            raise SpaJsonError(f"Expected '{expected_kind}', got {tok[0]} ({tok[1]!r})", tok[2], tok[3])
        self.pos += 1
        return tok

    @staticmethod
    def _coerce_atom(raw: str) -> Any:
        if raw == "true":
            return True
        if raw == "false":
            return False
        if raw == "null":
            return None
        if re.fullmatch(r"[+-]?(?:0|[1-9][0-9]*)", raw):
            try:
                return int(raw, 10)
            except ValueError:
                pass
        if re.fullmatch(r"0[xX][0-9a-fA-F]+", raw):
            try:
                return int(raw, 16)
            except ValueError:
                pass
        if re.fullmatch(r"[+-]?(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)(?:[eE][+-]?[0-9]+)?", raw):
            try:
                return float(raw)
            except ValueError:
                pass
        return raw

    def parse_document(self) -> dict[str, Any]:
        tok = self._peek()
        if tok is not None and tok[0] == "{":
            obj = self._parse_object()
            if self._peek() is not None:
                rem = self._peek()
                raise SpaJsonError(f"Trailing token {rem[1]!r}", rem[2], rem[3])
            return obj
        # Implicit top-level object
        return self._parse_object_body(stop_at_rbrace=False)

    def _parse_object(self) -> dict[str, Any]:
        self._consume("{")
        obj = self._parse_object_body(stop_at_rbrace=True)
        self._consume("}")
        return obj

    def _parse_object_body(self, stop_at_rbrace: bool) -> dict[str, Any]:
        result: dict[str, Any] = {}
        while True:
            tok = self._peek()
            if tok is None:
                if stop_at_rbrace:
                    raise SpaJsonError("Unexpected EOF inside object")
                break
            if tok[0] == ",":
                self._consume(",")
                continue
            if stop_at_rbrace and tok[0] == "}":
                break
            if tok[0] not in ("STRING", "ATOM"):
                raise SpaJsonError(f"Expected object key, got {tok[0]} ({tok[1]!r})", tok[2], tok[3])
            _, key, kl, kc = self._consume()
            key_str = str(key)
            sep = self._peek()
            if sep is None or sep[0] not in ("=", ":"):
                raise SpaJsonError(
                    f"Expected '=' or ':' after key {key_str!r}",
                    sep[2] if sep else kl,
                    sep[3] if sep else kc,
                )
            self._consume()
            val = self._parse_value()
            if key_str in result and isinstance(result[key_str], list) and isinstance(val, list):
                result[key_str].extend(val)
            else:
                result[key_str] = val
        return result

    def _parse_array(self) -> list[Any]:
        self._consume("[")
        items: list[Any] = []
        while True:
            tok = self._peek()
            if tok is None:
                raise SpaJsonError("Unexpected EOF inside array")
            if tok[0] == ",":
                self._consume(",")
                continue
            if tok[0] == "]":
                break
            items.append(self._parse_value())
        self._consume("]")
        return items

    def _parse_value(self) -> Any:
        tok = self._peek()
        if tok is None:
            raise SpaJsonError("Unexpected EOF expecting value")
        kind, val, l, c = tok
        if kind == "{":
            return self._parse_object()
        if kind == "[":
            return self._parse_array()
        if kind == "STRING":
            self._consume("STRING")
            return val
        if kind == "ATOM":
            self._consume("ATOM")
            return self._coerce_atom(val)
        raise SpaJsonError(f"Unexpected token {kind} ({val!r}) where value expected", l, c)


def parse_spa_json(text: str) -> dict[str, Any]:
    return SpaJsonParser(text).parse_document()


# ---------------------------------------------------------------------------
# LADSPA v1.1 ctypes ABI definitions (LP64 x86_64 Linux)
# ---------------------------------------------------------------------------

class LADSPA_PortRangeHint(ctypes.Structure):
    _fields_ = [
        ("HintDescriptor", ctypes.c_int),
        ("LowerBound", ctypes.c_float),
        ("UpperBound", ctypes.c_float),
    ]


class LADSPA_Descriptor(ctypes.Structure):
    pass


LADSPA_INSTANTIATE_FN = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.POINTER(LADSPA_Descriptor), ctypes.c_ulong)
LADSPA_CONNECT_PORT_FN = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_float))
LADSPA_ACTIVATE_FN = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
LADSPA_RUN_FN = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_ulong)
LADSPA_SET_GAIN_FN = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_float)

LADSPA_Descriptor._fields_ = [
    ("UniqueID", ctypes.c_ulong),
    ("Label", ctypes.c_char_p),
    ("Properties", ctypes.c_int),
    ("Name", ctypes.c_char_p),
    ("Maker", ctypes.c_char_p),
    ("Copyright", ctypes.c_char_p),
    ("PortCount", ctypes.c_ulong),
    ("PortDescriptors", ctypes.POINTER(ctypes.c_int)),
    ("PortNames", ctypes.POINTER(ctypes.c_char_p)),
    ("PortRangeHints", ctypes.POINTER(LADSPA_PortRangeHint)),
    ("ImplementationData", ctypes.c_void_p),
    ("instantiate", LADSPA_INSTANTIATE_FN),
    ("connect_port", LADSPA_CONNECT_PORT_FN),
    ("activate", LADSPA_ACTIVATE_FN),
    ("run", LADSPA_RUN_FN),
    ("run_adding", LADSPA_RUN_FN),
    ("set_run_adding_gain", LADSPA_SET_GAIN_FN),
    ("deactivate", LADSPA_ACTIVATE_FN),
    ("cleanup", LADSPA_ACTIVATE_FN),
]


# ---------------------------------------------------------------------------
# Pure-Python IEEE Float32 WAV Parser + Radix-2 FFT
# ---------------------------------------------------------------------------

IEEE_FLOAT_GUID = b"\x03\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"


def read_float32_wav(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if len(raw) < 12 or raw[0:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError("Not a valid RIFF/WAVE file")
    pos = 12
    fmt_info: dict[str, Any] | None = None
    data_bytes: bytes | None = None

    while pos + 8 <= len(raw):
        chunk_id = raw[pos:pos + 4]
        chunk_size = struct.unpack_from("<I", raw, pos + 4)[0]
        chunk_start = pos + 8
        chunk_end = chunk_start + chunk_size
        if chunk_end > len(raw):
            raise ValueError(f"Truncated chunk {chunk_id!r}")
        if chunk_id == b"fmt ":
            if chunk_size < 16:
                raise ValueError("fmt chunk too short")
            audio_fmt, channels, sample_rate, byte_rate, block_align, bits_per_sample = struct.unpack_from(
                "<HHIIHH", raw, chunk_start
            )
            is_ieee_float = (audio_fmt == 3)
            if audio_fmt == 0xFFFE and chunk_size >= 40:
                subformat_guid = raw[chunk_start + 24:chunk_start + 40]
                is_ieee_float = (subformat_guid == IEEE_FLOAT_GUID)
            fmt_info = {
                "audio_format": audio_fmt,
                "is_ieee_float": is_ieee_float,
                "channels": channels,
                "sample_rate": sample_rate,
                "byte_rate": byte_rate,
                "block_align": block_align,
                "bits_per_sample": bits_per_sample,
            }
        elif chunk_id == b"data":
            data_bytes = raw[chunk_start:chunk_end]
        pos = chunk_end + (chunk_size & 1)

    if fmt_info is None or data_bytes is None:
        raise ValueError("Missing fmt or data chunk")
    if not fmt_info["is_ieee_float"] or fmt_info["bits_per_sample"] != 32:
        raise ValueError(
            f"Expected 32-bit IEEE float WAV, got format={fmt_info['audio_format']}, "
            f"bits={fmt_info['bits_per_sample']}, ieee_float={fmt_info['is_ieee_float']}"
        )
    channels = fmt_info["channels"]
    if channels <= 0 or len(data_bytes) % (4 * channels) != 0:
        raise ValueError("Data chunk size not aligned to float32 channel frames")
    total_floats = len(data_bytes) // 4
    samples = struct.unpack(f"<{total_floats}f", data_bytes)
    num_frames = total_floats // channels
    channel_data = [list(samples[ch::channels]) for ch in range(channels)]
    return {**fmt_info, "num_frames": num_frames, "channels_data": channel_data}


def radix2_fft(x: list[float]) -> list[complex]:
    """Iterative Cooley-Tukey radix-2 FFT (power-of-2 length) in pure Python."""
    n = len(x)
    if n == 0 or (n & (n - 1)) != 0:
        raise ValueError("FFT length must be power of 2")
    a = [complex(v, 0.0) for v in x]
    # Bit-reversal permutation
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j ^= bit
        if i < j:
            a[i], a[j] = a[j], a[i]
    length = 2
    while length <= n:
        half = length >> 1
        ang = -2.0 * math.pi / length
        wlen = complex(math.cos(ang), math.sin(ang))
        for i in range(0, n, length):
            w = 1.0 + 0.0j
            for k in range(half):
                u = a[i + k]
                v = a[i + k + half] * w
                a[i + k] = u + v
                a[i + k + half] = u - v
                w *= wlen
        length <<= 1
    return a


# ---------------------------------------------------------------------------
# Check Group 1 — PipeWire Graph & Config Validator
# ---------------------------------------------------------------------------

def run_group1_pipewire_graph(workspace: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    extracted_context: dict[str, Any] = {
        "conf_parsed": False,
        "sink_names": [],
        "ladspa_controls_by_sink": {},
        "drive_db_by_sink": {},
        "ceiling_db_by_sink": {},
    }

    conf_path = workspace / "verify-integrate" / "staged" / "pipewire" / "92-xps17-studio.conf"
    if not conf_path.exists():
        checks.append(
            make_check(
                "G1.1_conf_presence",
                "92-xps17-studio.conf Presence & Syntax",
                "PENDING",
                f"Staged PipeWire config not yet present at {conf_path} (awaiting pipewire-controller-integrator).",
                {"path": str(conf_path)},
            )
        )
        status, counts = summarize_checks(checks)
        return (
            {"title": "Check Group 1 — PipeWire Graph & Config Validator", "status": status, "counts": counts, "checks": checks},
            extracted_context,
        )

    raw_text = conf_path.read_text(encoding="utf-8", errors="replace")
    try:
        doc = parse_spa_json(raw_text)
        extracted_context["conf_parsed"] = True
        checks.append(
            make_check(
                "G1.1_spa_json_syntax",
                "SPA-JSON Balanced Braces/Brackets/Quotes & Parse",
                "PASS",
                "92-xps17-studio.conf parsed cleanly with balanced braces, brackets, and quotes.",
                {"path": str(conf_path), "bytes": len(raw_text.encode("utf-8"))},
            )
        )
    except SpaJsonError as e:
        checks.append(
            make_check(
                "G1.1_spa_json_syntax",
                "SPA-JSON Balanced Braces/Brackets/Quotes & Parse",
                "FAIL",
                f"SPA-JSON parse error in 92-xps17-studio.conf: {e}",
                {"path": str(conf_path), "line": e.line, "col": e.col},
            )
        )
        status, counts = summarize_checks(checks)
        return (
            {"title": "Check Group 1 — PipeWire Graph & Config Validator", "status": status, "counts": counts, "checks": checks},
            extracted_context,
        )

    modules = doc.get("context.modules")
    if not isinstance(modules, list) or not modules:
        checks.append(
            make_check(
                "G1.2_context_modules",
                "context.modules Structure",
                "FAIL",
                "Top-level 'context.modules' must be a non-empty list.",
                {"top_level_keys": list(doc.keys())},
            )
        )
        status, counts = summarize_checks(checks)
        return (
            {"title": "Check Group 1 — PipeWire Graph & Config Validator", "status": status, "counts": counts, "checks": checks},
            extracted_context,
        )

    filter_chain_mods = [m for m in modules if isinstance(m, dict) and m.get("name") == "libpipewire-module-filter-chain"]
    checks.append(
        make_check(
            "G1.2_context_modules",
            "context.modules Structure",
            "PASS" if len(filter_chain_mods) >= len(EXPECTED_STUDIO_SINKS) else "FAIL",
            f"Found {len(filter_chain_mods)} libpipewire-module-filter-chain module(s) in context.modules.",
            {"total_modules": len(modules), "filter_chain_count": len(filter_chain_mods)},
        )
    )

    found_sinks: dict[str, dict[str, Any]] = {}
    passive_failures: list[str] = []
    mic_capture_violations: list[str] = []
    dag_failures: list[str] = []
    port_ref_failures: list[str] = []
    graph_summaries: dict[str, Any] = {}

    for idx, mod in enumerate(filter_chain_mods):
        args = mod.get("args", {})
        if not isinstance(args, dict):
            dag_failures.append(f"module[{idx}]: missing/invalid args object")
            continue
        cap_props = args.get("capture.props", {})
        play_props = args.get("playback.props", {})
        sink_name = cap_props.get("node.name") if isinstance(cap_props, dict) else None
        sink_label = str(sink_name or f"module[{idx}]")
        if sink_name:
            found_sinks[str(sink_name)] = mod

        # 1.3 Passive playback stream (zero idle CPU)
        if not isinstance(play_props, dict) or play_props.get("node.passive") is not True:
            passive_failures.append(f"{sink_label}: playback.props.node.passive != true (got {play_props.get('node.passive') if isinstance(play_props, dict) else None!r})")

        # 1.4 No unintended microphone / capture source capture
        media_class = str(cap_props.get("media.class", "")) if isinstance(cap_props, dict) else ""
        if media_class != "Audio/Sink":
            mic_capture_violations.append(f"{sink_label}: capture.props.media.class is {media_class!r}, expected 'Audio/Sink'")
        for prop_block_name, prop_block in (("capture.props", cap_props), ("playback.props", play_props)):
            if not isinstance(prop_block, dict):
                continue
            for pk, pv in prop_block.items():
                pvs = str(pv)
                if "Source" in pvs or "alsa_input" in pvs or "Mic" in pvs or "Capture" in pvs:
                    mic_capture_violations.append(f"{sink_label}: {prop_block_name}.{pk}={pvs!r} references capture/source/mic")
                if pk == "stream.capture.sink" and pv is True:
                    mic_capture_violations.append(f"{sink_label}: {prop_block_name}.stream.capture.sink=true (loopback capture)")

        # 1.5 Graph DAG acyclicity & port reference validation
        fg = args.get("filter.graph", {})
        if not isinstance(fg, dict):
            dag_failures.append(f"{sink_label}: missing filter.graph")
            continue
        nodes = fg.get("nodes", [])
        links = fg.get("links", [])
        inputs = fg.get("inputs", [])
        outputs = fg.get("outputs", [])

        node_map: dict[str, dict[str, Any]] = {}
        dup_nodes: list[str] = []
        for nspec in nodes if isinstance(nodes, list) else []:
            if not isinstance(nspec, dict) or "name" not in nspec:
                port_ref_failures.append(f"{sink_label}: malformed node entry without name")
                continue
            nname = str(nspec["name"])
            if nname in node_map:
                dup_nodes.append(nname)
            node_map[nname] = nspec
            # Capture LADSPA studio DSP controls for cross-contract check
            if nspec.get("type") == "ladspa" and nspec.get("label") == EXPECTED_LADSPA_LABEL:
                ctrl = nspec.get("control", {})
                if isinstance(ctrl, dict):
                    extracted_context["ladspa_controls_by_sink"][sink_label] = list(ctrl.keys())
                    if "Drive (dB)" in ctrl:
                        extracted_context["drive_db_by_sink"][sink_label] = ctrl["Drive (dB)"]
                    if "Ceiling (dBFS)" in ctrl:
                        extracted_context["ceiling_db_by_sink"][sink_label] = ctrl["Ceiling (dBFS)"]

        if dup_nodes:
            port_ref_failures.append(f"{sink_label}: duplicate node names {dup_nodes}")

        adj: dict[str, set[str]] = {n: set() for n in node_map}
        indeg: dict[str, int] = {n: 0 for n in node_map}

        def _check_port_ref(ref: Any, direction: str, ctx: str) -> str | None:
            if not isinstance(ref, str) or ":" not in ref:
                port_ref_failures.append(f"{sink_label}: invalid port reference {ref!r} in {ctx}")
                return None
            nname, pname = ref.split(":", 1)
            if nname not in node_map:
                port_ref_failures.append(f"{sink_label}: {ctx} references unknown node {nname!r} ({ref!r})")
                return None
            nspec = node_map[nname]
            ntype = nspec.get("type")
            nlabel = nspec.get("label")
            if ntype == "ladspa" and nlabel == EXPECTED_LADSPA_LABEL:
                allowed = EXPECTED_AUDIO_OUTPUT_PORTS if direction == "out" else EXPECTED_AUDIO_INPUT_PORTS
                if pname not in allowed:
                    port_ref_failures.append(
                        f"{sink_label}: {ctx} references invalid {EXPECTED_LADSPA_LABEL} {direction} port {pname!r} (expected one of {allowed})"
                    )
            elif ntype == "builtin":
                if nlabel in ("copy", "convolver", "delay"):
                    expected_p = "Out" if direction == "out" else "In"
                    if pname != expected_p:
                        port_ref_failures.append(
                            f"{sink_label}: {ctx} references invalid builtin {nlabel} {direction} port {pname!r} (expected {expected_p!r})"
                        )
                elif nlabel == "mixer":
                    if direction == "out" and pname != "Out":
                        port_ref_failures.append(f"{sink_label}: {ctx} invalid mixer out port {pname!r}")
                    elif direction == "in" and not re.fullmatch(r"In [1-8]", pname):
                        port_ref_failures.append(f"{sink_label}: {ctx} invalid mixer in port {pname!r}")
            return nname

        for lidx, link in enumerate(links if isinstance(links, list) else []):
            if not isinstance(link, dict) or "output" not in link or "input" not in link:
                port_ref_failures.append(f"{sink_label}: malformed link[{lidx}]")
                continue
            src_n = _check_port_ref(link["output"], "out", f"link[{lidx}].output")
            dst_n = _check_port_ref(link["input"], "in", f"link[{lidx}].input")
            if src_n and dst_n:
                if src_n == dst_n:
                    dag_failures.append(f"{sink_label}: self-loop on node {src_n!r}")
                elif dst_n not in adj[src_n]:
                    adj[src_n].add(dst_n)
                    indeg[dst_n] += 1

        if not isinstance(inputs, list) or len(inputs) == 0:
            port_ref_failures.append(f"{sink_label}: filter.graph.inputs is empty")
        else:
            for ref in inputs:
                _check_port_ref(ref, "in", "filter.graph.inputs")

        if not isinstance(outputs, list) or len(outputs) == 0:
            port_ref_failures.append(f"{sink_label}: filter.graph.outputs is empty")
        else:
            for ref in outputs:
                _check_port_ref(ref, "out", "filter.graph.outputs")

        # Kahn's topological sort for DAG acyclicity
        q: deque[str] = deque([n for n, d in indeg.items() if d == 0])
        visited = 0
        topo: list[str] = []
        while q:
            u = q.popleft()
            visited += 1
            topo.append(u)
            for v in adj[u]:
                indeg[v] -= 1
                if indeg[v] == 0:
                    q.append(v)
        if visited != len(node_map):
            cyclic = sorted([n for n, d in indeg.items() if d > 0])
            dag_failures.append(f"{sink_label}: cycle detected involving nodes {cyclic}")

        graph_summaries[sink_label] = {
            "node_count": len(node_map),
            "link_count": len(links) if isinstance(links, list) else 0,
            "inputs": inputs,
            "outputs": outputs,
            "topological_order": topo,
        }

    extracted_context["sink_names"] = sorted(found_sinks.keys())
    missing_sinks = [s for s in EXPECTED_STUDIO_SINKS if s not in found_sinks]
    checks.append(
        make_check(
            "G1.3_expected_sinks",
            "All 4 Studio Sink Nodes Declared",
            "PASS" if not missing_sinks else "FAIL",
            "All 4 expected Studio sink nodes present."
            if not missing_sinks
            else f"Missing expected Studio sink nodes: {missing_sinks}",
            {"expected": list(EXPECTED_STUDIO_SINKS), "found": sorted(found_sinks.keys()), "missing": missing_sinks},
        )
    )

    checks.append(
        make_check(
            "G1.4_passive_playback",
            "Zero Idle CPU (playback.props.node.passive = true)",
            "PASS" if not passive_failures else "FAIL",
            "All filter-chain playback streams set node.passive = true (zero idle CPU)."
            if not passive_failures
            else f"Passive stream check failed: {'; '.join(passive_failures)}",
            {"failures": passive_failures},
        )
    )

    checks.append(
        make_check(
            "G1.5_no_mic_capture",
            "No Unintended Microphone / Capture Source Capture",
            "PASS" if not mic_capture_violations else "FAIL",
            "All modules strictly Audio/Sink playback graphs with zero capture/mic references."
            if not mic_capture_violations
            else f"Capture/mic privacy violations: {'; '.join(mic_capture_violations)}",
            {"violations": mic_capture_violations},
        )
    )

    checks.append(
        make_check(
            "G1.6_dag_acyclicity",
            "Filter Graph DAG Acyclicity (Topological Sort)",
            "PASS" if not dag_failures else "FAIL",
            "All filter-chain graphs verified acyclic (strict DAGs)."
            if not dag_failures
            else f"DAG cycle/structure failures: {'; '.join(dag_failures)}",
            {"failures": dag_failures, "graphs": graph_summaries},
        )
    )

    checks.append(
        make_check(
            "G1.7_port_references",
            "Valid Node Input/Output Port References",
            "PASS" if not port_ref_failures else "FAIL",
            "All link, input, and output port references resolve cleanly to declared nodes and valid ports."
            if not port_ref_failures
            else f"Port reference errors: {'; '.join(port_ref_failures)}",
            {"failures": port_ref_failures},
        )
    )

    status, counts = summarize_checks(checks)
    return (
        {"title": "Check Group 1 — PipeWire Graph & Config Validator", "status": status, "counts": counts, "checks": checks},
        extracted_context,
    )


# ---------------------------------------------------------------------------
# Check Group 2 — LADSPA ABI & 10-Port Contract Parity + FIR Kernels
# ---------------------------------------------------------------------------

def _extract_controller_control_refs(py_path: Path) -> dict[str, Any]:
    """
    Parse a staged Python controller file (runtime.py or model.py) with AST
    to find references to Studio DSP node:control parameters and sink names.
    """
    src = py_path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src, filename=str(py_path))
    known_controls_lower = {c.lower(): c for c in EXPECTED_CONTROL_PORT_NAMES}
    referenced_controls: set[str] = set()
    suspicious_controls: list[str] = []
    referenced_sinks: set[str] = set()

    # Check string literals for sink names, "node:Control" patterns, or control names
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            val = node.value
            if val in EXPECTED_STUDIO_SINKS:
                referenced_sinks.add(val)
            if val in EXPECTED_CONTROL_PORT_NAMES:
                referenced_controls.add(val)
            elif ":" in val:
                parts = val.split(":", 1)
                lhs, rhs = parts[0].strip(), parts[1].strip()
                if any(k in lhs.lower() for k in ("studio", "dsp")) and rhs:
                    if rhs in EXPECTED_CONTROL_PORT_NAMES:
                        referenced_controls.add(rhs)
                    elif rhs not in ("In L", "In R", "Out L", "Out R", "In", "Out", "f"):
                        # Ignore URLs, log prefixes, systemd/dbus/pw strings
                        if not re.search(r"[/\\{}\[\]]", rhs) and len(rhs) <= 32:
                            suspicious_controls.append(val)
            elif val.lower() in known_controls_lower and val not in EXPECTED_CONTROL_PORT_NAMES:
                suspicious_controls.append(f"Case mismatch: {val!r} (expected {known_controls_lower[val.lower()]!r})")

    return {
        "file": str(py_path),
        "referenced_controls": sorted(referenced_controls),
        "suspicious_controls": sorted(set(suspicious_controls)),
        "referenced_sinks": sorted(referenced_sinks),
    }


def run_group2_ladspa_abi_and_kernels(workspace: Path, g1_ctx: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    abi_ctx: dict[str, Any] = {"so_valid": False, "so_path": None, "ports": []}

    so_path = workspace / "dsp-engine" / "lib" / "libxps17_studio_dsp.so"
    abi_ctx["so_path"] = str(so_path)

    # 2.1 LADSPA dlopen & ABI inspection
    if not so_path.exists():
        checks.append(
            make_check(
                "G2.1_ladspa_abi",
                "LADSPA dlopen & Descriptor ABI (Label, UniqueID, Hard-RT)",
                "PENDING",
                f"Plugin binary not yet built at {so_path} (awaiting Lead 3 dsp-engine).",
                {"path": str(so_path)},
            )
        )
        checks.append(
            make_check(
                "G2.2_ladspa_10port_contract",
                "LADSPA 4 Audio + Exact 10 Control Ports Contract",
                "PENDING",
                "Awaiting Lead 3 libxps17_studio_dsp.so to verify live dlopen port descriptors.",
                {"expected_controls": list(EXPECTED_CONTROL_PORT_NAMES)},
            )
        )
    else:
        try:
            lib = ctypes.CDLL(str(so_path))
            lib.ladspa_descriptor.argtypes = [ctypes.c_ulong]
            lib.ladspa_descriptor.restype = ctypes.POINTER(LADSPA_Descriptor)
            desc_ptr = lib.ladspa_descriptor(0)
            if not desc_ptr:
                raise RuntimeError("ladspa_descriptor(0) returned NULL")
            d = desc_ptr.contents
            label = d.Label.decode("utf-8", errors="replace") if d.Label else ""
            uid = int(d.UniqueID)
            props = int(d.Properties)
            hard_rt = bool(props & LADSPA_PROPERTY_HARD_RT_CAPABLE)
            has_callbacks = all(
                bool(getattr(d, cb))
                for cb in ("instantiate", "connect_port", "run", "cleanup")
            )

            abi_errors: list[str] = []
            if label != EXPECTED_LADSPA_LABEL:
                abi_errors.append(f"Label={label!r} != {EXPECTED_LADSPA_LABEL!r}")
            if uid != EXPECTED_LADSPA_UNIQUE_ID:
                abi_errors.append(f"UniqueID={uid} != {EXPECTED_LADSPA_UNIQUE_ID}")
            if not has_callbacks:
                abi_errors.append("Missing required LADSPA callback(s) (instantiate/connect_port/run/cleanup)")

            abi_status = "FAIL" if abi_errors else ("PASS" if hard_rt else "WARN")
            checks.append(
                make_check(
                    "G2.1_ladspa_abi",
                    "LADSPA dlopen & Descriptor ABI (Label, UniqueID, Hard-RT)",
                    abi_status,
                    f"dlopen OK: Label={label!r}, UniqueID={uid}, Hard-RT={hard_rt}, PortCount={d.PortCount}."
                    if not abi_errors
                    else f"LADSPA ABI mismatch: {'; '.join(abi_errors)}",
                    {
                        "path": str(so_path),
                        "sha256": sha256_file(so_path),
                        "label": label,
                        "unique_id": uid,
                        "properties_hex": hex(props),
                        "hard_rt_capable": hard_rt,
                        "port_count": int(d.PortCount),
                        "name": d.Name.decode("utf-8", errors="replace") if d.Name else "",
                        "maker": d.Maker.decode("utf-8", errors="replace") if d.Maker else "",
                    },
                )
            )

            # Inspect ports
            audio_in: list[str] = []
            audio_out: list[str] = []
            ctrl_in: list[dict[str, Any]] = []
            ctrl_out: list[str] = []
            port_table: list[dict[str, Any]] = []

            for i in range(int(d.PortCount)):
                pdesc = int(d.PortDescriptors[i])
                pname = d.PortNames[i].decode("utf-8", errors="replace") if d.PortNames[i] else ""
                hint = d.PortRangeHints[i]
                rec = {
                    "index": i,
                    "name": pname,
                    "descriptor_hex": hex(pdesc),
                    "hint_hex": hex(int(hint.HintDescriptor)),
                    "lower_bound": round(float(hint.LowerBound), 6),
                    "upper_bound": round(float(hint.UpperBound), 6),
                }
                port_table.append(rec)
                if (pdesc & LADSPA_PORT_AUDIO) and (pdesc & LADSPA_PORT_INPUT):
                    audio_in.append(pname)
                elif (pdesc & LADSPA_PORT_AUDIO) and (pdesc & LADSPA_PORT_OUTPUT):
                    audio_out.append(pname)
                elif (pdesc & LADSPA_PORT_CONTROL) and (pdesc & LADSPA_PORT_INPUT):
                    ctrl_in.append(rec)
                elif (pdesc & LADSPA_PORT_CONTROL) and (pdesc & LADSPA_PORT_OUTPUT):
                    ctrl_out.append(pname)

            abi_ctx["ports"] = port_table
            port_errors: list[str] = []
            if tuple(audio_in) != EXPECTED_AUDIO_INPUT_PORTS:
                port_errors.append(f"Audio inputs {audio_in} != {list(EXPECTED_AUDIO_INPUT_PORTS)}")
            if tuple(audio_out) != EXPECTED_AUDIO_OUTPUT_PORTS:
                port_errors.append(f"Audio outputs {audio_out} != {list(EXPECTED_AUDIO_OUTPUT_PORTS)}")
            ctrl_in_names = [c["name"] for c in ctrl_in]
            if set(ctrl_in_names) != set(EXPECTED_CONTROL_PORT_NAMES) or len(ctrl_in_names) != len(EXPECTED_CONTROL_PORT_NAMES):
                missing_c = [c for c in EXPECTED_CONTROL_PORT_NAMES if c not in ctrl_in_names]
                extra_c = [c for c in ctrl_in_names if c not in EXPECTED_CONTROL_PORT_NAMES]
                port_errors.append(f"Control port mismatch: missing={missing_c}, extra={extra_c}")
            else:
                # Verify bounds for each control port
                for c in ctrl_in:
                    spec = EXPECTED_CONTROL_PORT_MAP[c["name"]]
                    hdesc = int(c["hint_hex"], 16)
                    if (hdesc & LADSPA_HINT_BOUNDED_BELOW) and abs(c["lower_bound"] - spec["min"]) > 1e-3:
                        port_errors.append(f"Port {c['name']!r} lower_bound={c['lower_bound']} != {spec['min']}")
                    if (hdesc & LADSPA_HINT_BOUNDED_ABOVE) and abs(c["upper_bound"] - spec["max"]) > 1e-3:
                        port_errors.append(f"Port {c['name']!r} upper_bound={c['upper_bound']} != {spec['max']}")

            abi_ctx["so_valid"] = (not abi_errors and not port_errors)
            checks.append(
                make_check(
                    "G2.2_ladspa_10port_contract",
                    "LADSPA 4 Audio + Exact 10 Control Ports Contract",
                    "PASS" if not port_errors else "FAIL",
                    f"Exact 4 audio ports + 10 control ports verified (optional control outputs: {ctrl_out})."
                    if not port_errors
                    else f"Port contract violations: {'; '.join(port_errors)}",
                    {
                        "audio_in": audio_in,
                        "audio_out": audio_out,
                        "control_in": ctrl_in_names,
                        "control_out": ctrl_out,
                        "errors": port_errors,
                        "port_table": port_table,
                    },
                )
            )
        except Exception as e:
            checks.append(
                make_check(
                    "G2.1_ladspa_abi",
                    "LADSPA dlopen & Descriptor ABI (Label, UniqueID, Hard-RT)",
                    "FAIL",
                    f"Failed to dlopen or inspect {so_path}: {e}",
                    {"path": str(so_path), "error": str(e)},
                )
            )

    # 2.3 Cross-contract parity: 92-xps17-studio.conf & staged controller <-> 10-port contract
    ctrl_runtime_path = workspace / "verify-integrate" / "staged" / "controller" / "runtime.py"
    ctrl_model_path = workspace / "verify-integrate" / "staged" / "controller" / "model.py"
    parity_errors: list[str] = []
    parity_evidence: dict[str, Any] = {}

    if g1_ctx.get("conf_parsed"):
        conf_ctrls = g1_ctx.get("ladspa_controls_by_sink", {})
        parity_evidence["pipewire_conf_controls"] = conf_ctrls
        if not conf_ctrls:
            parity_errors.append("92-xps17-studio.conf has no ladspa node with label 'xps17_studio_dsp'")
        for sink, keys in conf_ctrls.items():
            unknown = [k for k in keys if k not in EXPECTED_CONTROL_PORT_NAMES]
            if unknown:
                parity_errors.append(f"92-xps17-studio.conf sink {sink} references unknown control port(s): {unknown}")
    else:
        parity_evidence["pipewire_conf_controls"] = "PENDING"

    controller_present = False
    for py_path in (ctrl_runtime_path, ctrl_model_path):
        if py_path.exists():
            controller_present = True
            try:
                info = _extract_controller_control_refs(py_path)
                parity_evidence[py_path.name] = info
                if info["suspicious_controls"]:
                    parity_errors.append(f"{py_path.name} has suspicious/mismatched control names: {info['suspicious_controls']}")
            except SyntaxError as se:
                parity_errors.append(f"{py_path.name} Python syntax error: {se}")

    if not g1_ctx.get("conf_parsed") and not controller_present:
        checks.append(
            make_check(
                "G2.3_cross_contract_parity",
                "10-Port Control Parity (LADSPA <-> 92-xps17-studio.conf <-> controller)",
                "PENDING",
                "Neither 92-xps17-studio.conf nor staged controller files are present yet.",
                parity_evidence,
            )
        )
    elif parity_errors:
        checks.append(
            make_check(
                "G2.3_cross_contract_parity",
                "10-Port Control Parity (LADSPA <-> 92-xps17-studio.conf <-> controller)",
                "FAIL",
                f"Control contract parity failure: {'; '.join(parity_errors)}",
                parity_evidence,
            )
        )
    elif not g1_ctx.get("conf_parsed") or not controller_present:
        checks.append(
            make_check(
                "G2.3_cross_contract_parity",
                "10-Port Control Parity (LADSPA <-> 92-xps17-studio.conf <-> controller)",
                "PENDING",
                "Present staged files match exact 10-port contract; remaining staged files still pending.",
                parity_evidence,
            )
        )
    else:
        checks.append(
            make_check(
                "G2.3_cross_contract_parity",
                "10-Port Control Parity (LADSPA <-> 92-xps17-studio.conf <-> controller)",
                "PASS",
                "Every control port name in 92-xps17-studio.conf and staged controller matches the exact 10-port LADSPA contract.",
                parity_evidence,
            )
        )

    # 2.4 FIR Kernels validation (4x 48 kHz, 2-ch, 4096-tap IEEE float32 WAV, finite, bounded gain)
    kernels_dir = workspace / "acoustics-research" / "kernels"
    missing_kernels: list[str] = []
    kernel_results: dict[str, Any] = {}
    kernel_failures: list[str] = []

    for kname in EXPECTED_KERNELS:
        kpath = kernels_dir / kname
        if not kpath.exists():
            missing_kernels.append(kname)
            continue
        try:
            wav = read_float32_wav(kpath)
            errs: list[str] = []
            if wav["sample_rate"] != 48000:
                errs.append(f"sample_rate={wav['sample_rate']} != 48000")
            if wav["channels"] != 2:
                errs.append(f"channels={wav['channels']} != 2")
            if wav["num_frames"] != 4096:
                errs.append(f"num_frames={wav['num_frames']} != 4096")
            ch_stats: list[dict[str, Any]] = []
            for ch_idx, taps in enumerate(wav["channels_data"]):
                non_finite = sum(1 for v in taps if not math.isfinite(v))
                if non_finite > 0:
                    errs.append(f"ch{ch_idx} has {non_finite} NaN/Inf tap(s)")
                    continue
                peak_tap = max(abs(v) for v in taps)
                l1 = sum(abs(v) for v in taps)
                l2 = math.sqrt(sum(v * v for v in taps))
                dc_gain = sum(taps)
                max_fft_mag = None
                max_fft_db = None
                if len(taps) == 4096:
                    spec = radix2_fft(taps)
                    mags = [abs(c) for c in spec[:2049]]
                    max_fft_mag = max(mags)
                    max_fft_db = 20.0 * math.log10(max(max_fft_mag, 1e-12))
                    if max_fft_db > 24.0:
                        errs.append(f"ch{ch_idx} excessive peak frequency gain {max_fft_db:.2f} dB (> +24 dB)")
                if l2 < 1e-6:
                    errs.append(f"ch{ch_idx} is silent/near-zero (L2={l2:.2e})")
                if peak_tap > 16.0:
                    errs.append(f"ch{ch_idx} unbounded tap amplitude {peak_tap:.3f} (> 16.0)")
                ch_stats.append({
                    "channel": ch_idx,
                    "peak_tap": round(peak_tap, 6),
                    "l1_norm": round(l1, 6),
                    "l2_norm": round(l2, 6),
                    "dc_gain": round(dc_gain, 6),
                    "max_fft_db": round(max_fft_db, 3) if max_fft_db is not None else None,
                })
            kernel_results[kname] = {
                "sha256": sha256_file(kpath),
                "sample_rate": wav["sample_rate"],
                "channels": wav["channels"],
                "num_frames": wav["num_frames"],
                "stats": ch_stats,
                "errors": errs,
            }
            if errs:
                kernel_failures.append(f"{kname}: {'; '.join(errs)}")
        except Exception as e:
            kernel_failures.append(f"{kname}: {e}")
            kernel_results[kname] = {"error": str(e)}

    if kernel_failures:
        k_status = "FAIL"
        k_msg = f"FIR kernel verification failed: {'; '.join(kernel_failures)}"
    elif missing_kernels:
        k_status = "PENDING"
        k_msg = f"Awaiting {len(missing_kernels)}/4 FIR kernel(s) from Lead 2: {missing_kernels}"
    else:
        k_status = "PASS"
        k_msg = "All 4 FIR kernels verified: 48 kHz, 2-ch, 4096-tap IEEE float32, finite taps, bounded FFT gain."

    checks.append(
        make_check(
            "G2.4_fir_kernels",
            "4x 4096-Tap Stereo 48 kHz Float32 FIR Kernels",
            k_status,
            k_msg,
            {"kernels_dir": str(kernels_dir), "missing": missing_kernels, "results": kernel_results},
        )
    )

    # 2.5 dsp_params.json presence & validity
    dsp_params_path = workspace / "acoustics-research" / "dsp_params.json"
    if not dsp_params_path.exists():
        checks.append(
            make_check(
                "G2.5_dsp_params_json",
                "acoustics-research/dsp_params.json Contract",
                "PENDING",
                f"dsp_params.json not yet present at {dsp_params_path} (awaiting Lead 2).",
                {"path": str(dsp_params_path)},
            )
        )
    else:
        try:
            params_obj = json.loads(dsp_params_path.read_text(encoding="utf-8"))
            checks.append(
                make_check(
                    "G2.5_dsp_params_json",
                    "acoustics-research/dsp_params.json Contract",
                    "PASS" if isinstance(params_obj, dict) and len(params_obj) > 0 else "FAIL",
                    f"dsp_params.json parsed cleanly ({len(params_obj)} top-level keys).",
                    {"path": str(dsp_params_path), "sha256": sha256_file(dsp_params_path), "keys": sorted(params_obj.keys()) if isinstance(params_obj, dict) else []},
                )
            )
        except Exception as e:
            checks.append(
                make_check(
                    "G2.5_dsp_params_json",
                    "acoustics-research/dsp_params.json Contract",
                    "FAIL",
                    f"Invalid JSON in dsp_params.json: {e}",
                    {"path": str(dsp_params_path)},
                )
            )

    status, counts = summarize_checks(checks)
    return (
        {"title": "Check Group 2 — LADSPA ABI, 10-Port Contract & FIR Kernels", "status": status, "counts": counts, "checks": checks},
        abi_ctx,
    )


# ---------------------------------------------------------------------------
# Check Group 3 — Live Mic-Privacy, Bluetooth & Loudness Authority Guards
# ---------------------------------------------------------------------------

def run_group3_privacy_bt_loudness(workspace: Path, home_dir: Path, g1_ctx: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    # 3.1 no-mic-monitor.conf
    no_mic_path = home_dir / ".config/systemd/user/easyeffects-speaker-dsp.service.d/no-mic-monitor.conf"
    if not no_mic_path.exists():
        checks.append(
            make_check(
                "G3.1_no_mic_monitor_dropin",
                "Live EasyEffects no-mic-monitor.conf Drop-in Guard",
                "FAIL",
                f"CRITICAL: Live safety guard missing at {no_mic_path}",
                {"path": str(no_mic_path)},
            )
        )
    else:
        txt = no_mic_path.read_text(encoding="utf-8", errors="replace")
        has_force_off = bool(re.search(r"ExecStartPre=.*--key\s+listenToMic\s+false", txt))
        checks.append(
            make_check(
                "G3.1_no_mic_monitor_dropin",
                "Live EasyEffects no-mic-monitor.conf Drop-in Guard",
                "PASS" if has_force_off else "FAIL",
                "no-mic-monitor.conf exists and enforces listenToMic=false via ExecStartPre."
                if has_force_off
                else "no-mic-monitor.conf missing ExecStartPre listenToMic false enforcement!",
                {"path": str(no_mic_path), "sha256": sha256_file(no_mic_path)},
            )
        )

    # 3.2 51-bluetooth-no-phone-audio.conf
    bt_path = home_dir / ".config/wireplumber/wireplumber.conf.d/51-bluetooth-no-phone-audio.conf"
    if not bt_path.exists():
        checks.append(
            make_check(
                "G3.2_bt_no_phone_audio",
                "Live WirePlumber 51-bluetooth-no-phone-audio.conf Guard",
                "FAIL",
                f"CRITICAL: Bluetooth privacy guard missing at {bt_path}",
                {"path": str(bt_path)},
            )
        )
    else:
        txt = bt_path.read_text(encoding="utf-8", errors="replace")
        try:
            bt_doc = parse_spa_json(txt)
            roles = bt_doc.get("monitor.bluez.properties", {}).get("bluez5.roles", [])
            forbidden_roles = [r for r in ("a2dp_sink", "hfp_hf") if r in roles]
            ok = isinstance(roles, list) and len(roles) > 0 and not forbidden_roles
            checks.append(
                make_check(
                    "G3.2_bt_no_phone_audio",
                    "Live WirePlumber 51-bluetooth-no-phone-audio.conf Guard",
                    "PASS" if ok else "FAIL",
                    f"51-bluetooth-no-phone-audio.conf verified: excludes a2dp_sink and hfp_hf (roles={roles})."
                    if ok
                    else f"Bluetooth guard violation: roles={roles}, forbidden_present={forbidden_roles}",
                    {"path": str(bt_path), "sha256": sha256_file(bt_path), "roles": roles},
                )
            )
        except SpaJsonError as e:
            checks.append(
                make_check(
                    "G3.2_bt_no_phone_audio",
                    "Live WirePlumber 51-bluetooth-no-phone-audio.conf Guard",
                    "FAIL",
                    f"51-bluetooth-no-phone-audio.conf failed SPA-JSON parse: {e}",
                    {"path": str(bt_path)},
                )
            )

    # 3.3 60-soundwire-input-default-volume.conf
    mic_vol_conf = home_dir / ".config/wireplumber/wireplumber.conf.d/60-soundwire-input-default-volume.conf"
    if not mic_vol_conf.exists():
        checks.append(
            make_check(
                "G3.3_soundwire_mic_default_conf",
                "Live WirePlumber 60-soundwire-input-default-volume.conf Guard",
                "FAIL",
                f"SoundWire input default-volume guard missing at {mic_vol_conf}",
                {"path": str(mic_vol_conf)},
            )
        )
    else:
        txt = mic_vol_conf.read_text(encoding="utf-8", errors="replace")
        try:
            m_doc = parse_spa_json(txt)
            rules = m_doc.get("monitor.alsa.rules", [])
            pinned_unity = any(
                isinstance(r, dict)
                and isinstance(r.get("actions"), dict)
                and isinstance(r["actions"].get("update-props"), dict)
                and abs(float(r["actions"]["update-props"].get("device.routes.default-source-volume", -1.0)) - 1.0) < 1e-6
                for r in (rules if isinstance(rules, list) else [])
            )
            checks.append(
                make_check(
                    "G3.3_soundwire_mic_default_conf",
                    "Live WirePlumber 60-soundwire-input-default-volume.conf Guard",
                    "PASS" if pinned_unity else "FAIL",
                    "60-soundwire-input-default-volume.conf pins device.routes.default-source-volume = 1.0."
                    if pinned_unity
                    else "60-soundwire-input-default-volume.conf does not pin default-source-volume = 1.0!",
                    {"path": str(mic_vol_conf), "sha256": sha256_file(mic_vol_conf)},
                )
            )
        except Exception as e:
            checks.append(
                make_check(
                    "G3.3_soundwire_mic_default_conf",
                    "Live WirePlumber 60-soundwire-input-default-volume.conf Guard",
                    "FAIL",
                    f"Failed to parse 60-soundwire-input-default-volume.conf: {e}",
                    {"path": str(mic_vol_conf)},
                )
            )

    # 3.4 Live Microphone Default Volume == 1.00 (read-only wpctl status query ONLY — never captures audio)
    try:
        proc = subprocess.run(
            ["wpctl", "get-volume", "@DEFAULT_AUDIO_SOURCE@"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        out = proc.stdout.strip()
        m = re.search(r"Volume:\s*([0-9]+(?:\.[0-9]+)?)", out)
        if proc.returncode == 0 and m:
            vol = float(m.group(1))
            is_unity = abs(vol - 1.00) < 0.005
            checks.append(
                make_check(
                    "G3.4_live_mic_volume",
                    "Live Default Audio Source Volume == 1.00 (100%)",
                    "PASS" if is_unity else "FAIL",
                    f"Read-only wpctl query reports @DEFAULT_AUDIO_SOURCE@ {out} (100% unity)."
                    if is_unity
                    else f"@DEFAULT_AUDIO_SOURCE@ volume is {vol:.2f} (expected 1.00): {out}",
                    {"raw_output": out, "volume": vol},
                )
            )
        else:
            checks.append(
                make_check(
                    "G3.4_live_mic_volume",
                    "Live Default Audio Source Volume == 1.00 (100%)",
                    "WARN",
                    f"wpctl get-volume returned rc={proc.returncode}, stdout={out!r}, stderr={proc.stderr.strip()!r}",
                    {"returncode": proc.returncode, "stdout": out, "stderr": proc.stderr.strip()},
                )
            )
    except Exception as e:
        checks.append(
            make_check(
                "G3.4_live_mic_volume",
                "Live Default Audio Source Volume == 1.00 (100%)",
                "WARN",
                f"Could not query wpctl: {e}",
                {"error": str(e)},
            )
        )

    # 3.5 Live EasyEffects listenToMic == false
    ee_rc = home_dir / ".config/easyeffects/db/easyeffectsrc"
    if not ee_rc.exists():
        checks.append(
            make_check(
                "G3.5_easyeffects_listen_to_mic",
                "Live EasyEffects listenToMic == false",
                "WARN",
                f"EasyEffects config file not found at {ee_rc}",
                {"path": str(ee_rc)},
            )
        )
    else:
        txt = ee_rc.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^listenToMic\s*=\s*(\S+)\s*$", txt, re.MULTILINE)
        val = m.group(1).lower() if m else None
        ok = (val == "false")
        checks.append(
            make_check(
                "G3.5_easyeffects_listen_to_mic",
                "Live EasyEffects listenToMic == false",
                "PASS" if ok else "FAIL",
                f"easyeffectsrc sets listenToMic={val} (mic loopback disabled)."
                if ok
                else f"CRITICAL: easyeffectsrc listenToMic={val!r} (must be 'false')!",
                {"path": str(ee_rc), "listenToMic": val},
            )
        )

    # 3.6 Staged files non-interference with privacy/BT guards & LOUDNESS_DRIVE_DB preservation
    staged_dir = workspace / "verify-integrate" / "staged"
    staged_files = [p for p in staged_dir.rglob("*") if p.is_file()] if staged_dir.exists() else []
    interference_hits: list[str] = []
    forbidden_staged_targets = (
        "no-mic-monitor.conf",
        "51-bluetooth-no-phone-audio.conf",
        "60-soundwire-input-default-volume.conf",
    )
    for sf in staged_files:
        if sf.name in forbidden_staged_targets:
            interference_hits.append(f"Staged file shadows protected guard: {sf}")
            continue
        try:
            content = sf.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if re.search(r"listenToMic\s*=\s*true", content, re.IGNORECASE):
            interference_hits.append(f"{sf}: sets listenToMic=true")
        if re.search(r"bluez5\.roles.*\b(a2dp_sink|hfp_hf)\b", content):
            interference_hits.append(f"{sf}: re-enables phone audio Bluetooth roles")
        for guard_name in forbidden_staged_targets:
            # Flag destructive operations referencing protected guards (ignoring read-only verification comments)
            for lno, line in enumerate(content.splitlines(), 1):
                if guard_name in line and re.search(r"\b(rm|mv|cp|tee|sed\s+-i|truncate|unlink|write_text)\b", line):
                    interference_hits.append(f"{sf}:{lno}: mutates protected guard {guard_name}")

    checks.append(
        make_check(
            "G3.6_staged_guard_non_interference",
            "Staged Files Do Not Touch Mic-Privacy or Bluetooth Guards",
            "PASS" if not interference_hits else "FAIL",
            f"Scanned {len(staged_files)} staged file(s): zero modifications to mic-privacy or Bluetooth guards."
            if not interference_hits
            else f"Guard interference detected: {'; '.join(interference_hits)}",
            {"staged_file_count": len(staged_files), "violations": interference_hits},
        )
    )

    # 3.7 Loudness authority preservation (LOUDNESS_DRIVE_DB in staged runtime.py & Drive (dB) in 92-xps17-studio.conf)
    staged_runtime = staged_dir / "controller" / "runtime.py"
    loudness_issues: list[str] = []
    loudness_evidence: dict[str, Any] = {}

    if staged_runtime.exists():
        rt_src = staged_runtime.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(rt_src, filename=str(staged_runtime))
            found_drive_val = None
            for node in tree.body:
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "LOUDNESS_DRIVE_DB":
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, (int, float)):
                        found_drive_val = float(node.value.value)
                elif isinstance(node, ast.Assign):
                    for t in node.targets:
                        if isinstance(t, ast.Name) and t.id == "LOUDNESS_DRIVE_DB":
                            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, (int, float)):
                                found_drive_val = float(node.value.value)
            loudness_evidence["staged_runtime_LOUDNESS_DRIVE_DB"] = found_drive_val
            if found_drive_val is None:
                loudness_issues.append("staged/controller/runtime.py strips LOUDNESS_DRIVE_DB!")
            elif found_drive_val <= 0.0:
                loudness_issues.append(f"staged/controller/runtime.py sets LOUDNESS_DRIVE_DB={found_drive_val} <= 0!")
        except SyntaxError as se:
            loudness_issues.append(f"staged/controller/runtime.py syntax error: {se}")

    if g1_ctx.get("conf_parsed"):
        drives = g1_ctx.get("drive_db_by_sink", {})
        ceilings = g1_ctx.get("ceiling_db_by_sink", {})
        loudness_evidence["pipewire_drive_db"] = drives
        loudness_evidence["pipewire_ceiling_db"] = ceilings
        for sink, drv in drives.items():
            if not isinstance(drv, (int, float)) or float(drv) <= 0.0:
                loudness_issues.append(f"92-xps17-studio.conf sink {sink} has Drive (dB)={drv!r} (loudness must live inside DSP chain!)")
        for sink, ceil in ceilings.items():
            if not isinstance(ceil, (int, float)) or not (-9.0 <= float(ceil) <= -0.3):
                loudness_issues.append(f"92-xps17-studio.conf sink {sink} Ceiling (dBFS)={ceil!r} outside [-9.0, -0.3]")

    if loudness_issues:
        l_status = "FAIL"
        l_msg = f"Loudness authority violation: {'; '.join(loudness_issues)}"
    elif not staged_runtime.exists() and not g1_ctx.get("conf_parsed"):
        l_status = "PENDING"
        l_msg = "Awaiting staged controller/runtime.py and 92-xps17-studio.conf to verify LOUDNESS_DRIVE_DB preservation."
    elif not staged_runtime.exists() or not g1_ctx.get("conf_parsed"):
        l_status = "PENDING"
        l_msg = "Present staged file preserves loudness authority; awaiting remaining staged file(s)."
    else:
        l_status = "PASS"
        l_msg = "Loudness authority preserved: LOUDNESS_DRIVE_DB intact in controller/runtime.py and Drive (dB) + brickwall Ceiling active in 92-xps17-studio.conf."

    checks.append(
        make_check(
            "G3.7_loudness_authority",
            "Loudness Authority (LOUDNESS_DRIVE_DB & In-Chain Drive/Ceiling)",
            l_status,
            l_msg,
            loudness_evidence,
        )
    )

    status, counts = summarize_checks(checks)
    return {"title": "Check Group 3 — Live Mic-Privacy, Bluetooth & Loudness Authority Guards", "status": status, "counts": counts, "checks": checks}


# ---------------------------------------------------------------------------
# Check Group 4 — Kernel Driver Forbidden-Register Static Audit (Lead 1)
# ---------------------------------------------------------------------------

def strip_c_comments_preserve_lines(src: str) -> str:
    """Remove C /* */ and // comments while keeping exact newline line numbering."""
    out: list[str] = []
    i = 0
    n = len(src)
    in_str = False
    str_ch = ""
    while i < n:
        ch = src[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if ch == str_ch:
                in_str = False
            i += 1
            continue
        if ch in ('"', "'"):
            in_str = True
            str_ch = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                if src[i] == "\n":
                    out.append("\n")
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


WRITE_CALL_RE = re.compile(
    r"\b(?:regmap_write|regmap_write_async|regmap_update_bits|regmap_update_bits_base|"
    r"regmap_write_bits|sdw_write|sdw_write_no_pm|sdw_update|sdw_update_no_pm|sdw_nwrite)\s*\(([^;]{0,300})\)",
    re.DOTALL,
)
REG_SEQ_ENTRY_RE = re.compile(
    r"\{\s*(0[xX][0-9a-fA-F]+|[A-Za-z_][A-Za-z0-9_]*)\s*,\s*(0[xX][0-9a-fA-F]+|[0-9]+|[A-Za-z_][A-Za-z0-9_]*)\s*(?:,[^}]*)?\}"
)
HEX_TOKEN_RE = re.compile(r"\b0[xX]([0-9a-fA-F]{4,8})\b")
SAFE_READ_OR_GUARD_LINE_RE = re.compile(
    r"\b(?:regmap_read|sdw_read|sdw_read_no_pm|forbidden|deny|block|reject|protect|WARN_ON|BUG_ON|case\s+0[xX]|return\s+-E(?:PERM|ACCES|INVAL))\b",
    re.IGNORECASE,
)
DEADMAN_KEYWORDS_RE = re.compile(
    r"\b(?:deadman|heartbeat|watchdog|delayed_work|mod_timer|timer_setup|jiffies|high_gain_enable|allow_high_gain|macaudio)\b",
    re.IGNORECASE,
)


# Exact immutable upstream Linux v7.2.15 rt1316-sdw.c blind_write entries & SW reset.
# Recognized ONLY inside full copies of upstream rt1316-sdw.c (NEVER in .patch added lines or custom files!).
UPSTREAM_RT1316_BLIND_WRITE_STOCK: dict[int, int] = {
    0xC60E: 0x05,
    0xC102: 0x00,
    0xC09C: 0x7B,
    0xC0A3: 0x71,
    0xC093: 0x80,
    0xC09D: 0x80,
    0xC050: 0x83,
}


def _scan_c_lines_for_registers(
    rel_path: str,
    lines: list[tuple[int, str]],
    full_text_clean: str,
    *,
    is_patch: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    forbidden_hits: list[dict[str, Any]] = []
    high_gain_hits: list[dict[str, Any]] = []
    upstream_stock_notes: list[dict[str, Any]] = []
    seen_forbidden: set[tuple[int, int]] = set()
    seen_high_gain: set[tuple[int, int]] = set()

    is_upstream_rt1316_c = (not is_patch) and (Path(rel_path).name == "rt1316-sdw.c")
    has_deadman_mechanism = bool(DEADMAN_KEYWORDS_RE.search(full_text_clean))

    # Build #define constant table for hex values (including bit-shifted/parenthesized constants)
    defines: dict[str, int] = {}
    for _, line in lines:
        dm = re.match(r"^\s*#\s*define\s+([A-Za-z_][A-Za-z0-9_]*)\s+\(?\s*(0[xX][0-9a-fA-F]+)[uUlL]*\s*\)?", line)
        if dm:
            defines[dm.group(1)] = int(dm.group(2), 16)

    def _resolve_int(token: str) -> int | None:
        t = token.strip().strip("()")
        if re.fullmatch(r"0[xX][0-9a-fA-F]+[uUlL]*", t):
            return int(re.sub(r"[uUlL]+$", "", t), 16)
        if re.fullmatch(r"[0-9]+[uUlL]*", t):
            return int(re.sub(r"[uUlL]+$", "", t), 10)
        return defines.get(t)

    # Map clean line index (0-based) -> original file line number
    line_num_map = [orig_lno for orig_lno, _ in lines]

    def _orig_line_for_offset(offset: int) -> int:
        idx = full_text_clean[:offset].count("\n")
        if 0 <= idx < len(line_num_map):
            return line_num_map[idx]
        return idx + 1

    # 1. Scan single-line and multi-line write function calls across full_text_clean
    for wm in WRITE_CALL_RE.finditer(full_text_clean):
        lno = _orig_line_for_offset(wm.start())
        args_str = wm.group(1)
        args = [a.strip() for a in args_str.split(",")]
        snippet = re.sub(r"\s+", " ", wm.group(0)).strip()[:180]
        if len(args) >= 2:
            reg_val = _resolve_int(args[1])
            val_arg = _resolve_int(args[2]) if len(args) >= 3 else None
            # For regmap_update_bits(map, reg, mask, val), value is args[3]
            if len(args) >= 4 and ("update_bits" in wm.group(0) or "write_bits" in wm.group(0)):
                val_arg = _resolve_int(args[3])
            if reg_val in FORBIDDEN_ALC1316_REGS and (lno, reg_val) not in seen_forbidden:
                if is_upstream_rt1316_c and reg_val == 0xC000 and val_arg == 0x02 and "rt1316->regmap" in args[0]:
                    upstream_stock_notes.append({
                        "file": rel_path,
                        "line": lno,
                        "register": hex(reg_val),
                        "value": hex(val_arg),
                        "note": "Unchanged upstream v7.2.15 rt1316_io_init SW reset",
                    })
                else:
                    seen_forbidden.add((lno, reg_val))
                    forbidden_hits.append({
                        "file": rel_path,
                        "line": lno,
                        "register": hex(reg_val),
                        "reason": FORBIDDEN_ALC1316_REGS[reg_val],
                        "snippet": snippet,
                    })
            if reg_val in HIGH_GAIN_GATED_REGS and val_arg == HIGH_GAIN_GATED_REGS[reg_val] and (lno, reg_val) not in seen_high_gain:
                seen_high_gain.add((lno, reg_val))
                high_gain_hits.append({
                    "file": rel_path,
                    "line": lno,
                    "register": hex(reg_val),
                    "value": hex(val_arg),
                    "kind": "write_call",
                    "deadman_present_in_file": has_deadman_mechanism,
                    "snippet": snippet,
                })

    # 2. Scan line-by-line for static register table entries ({ reg, val }),
    #    skipping policy denylist/range arrays and recognizing exact upstream rt1316_blind_write stock values.
    in_nonwrite_array = False
    in_blind_write_array = False
    for lno, line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if re.search(
            r"(?:(?:forbidden|blocked|denied|protected)_regs?|_lab_never|_lab_volatile|_lab_ranges|reg_range)\b.*=",
            stripped,
            re.IGNORECASE,
        ):
            in_nonwrite_array = True
        if is_upstream_rt1316_c and re.search(r"\brt1316_blind_write\b.*=", stripped):
            in_blind_write_array = True

        if in_nonwrite_array:
            if "};" in stripped:
                in_nonwrite_array = False
            continue

        if not SAFE_READ_OR_GUARD_LINE_RE.search(stripped):
            for rm in REG_SEQ_ENTRY_RE.finditer(stripped):
                reg_val = _resolve_int(rm.group(1))
                val_arg = _resolve_int(rm.group(2))
                if reg_val in FORBIDDEN_ALC1316_REGS and (lno, reg_val) not in seen_forbidden:
                    if in_blind_write_array and UPSTREAM_RT1316_BLIND_WRITE_STOCK.get(reg_val) == val_arg:
                        upstream_stock_notes.append({
                            "file": rel_path,
                            "line": lno,
                            "register": hex(reg_val),
                            "value": hex(val_arg),
                            "note": "Unchanged upstream v7.2.15 rt1316_blind_write stock entry",
                        })
                    else:
                        seen_forbidden.add((lno, reg_val))
                        forbidden_hits.append({
                            "file": rel_path,
                            "line": lno,
                            "register": hex(reg_val),
                            "reason": FORBIDDEN_ALC1316_REGS[reg_val],
                            "snippet": stripped[:180],
                        })
                if reg_val in HIGH_GAIN_GATED_REGS and val_arg == HIGH_GAIN_GATED_REGS[reg_val] and (lno, reg_val) not in seen_high_gain:
                    seen_high_gain.add((lno, reg_val))
                    high_gain_hits.append({
                        "file": rel_path,
                        "line": lno,
                        "register": hex(reg_val),
                        "value": hex(val_arg),
                        "kind": "static_table_entry",
                        "deadman_present_in_file": has_deadman_mechanism,
                        "snippet": stripped[:180],
                    })

        if in_blind_write_array and "};" in stripped:
            in_blind_write_array = False

    return forbidden_hits, high_gain_hits, upstream_stock_notes


def run_group4_kernel_driver_safety(workspace: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    kd_dir = workspace / "kernel-driver"

    code_files: list[Path] = []
    ipc4_files: list[Path] = []
    all_text_files: list[Path] = []

    if kd_dir.exists():
        for p in sorted(kd_dir.rglob("*")):
            if not p.is_file():
                continue
            if p.suffix in (".c", ".h", ".patch"):
                code_files.append(p)
            if p.suffix in (".c", ".h", ".patch", ".conf", ".ucm", ".sh", ".py", ".txt", ".md"):
                all_text_files.append(p)
            if "sof-ipc4" in p.parts or p.suffix in (".conf", ".ucm") or "ucm" in p.name.lower() or "ipc4" in p.name.lower():
                if p.suffix in (".conf", ".ucm", ".sh", ".py", ".c", ".patch"):
                    ipc4_files.append(p)

    # 4.1 Forbidden ALC1316 protection register writes
    if not code_files:
        checks.append(
            make_check(
                "G4.1_forbidden_alc1316_regs",
                "Forbidden ALC1316 Protection Registers Static Audit",
                "PENDING",
                f"No .c/.h/.patch files present yet under {kd_dir} (awaiting Lead 1 kernel-driver).",
                {"kernel_driver_dir": str(kd_dir), "forbidden_regs": [hex(r) for r in sorted(FORBIDDEN_ALC1316_REGS)]},
            )
        )
        checks.append(
            make_check(
                "G4.2_high_gain_deadman_gate",
                "High-Gain (0xC60F=0x05, 0xD002/0xD004=0xA9) Deadman Protection Gate",
                "PENDING",
                "No kernel driver source/patch files yet under kernel-driver/ (awaiting Lead 1).",
                {},
            )
        )
    else:
        all_forbidden_hits: list[dict[str, Any]] = []
        all_high_gain_hits: list[dict[str, Any]] = []
        all_upstream_stock_notes: list[dict[str, Any]] = []

        for fpath in code_files:
            rel = str(fpath.relative_to(workspace))
            raw = fpath.read_text(encoding="utf-8", errors="replace")
            if fpath.suffix == ".patch":
                # Only scan added lines ('+' excluding '+++')
                added_lines: list[tuple[int, str]] = []
                for idx, ln in enumerate(raw.splitlines(), 1):
                    if ln.startswith("+") and not ln.startswith("+++"):
                        added_lines.append((idx, ln[1:]))
                clean_joined = strip_c_comments_preserve_lines("\n".join(t for _, t in added_lines))
                clean_pairs = [(added_lines[i][0], cl) for i, cl in enumerate(clean_joined.splitlines()) if i < len(added_lines)]
                fh, hgh, notes = _scan_c_lines_for_registers(rel, clean_pairs, clean_joined, is_patch=True)
            else:
                clean = strip_c_comments_preserve_lines(raw)
                clean_pairs = list(enumerate(clean.splitlines(), 1))
                fh, hgh, notes = _scan_c_lines_for_registers(rel, clean_pairs, clean, is_patch=False)
            all_forbidden_hits.extend(fh)
            all_high_gain_hits.extend(hgh)
            all_upstream_stock_notes.extend(notes)

        checks.append(
            make_check(
                "G4.1_forbidden_alc1316_regs",
                "Forbidden ALC1316 Protection Registers Static Audit",
                "PASS" if not all_forbidden_hits else "FAIL",
                f"Scanned {len(code_files)} kernel C/H/patch file(s): ZERO writes to forbidden ALC1316 protection registers."
                if not all_forbidden_hits
                else f"CRITICAL: Detected {len(all_forbidden_hits)} write(s) to forbidden ALC1316 protection register(s)!",
                {
                    "scanned_files": [str(f.relative_to(workspace)) for f in code_files],
                    "forbidden_regs": [hex(r) for r in sorted(FORBIDDEN_ALC1316_REGS)],
                    "violations": all_forbidden_hits,
                    "upstream_stock_entries_verified_unchanged": all_upstream_stock_notes,
                },
            )
        )

        # 4.2 High-gain deadman gate audit
        ungated_high_gain = [
            h for h in all_high_gain_hits
            if h["kind"] == "static_table_entry" or not h["deadman_present_in_file"]
        ]
        if not all_high_gain_hits:
            hg_status = "PASS"
            hg_msg = "Conservative safe posture: no 0xC60F=0x05 or 0xD002/0xD004=0xA9 high-gain writes present."
        elif not ungated_high_gain:
            hg_status = "PASS"
            hg_msg = f"All {len(all_high_gain_hits)} high-gain write(s) are runtime-gated behind a deadman/heartbeat protection mechanism."
        else:
            hg_status = "FAIL"
            hg_msg = f"CRITICAL: {len(ungated_high_gain)} unconditional/ungated high-gain register write(s) found without deadman timer gate!"

        checks.append(
            make_check(
                "G4.2_high_gain_deadman_gate",
                "High-Gain (0xC60F=0x05, 0xD002/0xD004=0xA9) Deadman Protection Gate",
                hg_status,
                hg_msg,
                {"high_gain_hits": all_high_gain_hits, "ungated_violations": ungated_high_gain},
            )
        )

    # 4.3 IPC4 Post Mixer Speaker Playback DRC switch fence check
    if not ipc4_files:
        checks.append(
            make_check(
                "G4.3_ipc4_drc_switch_fence",
                "IPC4 'Post Mixer Speaker Playback DRC switch' Fenced OFF",
                "PENDING",
                f"No SOF IPC4 / UCM2 config files present yet under {kd_dir / 'sof-ipc4'} (awaiting Lead 1).",
                {"sof_ipc4_dir": str(kd_dir / "sof-ipc4")},
            )
        )
    else:
        drc_fenced_off = False
        fence_mechanisms: list[str] = []
        drc_enabled_hits: list[str] = []
        drc_evidence: list[str] = []
        for fpath in ipc4_files:
            txt = fpath.read_text(encoding="utf-8", errors="replace")
            rel = str(fpath.relative_to(workspace))
            defines_drc_ctl_var = bool(
                re.search(r'^[ \t]*(?:local\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=\s*["\']Post Mixer Speaker Playback DRC switch["\']', txt, re.MULTILINE)
            )
            for lno, line in enumerate(txt.splitlines(), 1):
                stripped = line.strip()
                if (
                    "Post Mixer Speaker Playback DRC switch" in line
                    or "PostMixerSpeakerPlaybackDrcSwitch" in line
                ):
                    drc_evidence.append(f"{rel}:{lno}: {stripped[:140]}")
                # Ignore pure comments when deciding fence vs enable
                if not stripped or stripped.startswith("#") or stripped.startswith("//"):
                    continue

                # Layer 1: ALSA UCM2 Macro.SpeakerDrc.SofDrcBootSetup override (`Define.PostMixerSpeakerPlaybackDrcSwitch "N/A"` or `""`)
                m_ucm_def = re.search(r'\bDefine\.PostMixerSpeakerPlaybackDrcSwitch\s+"([^"]*)"', stripped)
                if m_ucm_def:
                    val = m_ucm_def.group(1).strip()
                    if val in ("", "N/A", "NONE", "OFF"):
                        drc_fenced_off = True
                        fence_mechanisms.append(f"UCM2 Define override ({rel}:{lno}: {stripped[:100]})")
                    else:
                        drc_enabled_hits.append(f"{rel}:{lno}: {stripped[:140]}")

                # Layer 2: Explicit ALSA cset / amixer forcing DRC switch OFF vs ON
                if "Post Mixer Speaker Playback DRC switch" in stripped:
                    if re.search(r"Post Mixer Speaker Playback DRC switch['\"\s,:=]*(?:off|0|false)\b", stripped, re.IGNORECASE):
                        drc_fenced_off = True
                        fence_mechanisms.append(f"Explicit cset off ({rel}:{lno})")
                    elif re.search(r"Post Mixer Speaker Playback DRC switch['\"\s,:=]*(?:on|1|true)\b", stripped, re.IGNORECASE):
                        drc_enabled_hits.append(f"{rel}:{lno}: {stripped[:140]}")

                # Layer 3: Shell verifier forcing $CTL_DRC off (and flagging if forcing it on)
                if defines_drc_ctl_var and re.search(r'\bamixer(?:_cset|\s+[^\n]*cset)\b.*\$[{]?CTL_DRC[}]?', stripped):
                    if re.search(r'["\']?(?:off|0|false)["\']?\b', stripped, re.IGNORECASE):
                        drc_fenced_off = True
                        fence_mechanisms.append(f"Runtime amixer fence ({rel}:{lno}: {stripped[:100]})")
                    elif re.search(r'["\']?(?:on|1|true)["\']?\b', stripped, re.IGNORECASE):
                        drc_enabled_hits.append(f"{rel}:{lno}: {stripped[:140]}")

        if drc_enabled_hits:
            drc_status = "FAIL"
            drc_msg = f"CRITICAL: IPC4 config enables 'Post Mixer Speaker Playback DRC switch' (+13.7 dB hazard): {drc_enabled_hits}"
        elif drc_fenced_off:
            drc_status = "PASS"
            drc_msg = f"IPC4 configs explicitly fence 'Post Mixer Speaker Playback DRC switch' OFF ({len(fence_mechanisms)} fence layer(s) verified)."
        else:
            drc_status = "FAIL"
            drc_msg = "IPC4 configs exist under kernel-driver/sof-ipc4/ without explicitly fencing 'Post Mixer Speaker Playback DRC switch' OFF!"

        checks.append(
            make_check(
                "G4.3_ipc4_drc_switch_fence",
                "IPC4 'Post Mixer Speaker Playback DRC switch' Fenced OFF",
                drc_status,
                drc_msg,
                {
                    "ipc4_files": [str(f.relative_to(workspace)) for f in ipc4_files],
                    "fence_mechanisms": fence_mechanisms,
                    "matches": drc_evidence,
                    "enabled_violations": drc_enabled_hits,
                },
            )
        )

    # 4.4 Regmap hang-file prohibition check across kernel-driver/
    regmap_hang_hits: list[str] = []
    excluded_mutant_fixtures: list[str] = []
    for fpath in all_text_files:
        if fpath.suffix == ".md":
            continue
        rel = str(fpath.relative_to(workspace))
        if "mutant" in fpath.name.lower():
            excluded_mutant_fixtures.append(rel)
            continue
        txt = fpath.read_text(encoding="utf-8", errors="replace")
        for lno, line in enumerate(txt.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            if re.search(r"/sys/kernel/debug/regmap/[^\s'\"]*/(?:access|range)\b", stripped):
                regmap_hang_hits.append(f"{rel}:{lno}: {stripped[:140]}")

    checks.append(
        make_check(
            "G4.4_no_regmap_hang_reads",
            "No Reads of /sys/kernel/debug/regmap/*/{access,range}",
            "PASS" if not regmap_hang_hits else "FAIL",
            f"Scanned {len(all_text_files) - len(excluded_mutant_fixtures)} file(s) under kernel-driver/: zero unsafe regmap access/range reads."
            if not regmap_hang_hits
            else f"CRITICAL: Unsafe regmap access/range read detected: {regmap_hang_hits}",
            {"violations": regmap_hang_hits, "excluded_mutation_test_fixtures": excluded_mutant_fixtures},
        )
    )

    status, counts = summarize_checks(checks)
    return {"title": "Check Group 4 — Kernel Driver Forbidden-Register & IPC4 Static Audit", "status": status, "counts": counts, "checks": checks}


# ---------------------------------------------------------------------------
# Check Group 5 — Thermal & Real-Time CPU Budget Verification
# ---------------------------------------------------------------------------

def read_package_temp_c() -> dict[str, Any]:
    """Safely read x86 Package id 0 temperature via /sys/class/thermal or sensors."""
    result: dict[str, Any] = {"source": None, "package_temp_c": None}
    thermal_root = Path("/sys/class/thermal")
    if thermal_root.exists():
        for tz in sorted(thermal_root.glob("thermal_zone*")):
            try:
                t_type = (tz / "type").read_text(encoding="utf-8").strip()
                if t_type == "x86_pkg_temp":
                    milli_c = int((tz / "temp").read_text(encoding="utf-8").strip())
                    result["source"] = str(tz / "temp")
                    result["package_temp_c"] = round(milli_c / 1000.0, 2)
                    return result
            except Exception:
                continue
    try:
        proc = subprocess.run(["sensors"], capture_output=True, text=True, timeout=5, check=False)
        m = re.search(r"Package id 0:\s*\+?([0-9]+(?:\.[0-9]+)?)°C", proc.stdout)
        if m:
            result["source"] = "sensors"
            result["package_temp_c"] = float(m.group(1))
    except Exception:
        pass
    return result


def _pin_to_ecores() -> list[int]:
    try:
        available = sorted(os.sched_getaffinity(0))
        ecores = [c for c in range(12, 20) if c in available]
        if ecores:
            os.sched_setaffinity(0, set(ecores))
            return ecores
        return available
    except Exception:
        return []


def _run_ctypes_dsp_benchmark(so_path: Path) -> dict[str, Any]:
    """
    Pin to E-cores (12-19) and run an offline 48 kHz stereo real-time CPU + numerical
    stability benchmark across all 4 modes of libxps17_studio_dsp.so.
    """
    pinned_cores = _pin_to_ecores()
    lib = ctypes.CDLL(str(so_path))
    lib.ladspa_descriptor.argtypes = [ctypes.c_ulong]
    lib.ladspa_descriptor.restype = ctypes.POINTER(LADSPA_Descriptor)
    desc_p = lib.ladspa_descriptor(0)
    if not desc_p:
        raise RuntimeError("ladspa_descriptor(0) returned NULL")
    d = desc_p.contents

    sample_rate = 48000
    block_size = 256  # 5.333 ms PipeWire quantum
    warmup_blocks = 32
    bench_blocks_per_mode = 750  # 750 * 256 / 48000 = 4.0 seconds per mode

    # Build rich multi-tone + transient synthetic stereo test signal
    in_l_buf = (ctypes.c_float * block_size)()
    in_r_buf = (ctypes.c_float * block_size)()
    out_l_buf = (ctypes.c_float * block_size)()
    out_r_buf = (ctypes.c_float * block_size)()
    for i in range(block_size):
        t = i / sample_rate
        s_l = (
            0.28 * math.sin(2.0 * math.pi * 45.0 * t)
            + 0.24 * math.sin(2.0 * math.pi * 110.0 * t)
            + 0.18 * math.sin(2.0 * math.pi * 440.0 * t)
            + 0.14 * math.sin(2.0 * math.pi * 2400.0 * t)
            + 0.10 * math.sin(2.0 * math.pi * 9500.0 * t)
        )
        s_r = (
            0.28 * math.sin(2.0 * math.pi * 45.0 * t + 0.3)
            + 0.24 * math.sin(2.0 * math.pi * 110.0 * t + 0.5)
            + 0.18 * math.sin(2.0 * math.pi * 660.0 * t)
            + 0.14 * math.sin(2.0 * math.pi * 3100.0 * t)
            + 0.10 * math.sin(2.0 * math.pi * 10500.0 * t)
        )
        in_l_buf[i] = s_l
        in_r_buf[i] = s_r

    # Map port indices
    port_indices: dict[str, int] = {}
    ctrl_out_indices: list[int] = []
    for i in range(int(d.PortCount)):
        pdesc = int(d.PortDescriptors[i])
        pname = d.PortNames[i].decode("utf-8", errors="replace")
        port_indices[pname] = i
        if (pdesc & LADSPA_PORT_CONTROL) and (pdesc & LADSPA_PORT_OUTPUT):
            ctrl_out_indices.append(i)

    handle = d.instantiate(desc_p, sample_rate)
    if not handle:
        raise RuntimeError("instantiate() returned NULL")

    ctrl_vars: dict[str, ctypes.c_float] = {}
    out_ctrl_vars: list[ctypes.c_float] = [ctypes.c_float(0.0) for _ in ctrl_out_indices]

    try:
        d.connect_port(handle, port_indices["In L"], in_l_buf)
        d.connect_port(handle, port_indices["In R"], in_r_buf)
        d.connect_port(handle, port_indices["Out L"], out_l_buf)
        d.connect_port(handle, port_indices["Out R"], out_r_buf)
        for spec in EXPECTED_CONTROL_PORTS:
            v = ctypes.c_float(spec["default"])
            ctrl_vars[spec["name"]] = v
            d.connect_port(handle, port_indices[spec["name"]], ctypes.byref(v))
        for idx, cvar in zip(ctrl_out_indices, out_ctrl_vars):
            d.connect_port(handle, idx, ctypes.byref(cvar))
        if d.activate:
            d.activate(handle)

        mode_results: dict[str, Any] = {}
        worst_cpu_pct = 0.0
        total_non_finite = 0

        for mode_id, mode_name in ((0, "deep"), (1, "spatial"), (2, "reference"), (3, "voice")):
            ctrl_vars["Mode"].value = float(mode_id)
            ctrl_vars["Spatial XTC"].value = 0.75 if mode_id == 1 else 0.0
            ctrl_vars["Drive (dB)"].value = 7.0
            ctrl_vars["Ceiling (dBFS)"].value = -1.0
            ctrl_vars["Volume (dB)"].value = -12.0

            for _ in range(warmup_blocks):
                d.run(handle, block_size)

            block_times_us: list[float] = []
            max_abs_sample = 0.0
            mode_non_finite = 0

            t_start = time.perf_counter()
            for _ in range(bench_blocks_per_mode):
                bt0 = time.perf_counter()
                d.run(handle, block_size)
                bt1 = time.perf_counter()
                block_times_us.append((bt1 - bt0) * 1e6)
                # Inspect samples every 25th block + final block (and quick edge checks)
                for s in out_l_buf:
                    if not math.isfinite(s):
                        mode_non_finite += 1
                    elif abs(s) > max_abs_sample:
                        max_abs_sample = abs(s)
                for s in out_r_buf:
                    if not math.isfinite(s):
                        mode_non_finite += 1
                    elif abs(s) > max_abs_sample:
                        max_abs_sample = abs(s)
            t_end = time.perf_counter()

            # Use pure DSP run time (sum of block_times_us) to exclude Python sample-check loop overhead!
            pure_dsp_sec = sum(block_times_us) * 1e-6
            simulated_audio_sec = (bench_blocks_per_mode * block_size) / float(sample_rate)
            cpu_pct = (pure_dsp_sec / simulated_audio_sec) * 100.0
            block_times_us.sort()
            p50_us = block_times_us[len(block_times_us) // 2]
            p99_us = block_times_us[int(len(block_times_us) * 0.99)]
            max_us = block_times_us[-1]
            worst_cpu_pct = max(worst_cpu_pct, cpu_pct)
            total_non_finite += mode_non_finite

            mode_results[mode_name] = {
                "cpu_percent_1_ecore": round(cpu_pct, 4),
                "p50_us": round(p50_us, 2),
                "p99_us": round(p99_us, 2),
                "max_us": round(max_us, 2),
                "quantum_budget_us": round((block_size / sample_rate) * 1e6, 2),
                "peak_sample_abs": round(max_abs_sample, 6),
                "non_finite_samples": mode_non_finite,
                "wall_with_checks_sec": round(t_end - t_start, 3),
            }

        return {
            "pinned_ecores": pinned_cores,
            "sample_rate": sample_rate,
            "block_size": block_size,
            "worst_cpu_percent": round(worst_cpu_pct, 4),
            "total_non_finite_samples": total_non_finite,
            "modes": mode_results,
        }
    finally:
        if d.deactivate:
            d.deactivate(handle)
        d.cleanup(handle)


def run_group5_thermal_rt_cpu(workspace: Path, abi_ctx: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    # 5.1 Package thermal check (< 82°C fail, < 78°C pass, 78-82°C warn)
    temp_info = read_package_temp_c()
    pkg_c = temp_info.get("package_temp_c")
    if pkg_c is None:
        checks.append(
            make_check(
                "G5.1_package_temperature",
                "CPU Package Thermal Envelope (< 82°C)",
                "WARN",
                "Unable to read CPU Package id 0 temperature.",
                temp_info,
            )
        )
    else:
        if pkg_c >= TEMP_FAIL_C:
            t_status = "FAIL"
            t_msg = f"Package temperature {pkg_c:.1f}°C >= {TEMP_FAIL_C:.0f}°C thermal limit!"
        elif pkg_c >= TEMP_WARN_C:
            t_status = "WARN"
            t_msg = f"Package temperature {pkg_c:.1f}°C is elevated (warn >= {TEMP_WARN_C:.0f}°C, limit {TEMP_FAIL_C:.0f}°C)."
        else:
            t_status = "PASS"
            t_msg = f"Package temperature {pkg_c:.1f}°C is cool and well under the {TEMP_FAIL_C:.0f}°C safety ceiling."
        checks.append(
            make_check(
                "G5.1_package_temperature",
                "CPU Package Thermal Envelope (< 82°C)",
                t_status,
                t_msg,
                temp_info,
            )
        )

    # 5.2 xps17-studio-cli bench (if present)
    cli_path = workspace / "dsp-engine" / "bin" / "xps17-studio-cli"
    cli_cpu_pct: float | None = None
    if cli_path.exists() and os.access(cli_path, os.X_OK):
        try:
            proc = subprocess.run(
                ["taskset", "-c", "12-19", "nice", "-n", "19", str(cli_path), "bench"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            out = (proc.stdout + "\n" + proc.stderr).strip()
            m_cpu = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%\s*(?:CPU|of|core|e-?core|@)?", out, re.IGNORECASE)
            if m_cpu:
                cli_cpu_pct = float(m_cpu.group(1))
            if proc.returncode != 0:
                checks.append(
                    make_check(
                        "G5.2_cli_bench",
                        "xps17-studio-cli bench (E-core Pinned)",
                        "FAIL",
                        f"xps17-studio-cli bench exited with code {proc.returncode}.",
                        {"path": str(cli_path), "returncode": proc.returncode, "output": out[-1500:]},
                    )
                )
            else:
                if cli_cpu_pct is not None and cli_cpu_pct > CPU_FAIL_PCT:
                    c_status = "FAIL"
                    c_msg = f"xps17-studio-cli bench reported {cli_cpu_pct:.3f}% CPU (> {CPU_FAIL_PCT}% limit)."
                elif cli_cpu_pct is not None and cli_cpu_pct > CPU_WARN_PCT:
                    c_status = "WARN"
                    c_msg = f"xps17-studio-cli bench reported {cli_cpu_pct:.3f}% CPU (> {CPU_WARN_PCT}% warn, < {CPU_FAIL_PCT}% limit)."
                else:
                    c_status = "PASS"
                    c_msg = f"xps17-studio-cli bench succeeded cleanly (parsed CPU={cli_cpu_pct}%)."
                checks.append(
                    make_check(
                        "G5.2_cli_bench",
                        "xps17-studio-cli bench (E-core Pinned)",
                        c_status,
                        c_msg,
                        {"path": str(cli_path), "parsed_cpu_pct": cli_cpu_pct, "output": out[-1500:]},
                    )
                )
        except Exception as e:
            checks.append(
                make_check(
                    "G5.2_cli_bench",
                    "xps17-studio-cli bench (E-core Pinned)",
                    "FAIL",
                    f"Failed executing xps17-studio-cli bench: {e}",
                    {"path": str(cli_path), "error": str(e)},
                )
            )
    else:
        checks.append(
            make_check(
                "G5.2_cli_bench",
                "xps17-studio-cli bench (E-core Pinned)",
                "PENDING",
                f"Standalone CLI not yet built at {cli_path} (awaiting Lead 3 dsp-engine).",
                {"path": str(cli_path)},
            )
        )

    # 5.3 Independent ctypes dlopen 48 kHz RT CPU & zero NaN/Inf verification
    so_path = Path(abi_ctx["so_path"]) if abi_ctx.get("so_path") else (workspace / "dsp-engine/lib/libxps17_studio_dsp.so")
    if not so_path.exists():
        checks.append(
            make_check(
                "G5.3_ctypes_rt_cpu_and_nan",
                "Independent E-Core dlopen RT CPU (< 1.5%) & Zero NaN/Inf Audit",
                "PENDING",
                f"Plugin binary not yet present at {so_path} (awaiting Lead 3 dsp-engine).",
                {"path": str(so_path)},
            )
        )
    elif not abi_ctx.get("so_valid"):
        checks.append(
            make_check(
                "G5.3_ctypes_rt_cpu_and_nan",
                "Independent E-Core dlopen RT CPU (< 1.5%) & Zero NaN/Inf Audit",
                "FAIL",
                "Skipped runtime DSP benchmark because LADSPA ABI/port contract failed in Group 2.",
                {"path": str(so_path)},
            )
        )
    else:
        try:
            bench = _run_ctypes_dsp_benchmark(so_path)
            worst_cpu = bench["worst_cpu_percent"]
            non_fin = bench["total_non_finite_samples"]
            if non_fin > 0:
                b_status = "FAIL"
                b_msg = f"CRITICAL: DSP emitted {non_fin} NaN/Inf sample(s) during 48 kHz test!"
            elif worst_cpu > CPU_FAIL_PCT:
                b_status = "FAIL"
                b_msg = f"Worst-mode E-core CPU {worst_cpu:.3f}% exceeds {CPU_FAIL_PCT:.1f}% real-time budget!"
            elif worst_cpu > CPU_WARN_PCT:
                b_status = "WARN"
                b_msg = f"Worst-mode E-core CPU {worst_cpu:.3f}% is between {CPU_WARN_PCT:.1f}% warn and {CPU_FAIL_PCT:.1f}% limit; zero NaN/Inf."
            else:
                b_status = "PASS"
                b_msg = f"All 4 modes verified on E-cores: worst CPU {worst_cpu:.3f}% (< {CPU_WARN_PCT:.1f}%), zero NaN/Inf samples."
            checks.append(
                make_check(
                    "G5.3_ctypes_rt_cpu_and_nan",
                    "Independent E-Core dlopen RT CPU (< 1.5%) & Zero NaN/Inf Audit",
                    b_status,
                    b_msg,
                    bench,
                )
            )
        except Exception as e:
            checks.append(
                make_check(
                    "G5.3_ctypes_rt_cpu_and_nan",
                    "Independent E-Core dlopen RT CPU (< 1.5%) & Zero NaN/Inf Audit",
                    "FAIL",
                    f"Runtime ctypes benchmark crashed/failed: {e}",
                    {"path": str(so_path), "error": str(e)},
                )
            )

    # 5.4 Post-benchmark package thermal check
    post_temp = read_package_temp_c()
    post_c = post_temp.get("package_temp_c")
    if post_c is not None and post_c >= TEMP_FAIL_C:
        checks.append(
            make_check(
                "G5.4_post_bench_thermal",
                "Post-Audit Package Temperature (< 82°C)",
                "FAIL",
                f"Post-audit package temperature rose to {post_c:.1f}°C >= {TEMP_FAIL_C:.0f}°C!",
                post_temp,
            )
        )
    elif post_c is not None:
        checks.append(
            make_check(
                "G5.4_post_bench_thermal",
                "Post-Audit Package Temperature (< 82°C)",
                "PASS" if post_c < TEMP_WARN_C else "WARN",
                f"Post-audit package temperature remained at {post_c:.1f}°C (< {TEMP_FAIL_C:.0f}°C).",
                post_temp,
            )
        )

    status, counts = summarize_checks(checks)
    return {"title": "Check Group 5 — Thermal & Real-Time CPU Budget Verification", "status": status, "counts": counts, "checks": checks}


# ---------------------------------------------------------------------------
# Report Renderers (JSON + Markdown)
# ---------------------------------------------------------------------------

def render_markdown(report: dict[str, Any]) -> str:
    s = report["summary"]
    c = s["counts"]
    lines: list[str] = [
        "# Dell XPS 17 9720 Studio Audio — Safety, Mic-Privacy, Hardware-Protection & Thermal Audit",
        "",
        f"- **Generated (UTC):** `{report['generated_at_utc']}`",
        f"- **Workspace:** `{report['workspace']}`",
        f"- **Inspected Home (Read-Only):** `{report['target_home']}`",
        f"- **Overall Status:** **`{s['overall_status']}`**",
        "",
        "## Executive Summary",
        "",
        "| Status | Count | Meaning |",
        "| :--- | ---: | :--- |",
        f"| **PASS** | {c['PASS']} | Verified compliant with safety, privacy, ABI, and thermal contracts |",
        f"| **WARN** | {c['WARN']} | Non-fatal advisory (e.g., elevated CPU/temp within hard limit) |",
        f"| **FAIL** | {c['FAIL']} | Hard safety, privacy, ABI, register, or budget violation |",
        f"| **PENDING** | {c['PENDING']} | Upstream artifact not yet landed (Lead 1 / Lead 2 / Lead 3 / Integrator) |",
        f"| **TOTAL** | {s['total_checks']} | Machine-verifiable audit checks executed |",
        "",
        "## Check Group Breakdown",
        "",
        "| Group | Status | PASS | WARN | FAIL | PENDING |",
        "| :--- | :---: | ---: | ---: | ---: | ---: |",
    ]
    for gkey, g in report["groups"].items():
        gc = g["counts"]
        lines.append(
            f"| {g['title']} | **{g['status']}** | {gc['PASS']} | {gc['WARN']} | {gc['FAIL']} | {gc['PENDING']} |"
        )

    lines.append("")
    for gkey, g in report["groups"].items():
        lines.append(f"## {g['title']} (`{g['status']}`)")
        lines.append("")
        lines.append("| Check ID | Check | Status | Details |")
        lines.append("| :--- | :--- | :---: | :--- |")
        for chk in g["checks"]:
            msg = chk["message"].replace("|", "\\|").replace("\n", " ")
            lines.append(f"| `{chk['id']}` | {chk['name']} | **{chk['status']}** | {msg} |")
        lines.append("")

    # Surface SHA256 fingerprints of live safety guards for tamper-evident comparison
    g3 = report["groups"].get("group3_privacy_bt_loudness", {})
    guard_rows = []
    for chk in g3.get("checks", []):
        ev = chk.get("evidence", {})
        if "sha256" in ev and "path" in ev:
            guard_rows.append((chk["id"], ev["path"], ev["sha256"]))
    if guard_rows:
        lines.append("## Live Safety Guard SHA256 Fingerprints (Read-Only)")
        lines.append("")
        lines.append("| Guard | Path | SHA256 |")
        lines.append("| :--- | :--- | :--- |")
        for gid, gpath, gsha in guard_rows:
            lines.append(f"| `{gid}` | `{gpath}` | `{gsha}` |")
        lines.append("")

    lines.append("## Standardized Runtime Asset Paths")
    lines.append("")
    lines.append("- LADSPA plugin: `$TARGET_HOME/.local/share/xps17-studio-audio/lib/libxps17_studio_dsp.so`")
    lines.append("- FIR kernels: `$TARGET_HOME/.local/share/xps17-studio-audio/kernels/xps17-studio-{deep,spatial,reference,voice}.wav`")
    lines.append("- PipeWire graph: `$TARGET_HOME/.config/pipewire/omarchy-speaker-engine.conf.d/92-xps17-studio.conf`")
    lines.append("- Pre-deploy backups: `$TARGET_HOME/.local/state/xps17-studio-audio/backups/<timestamp>/`")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Safety, Mic-Privacy, Hardware-Protection & Thermal Auditor for XPS 17 9720 Studio Audio"
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("/home/akshaybapat/project/xps17-studio-audio"),
        help="Path to xps17-studio-audio workspace",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path.home(),
        help="Target HOME directory for read-only live guard inspection",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Output path for JSON audit report (default: <workspace>/verify-integrate/audit/audit_report.json)",
    )
    parser.add_argument(
        "--md-out",
        type=Path,
        default=None,
        help="Output path for Markdown summary (default: <workspace>/verify-integrate/audit/AUDIT-SUMMARY.md)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any check is FAIL or PENDING (default: exit non-zero only on FAIL)",
    )
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    home_dir = args.home.resolve()
    audit_dir = workspace / "verify-integrate" / "audit"
    json_out = args.json_out if args.json_out is not None else (audit_dir / "audit_report.json")
    md_out = args.md_out if args.md_out is not None else (audit_dir / "AUDIT-SUMMARY.md")

    g1, g1_ctx = run_group1_pipewire_graph(workspace)
    g2, abi_ctx = run_group2_ladspa_abi_and_kernels(workspace, g1_ctx)
    g3 = run_group3_privacy_bt_loudness(workspace, home_dir, g1_ctx)
    g4 = run_group4_kernel_driver_safety(workspace)
    g5 = run_group5_thermal_rt_cpu(workspace, abi_ctx)

    groups = {
        "group1_pipewire_graph": g1,
        "group2_ladspa_abi_and_kernels": g2,
        "group3_privacy_bt_loudness": g3,
        "group4_kernel_driver_safety": g4,
        "group5_thermal_rt_cpu": g5,
    }

    all_checks = [chk for g in groups.values() for chk in g["checks"]]
    overall_status, overall_counts = summarize_checks(all_checks)

    report = {
        "schema_version": "1.0.0",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "workspace": str(workspace),
        "target_home": str(home_dir),
        "summary": {
            "overall_status": overall_status,
            "counts": overall_counts,
            "total_checks": len(all_checks),
        },
        "groups": groups,
    }

    json_out.parent.mkdir(parents=True, exist_ok=True)
    md_out.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write for audit reports
    tmp_json = json_out.with_suffix(f".json.tmp.{os.getpid()}")
    tmp_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_json, json_out)

    tmp_md = md_out.with_suffix(f".md.tmp.{os.getpid()}")
    tmp_md.write_text(render_markdown(report), encoding="utf-8")
    os.replace(tmp_md, md_out)

    print(
        f"[xps17-safety-audit] overall={overall_status} "
        f"PASS={overall_counts['PASS']} WARN={overall_counts['WARN']} "
        f"FAIL={overall_counts['FAIL']} PENDING={overall_counts['PENDING']} "
        f"(total={len(all_checks)})"
    )
    print(f"  JSON: {json_out}")
    print(f"  MD:   {md_out}")

    if overall_counts["FAIL"] > 0:
        return 1
    if args.strict and overall_counts["PENDING"] > 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Dell XPS 17 9720 (SUBSYS 0AFF1028 / Stradale) Silicon & Windows Audio Table Analyzer.

Strictly read-only offline analyzer over `/home/akshaybapat/project/xps17-speaker-stack/`.
Generates `/home/akshaybapat/project/xps17-studio-audio/kernel-driver/audit/waves_stradale_summary.json`
and prints a verified engineering summary to stdout.

Run on E-cores:
  taskset -c 12-19 nice -n 19 python3 analyze_windows_tables.py
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import struct
import sys
from pathlib import Path
from typing import Any

STACK_ROOT = Path("/home/akshaybapat/project/xps17-speaker-stack")
OUT_DIR = Path("/home/akshaybapat/project/xps17-studio-audio/kernel-driver/audit")
OUT_JSON = OUT_DIR / "waves_stradale_summary.json"

WIN_TABLES = STACK_ROOT / "work/win/tables"
WIN_CODEC_LL = STACK_ROOT / "work/win/dec/codec_tree/Realtek/SDCA_06_Alc1316_AggrLL_SM_0AFE1028"
WIN_CODEC_RR = STACK_ROOT / "work/win/dec/codec_tree/Realtek/SDCA_06_Alc1316_AggrRR_SM_0AFE1028"
WIN_MAXX_TREE = STACK_ROOT / "work/win/dec/maxx/tree"
DIFF_DIR = STACK_ROOT / "diff"
AMP_DUMPS = STACK_ROOT / "work/amp/dumps"
COEFF_HEADER = DIFF_DIR / "dl/kernelorg-v7.2.15/sound_soc_sdw_utils_soc_sdw_rt_amp_coeff_tables.h"


# ---------------------------------------------------------------------------
# 1. Helper math for RT1316 Q5.27 biquads & Waves RBJ PEQ response
# ---------------------------------------------------------------------------

def rbj_biquad(kind: int, f0: float, q: float, fs: float) -> list[float]:
    """RBJ cookbook 2nd-order filter: kind=1 HPF, kind=2 LPF -> [b0, b1, b2, a1, a2]."""
    w = 2.0 * math.pi * f0 / fs
    al = math.sin(w) / (2.0 * q)
    c = math.cos(w)
    a0 = 1.0 + al
    if kind == 2:
        b = [(1.0 - c) / 2.0, 1.0 - c, (1.0 - c) / 2.0]
    elif kind == 1:
        b = [(1.0 + c) / 2.0, -(1.0 + c), (1.0 + c) / 2.0]
    else:
        raise ValueError(f"Unsupported biquad kind: {kind}")
    return [x / a0 for x in b] + [-2.0 * c / a0, (1.0 - al) / a0]


def encode_q5_27(val: float) -> list[int]:
    v = int(round(val * (1 << 27))) & 0xFFFFFFFF
    return [(v >> s) & 0xFF for s in (24, 16, 8, 0)]


def decode_q5_27(bs: list[int]) -> float:
    v = int.from_bytes(bytes(bs), "big")
    if v & 0x80000000:
        v -= 1 << 32
    return v / float(1 << 27)


def biquad_mag_db(coeffs: list[float], freq_hz: float, fs: float) -> float:
    """Compute magnitude response in dB for y = b0 x + b1 x1 + b2 x2 - a1 y1 - a2 y2."""
    b0, b1, b2, a1, a2 = coeffs
    w = 2.0 * math.pi * freq_hz / fs
    z1 = complex(math.cos(w), -math.sin(w))
    z2 = complex(math.cos(2.0 * w), -math.sin(2.0 * w))
    num = b0 + b1 * z1 + b2 * z2
    den = 1.0 + a1 * z1 + a2 * z2
    mag = abs(num / den)
    return -120.0 if mag < 1e-6 else 20.0 * math.log10(mag)


def biquad_cascade_stats(stages: list[list[float]], fs: float) -> dict[str, Any]:
    """Evaluate cascaded stages over dense log-grid + key frequencies."""
    key_freqs = [
        0.0, 10.0, 18.0, 20.0, 31.0, 50.0, 100.0, 200.0, 500.0, 800.0,
        838.0, 1000.0, 1088.0, 1470.0, 2000.0, 2970.0, 5000.0, 5442.0,
        6160.0, 8000.0, 10000.0, 12000.0, 16000.0, 20000.0, fs / 2.0,
    ]
    resp_db = {}
    for f in key_freqs:
        total = sum(biquad_mag_db(c, f, fs) for c in stages)
        resp_db[f"{f:g}Hz"] = round(total, 3)

    # Dense log sweep from 5 Hz to Nyquist - 1 Hz to locate peak and -3 dB corners
    n_pts = 4000
    f_min, f_max = 5.0, fs / 2.0 - 1.0
    freqs = [f_min * ((f_max / f_min) ** (i / (n_pts - 1))) for i in range(n_pts)]
    mags = [sum(biquad_mag_db(c, f, fs) for c in stages) for f in freqs]
    peak_idx = max(range(n_pts), key=lambda i: mags[i])
    peak_db = mags[peak_idx]
    peak_hz = freqs[peak_idx]

    # Find -3 dB crossing points (rising and/or falling)
    crossings = []
    for i in range(1, n_pts):
        if (mags[i - 1] < -3.0 <= mags[i]) or (mags[i - 1] >= -3.0 > mags[i]):
            frac = (-3.0 - mags[i - 1]) / (mags[i] - mags[i - 1])
            fc = freqs[i - 1] + frac * (freqs[i] - freqs[i - 1])
            direction = "HPF_rising" if mags[i] > mags[i - 1] else "LPF_falling"
            crossings.append({"hz": round(fc, 2), "edge": direction})

    return {
        "response_db": resp_db,
        "peak_gain_db": round(peak_db, 4),
        "peak_freq_hz": round(peak_hz, 2),
        "minus_3db_crossings": crossings,
    }


def waves_peq_band_mag_db(band_type: int, freq: float, gain_db: float, q: float, f_eval: float, fs: float = 48000.0) -> float:
    """RBJ approximation for Waves PEQ band types (0=peak, 1=lowshelf, 2=highshelf, 4=HPF)."""
    if freq <= 0.0 or freq >= fs / 2.0 or q <= 0.0:
        return 0.0
    w0 = 2.0 * math.pi * freq / fs
    c = math.cos(w0)
    s = math.sin(w0)
    if band_type == 0:  # Peaking EQ
        if abs(gain_db) < 1e-6:
            return 0.0
        A = 10.0 ** (gain_db / 40.0)
        al = s / (2.0 * q)
        b0, b1, b2 = 1.0 + al * A, -2.0 * c, 1.0 - al * A
        a0, a1, a2 = 1.0 + al / A, -2.0 * c, 1.0 - al / A
    elif band_type == 1:  # Low shelf
        if abs(gain_db) < 1e-6:
            return 0.0
        A = 10.0 ** (gain_db / 40.0)
        al = s / (2.0 * q)
        t = 2.0 * math.sqrt(A) * al
        b0 = A * ((A + 1.0) - (A - 1.0) * c + t)
        b1 = 2.0 * A * ((A - 1.0) - (A + 1.0) * c)
        b2 = A * ((A + 1.0) - (A - 1.0) * c - t)
        a0 = (A + 1.0) + (A - 1.0) * c + t
        a1 = -2.0 * ((A - 1.0) + (A + 1.0) * c)
        a2 = (A + 1.0) + (A - 1.0) * c - t
    elif band_type == 2:  # High shelf
        if abs(gain_db) < 1e-6:
            return 0.0
        A = 10.0 ** (gain_db / 40.0)
        al = s / (2.0 * q)
        t = 2.0 * math.sqrt(A) * al
        b0 = A * ((A + 1.0) + (A - 1.0) * c + t)
        b1 = -2.0 * A * ((A - 1.0) + (A + 1.0) * c)
        b2 = A * ((A + 1.0) + (A - 1.0) * c - t)
        a0 = (A + 1.0) - (A - 1.0) * c + t
        a1 = 2.0 * ((A - 1.0) - (A + 1.0) * c)
        a2 = (A + 1.0) - (A - 1.0) * c - t
    elif band_type == 4:  # 2nd-order HPF
        al = s / (2.0 * q)
        b0, b1, b2 = (1.0 + c) / 2.0, -(1.0 + c), (1.0 + c) / 2.0
        a0, a1, a2 = 1.0 + al, -2.0 * c, 1.0 - al
    else:
        return 0.0
    return biquad_mag_db([b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0], f_eval, fs)


# ---------------------------------------------------------------------------
# 2. Parse Linux dell_0b00_bq_params, live dumps, and Windows codec scripts
# ---------------------------------------------------------------------------

def parse_dell_bq_table() -> list[tuple[int, int]]:
    text = COEFF_HEADER.read_text()
    m = re.search(r"dell_0b00_bq_params\[\]\s*=\s*\{([^}]+)\};", text, re.S)
    assert m, "dell_0b00_bq_params not found"
    bs = [int(h, 16) for h in re.findall(r"0x[0-9a-fA-F]+", m.group(1))]
    assert len(bs) % 3 == 0
    return [(bs[i] | (bs[i + 1] << 8), bs[i + 2]) for i in range(0, len(bs), 3)]


def parse_reg_dump(path: Path) -> dict[int, tuple[int, int]]:
    out: dict[int, tuple[int, int]] = {}
    with path.open() as f:
        for ln, line in enumerate(f, 1):
            m = re.match(r"^([0-9a-fA-F]+):\s+([0-9a-fA-F]+)", line.strip())
            if m:
                out[int(m.group(1), 16)] = (ln, int(m.group(2), 16))
    return out


def parse_sdca_script_writes(path: Path) -> list[dict[str, Any]]:
    writes = []
    with path.open(errors="replace") as f:
        for ln, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("//"):
                continue
            m = re.match(r"^sdca_w\s+(0x[0-9a-fA-F]+)\s+(\d+)\s+(0x[0-9a-fA-F]+|[0-9a-fA-F]+|payload)\s*(?://(.*))?$", line)
            if m:
                addr = int(m.group(1), 16)
                val_s = m.group(3)
                val = int(val_s, 16) if val_s != "payload" else None
                writes.append({"line": ln, "addr": addr, "val": val, "raw_val": val_s, "comment": (m.group(4) or "").strip()})
    return writes


def analyze_alc1316_hardware() -> dict[str, Any]:
    bq_writes = parse_dell_bq_table()
    bq_map = dict(bq_writes)
    amp1 = parse_reg_dump(AMP_DUMPS / "amp1.registers.txt")
    amp2 = parse_reg_dump(AMP_DUMPS / "amp2.registers.txt")

    # Verify Windows CODEC_Init.txt (LL & RR)
    ll_writes = parse_sdca_script_writes(WIN_CODEC_LL / "CODEC_Init.txt")
    rr_writes = parse_sdca_script_writes(WIN_CODEC_RR / "CODEC_Init.txt")

    ll_final = {w["addr"]: w["val"] for w in ll_writes if w["val"] is not None}
    rr_final = {w["addr"]: w["val"] for w in rr_writes if w["val"] is not None}
    ll_vs_rr = {}
    for addr in sorted(set(ll_final) | set(rr_final)):
        if ll_final.get(addr) != rr_final.get(addr):
            ll_vs_rr[f"0x{addr:04X}"] = {
                "LL_amp1": f"0x{ll_final[addr]:02X}" if addr in ll_final else None,
                "RR_amp2": f"0x{rr_final[addr]:02X}" if addr in rr_final else None,
            }

    # Check 134-write Dell table vs Windows CODEC_Init.txt & live amp1/amp2
    bq_matches_win = all(ll_final.get(r) == v and rr_final.get(r) == v for r, v in bq_writes)
    bq_matches_amp1 = sum(1 for r, v in bq_writes if amp1.get(r, (0, -1))[1] == v)
    bq_matches_amp2 = sum(1 for r, v in bq_writes if amp2.get(r, (0, -1))[1] == v)

    # Decode the 4 biquads (SPOL BQ1/BQ2, SPOR BQ1/BQ2)
    bq_specs = [
        ("SPOL_BQ1_woofer_LPF", 0xC203, 0xC380, "SPOL (Left/Woofer)", "BQ1"),
        ("SPOL_BQ2_subsonic_HPF", 0xC217, 0xC392, "SPOL (Left/Woofer)", "BQ2"),
        ("SPOR_BQ1_tweeter_HPF", 0xC22B, 0xC389, "SPOR (Right/Tweeter)", "BQ1"),
        ("SPOR_BQ2_subsonic_HPF", 0xC23F, 0xC39B, "SPOR (Right/Tweeter)", "BQ2"),
    ]
    biquad_stages: dict[str, Any] = {}
    spol_44k_coeffs, spor_44k_coeffs = [], []
    spol_48k_coeffs, spor_48k_coeffs = [], []

    for key, base_reg, ui_reg, channel, stage_name in bq_specs:
        raw_bytes = [bq_map[base_reg + i] for i in range(20)]
        words_hex = ["".join(f"{b:02x}" for b in raw_bytes[k : k + 4]) for k in range(0, 20, 4)]
        coeffs = [decode_q5_27(raw_bytes[k : k + 4]) for k in range(0, 20, 4)]

        # Parse UI metadata (Fc:u16, BW:u16, Q:u16, Gain:s16, Type:u8)
        ui_bytes = [bq_map[ui_reg + i] for i in range(9)]
        fc_ui = (ui_bytes[0] << 8) | ui_bytes[1]
        bw_ui = (ui_bytes[2] << 8) | ui_bytes[3]
        q_ui_field = (ui_bytes[4] << 8) | ui_bytes[5]
        gain_ui = struct.unpack(">h", bytes(ui_bytes[6:8]))[0]
        type_ui = ui_bytes[8]
        q_derived = fc_ui / float(bw_ui)

        # Verify RBJ bit-exactness at 44.1 kHz and compute 48 kHz corrected bytes
        rbj_44k = rbj_biquad(type_ui, fc_ui, q_derived, 44100.0)
        codes_44k = [int.from_bytes(bytes(encode_q5_27(x)), "big") for x in rbj_44k]
        codes_vend = [int(w, 16) for w in words_hex]
        max_lsb_diff_44k = max(abs(((a - b + (1 << 31)) % (1 << 32)) - (1 << 31)) for a, b in zip(codes_44k, codes_vend))

        rbj_48k = rbj_biquad(type_ui, fc_ui, q_derived, 48000.0)
        bytes_48k = [b for x in rbj_48k for b in encode_q5_27(x)]
        words_48k_hex = ["".join(f"{b:02x}" for b in bytes_48k[k : k + 4]) for k in range(0, 20, 4)]
        coeffs_48k_quant = [decode_q5_27(bytes_48k[k : k + 4]) for k in range(0, 20, 4)]

        if channel.startswith("SPOL"):
            spol_44k_coeffs.append(coeffs)
            spol_48k_coeffs.append(coeffs_48k_quant)
        else:
            spor_44k_coeffs.append(coeffs)
            spor_48k_coeffs.append(coeffs_48k_quant)

        biquad_stages[key] = {
            "channel": channel,
            "stage": stage_name,
            "coeff_reg_range": f"0x{base_reg:04X}..0x{base_reg + 19:04X}",
            "ui_reg_range": f"0x{ui_reg:04X}..0x{ui_reg + 8:04X}",
            "ui_design_fields": {
                "Fc_hz": fc_ui,
                "BW_hz": bw_ui,
                "Q_derived_Fc_over_BW": round(q_derived, 6),
                "Q_field_raw": q_ui_field,
                "Gain_field_raw_s16": gain_ui,
                "Type_enum": type_ui,
                "Type_name": "LPF" if type_ui == 2 else "HPF",
            },
            "shipped_44k1_designed": {
                "words_q5_27_hex": dict(zip(["b0", "b1", "b2", "a1", "a2"], words_hex)),
                "coeffs_float": {k: round(v, 8) for k, v in zip(["b0", "b1", "b2", "a1", "a2"], coeffs)},
                "rbj_44100Hz_max_lsb_error": max_lsb_diff_44k,
                "effective_fc_at_48kHz_hz": round(fc_ui * (48000.0 / 44100.0), 2),
                "single_stage_at_48kHz": biquad_cascade_stats([coeffs], 48000.0),
            },
            "corrected_48k_designed": {
                "words_q5_27_hex": dict(zip(["b0", "b1", "b2", "a1", "a2"], words_48k_hex)),
                "coeffs_float": {k: round(v, 8) for k, v in zip(["b0", "b1", "b2", "a1", "a2"], coeffs_48k_quant)},
                "single_stage_at_48kHz": biquad_cascade_stats([coeffs_48k_quant], 48000.0),
            },
        }

    # Verify pre/post vol words 0xC261..0xC270
    vol_words = {}
    for label, base in [("pre_vol_L", 0xC261), ("pre_vol_R", 0xC265), ("post_vol_L", 0xC269), ("post_vol_R", 0xC26D)]:
        bs = [bq_map[base + i] for i in range(4)]
        vol_words[label] = {
            "reg_range": f"0x{base:04X}..0x{base + 3:04X}",
            "hex": "".join(f"{b:02X}" for b in bs),
            "q5_27_linear": decode_q5_27(bs),
        }

    # Parse CSV tables for cross-verification
    init_csv_rows = list(csv.DictReader((WIN_TABLES / "alc1316_LL_0AFE1028_CODEC_Init.csv").open()))
    runtime_csv_rows = list(csv.DictReader((WIN_TABLES / "windows_runtime_writes_0AFF.csv").open()))
    vs_linux_rows = list(csv.DictReader((WIN_TABLES / "windows_vs_linux_vs_live_rt1316.csv").open()))
    vs_live_rows = list(csv.DictReader((WIN_TABLES / "windows_vs_live_amp_registers.csv").open()))

    # Gain staging math
    # D001:D002 and D003:D004 (10-bit code, diff/README inference: 0x3FF=0 dB, 0.09375 dB/step)
    def d00x_db(code10: int) -> float:
        return round((code10 - 0x3FF) * 0.09375, 4)

    gain_staging = {
        "hv_gain_0xC60F": {
            "linux_live_hex": f"0x{amp1[0xC60F][1]:02X}",
            "linux_live_amp1_line": amp1[0xC60F][0],
            "linux_live_amp2_line": amp2[0xC60F][0],
            "windows_basic_hex": "0x05",
            "windows_advance_hex": "0x05",
            "vendor_documented_points": {"0x00": "+6.0 dB (305210EC TagIO_Basic.txt)", "0x05": "+9.0 dB (Basic_cmd.txt:6, Advance_cmd.txt:6)"},
            "inferred_linear_step_db": 0.6,
            "inferred_linux_0x01_db": 6.6,
            "inferred_delta_win_minus_linux_db": 2.4,
            "status": "INFERRED_MEDIUM_UNMEASURED_STEP_LINEARITY_AND_BOOST_RAIL_UNKNOWN",
        },
        "digital_gain_0xD001_0xD004": {
            "linux_live_code10": "0x3BF",
            "linux_live_d001_d002": f"0x{amp1[0xD001][1]:02X}:0x{amp1[0xD002][1]:02X}",
            "windows_basic_code10": "0x3A9",
            "windows_advance_code10": "0x3CA",
            "diff_lane_inferred_scale_db_per_step": 0.09375,
            "diff_lane_linux_db": d00x_db(0x3BF),
            "diff_lane_windows_basic_db": d00x_db(0x3A9),
            "diff_lane_windows_advance_db": d00x_db(0x3CA),
            "diff_lane_basic_minus_linux_db": round(d00x_db(0x3A9) - d00x_db(0x3BF), 4),
            "diff_lane_advance_minus_linux_db": round(d00x_db(0x3CA) - d00x_db(0x3BF), 4),
            "module_lane_safety_caveat": (
                "Direction of 0xD002/0xD004 is NOT measured on hardware yet. "
                "Basic_cmd.txt:7 labels 0xA9 '(30 L, 0dB=2.8Vrms)' while Advance_cmd.txt:7 labels 0xCA '(30 L, -6dB=2Vrms)'; "
                "read literally, 0xA9 could be up to +4.0 dB louder than 0xBF. "
                "Both C60F=0x05 and D002/D004=0xA9 MUST remain gated behind P4 250ms thermal deadman interlock and verified via faint measurement first."
            ),
        },
        "net_gain_estimates_win_vs_linux": {
            "diff_lane_basic_net_db": round(2.4 + (d00x_db(0x3A9) - d00x_db(0x3BF)), 3),
            "diff_lane_advance_net_db": round(2.4 + (d00x_db(0x3CA) - d00x_db(0x3BF)), 3),
            "protection_lane_conservative_bound_db": "+3.0 to +9.0 dB (uncalibrated until live faint tone / I-sense measurement)",
        },
        "windows_fu21_endpoint_volume": {
            "registers": ["0x41000191 (ch1)", "0x41000192 (ch2)"],
            "range_db": [-63.0, 0.0],
            "step_db": 0.75,
            "windows_default_db": -6.0,
            "linux_exposed": False,
        },
    }

    # Forbidden registers table
    forbidden_specs = [
        (0xC000, "Software reset", "0x01", "0x02 (io_init)", True, "Resets chip state; Linux/Windows write different reset bits (0x02 vs 0x01); volatile."),
        (0xC050, "DC detection enable", "0x83", "0x83", False, "Hardware speaker DC-offset protection; disabling risks voice-coil burnout."),
        (0xC082, "Sine generator control", "0x39", "not written (reset 0x3B)", False, "Internal test-tone generator; accidental enable injects full-scale sine directly into drivers."),
        (0xC093, "Silence detection control", "0x80 init (0xE0/0x60 runtime)", "0x80", True, "Controls silence-detect auto-mute; volatile (live reads 0x87 status bits)."),
        (0xC09C, "Software PDB / K_R0 cal mode", "0x7B (K_R0 toggles 0x73)", "0x7B", False, "Power-down / R0-calibration mode switch; unsafe outside guarded calibration sequence."),
        (0xC09D, "Silence detection & S3 hold", "0x80", "0x80", True, "Silence detection / hold S3 control; volatile (live reads 0x87)."),
        (0xC0A3, "SoundWire clock detection", "0x71", "0x71", True, "SoundWire clock loss protection; volatile; disabling risks runaway PWM on clock stop."),
        (0xC102, "HV mute behaviour (Rev B)", "0x00", "0x00", False, "Bit 3=0 forces HV_Mute=1 when muted (Rev B silicon protection)."),
        (0xC60E, "HV_POW_SPK_PRO enable", "0x05", "0x05", False, "Bit 0 enables high-voltage output-stage speaker power protection; MUST stay 0x05."),
    ]
    forbidden_registers = []
    for reg, name, win_v, lin_v, is_vol, why in forbidden_specs:
        l1, v1 = amp1[reg]
        l2, v2 = amp2[reg]
        forbidden_registers.append({
            "register": f"0x{reg:04X}",
            "name": name,
            "windows_value": win_v,
            "linux_blind_write": lin_v,
            "live_amp1": f"0x{v1:02X} (amp1.registers.txt:{l1})",
            "live_amp2": f"0x{v2:02X} (amp2.registers.txt:{l2})",
            "volatile_in_driver": is_vol,
            "safety_rationale": why,
        })

    # IV-sense comparison
    iv_sense = {
        "slot_tags_0x3004_0x3005": {
            "linux_amp1": {"0x3004": f"0x{amp1[0x3004][1]:02X}", "0x3005": f"0x{amp1[0x3005][1]:02X}"},
            "linux_amp2": {"0x3004": f"0x{amp2[0x3004][1]:02X}", "0x3005": f"0x{amp2[0x3005][1]:02X}"},
            "windows_LL_amp1_final": {"0x3004": f"0x{ll_final[0x3004]:02X}", "0x3005": f"0x{ll_final[0x3005]:02X}"},
            "windows_RR_amp2_final": {"0x3004": f"0x{rr_final[0x3004]:02X}", "0x3005": f"0x{rr_final[0x3005]:02X}"},
            "windows_early_init_LL": {"0x3004": "0x82", "0x3005": "0x93"},
            "windows_early_init_RR": {"0x3004": "0xC6", "0x3005": "0xD7"},
            "linux_collision": True,
            "nibble_order_conflict_warning": (
                "Windows comments say 'LV=2, LI=0' for 0xA0 / 'LV=6, LI=4' for 0xE4 (implying high nibble=V, low=I), "
                "whereas Linux rt1316-sdw.c:435-438 defines Left V Tag = bits 2:0, Left I Tag = bits 6:4 (opposite nibble order). "
                "Actual slot identity (I vs V) is UNKNOWN until live IPC4 PCM 3 capture."
            ),
        },
        "other_ll_vs_rr_differences": {
            "CODEC_Init_register_diffs": ll_vs_rr,
            "0xC0B1_channel_select": {
                "windows_LL_amp1": "RMW (val & ~0x3) | 0x1 (L,L)",
                "windows_RR_amp2": "RMW (val & ~0x3) | 0x3 (R,R)",
                "linux_live_both": f"0x{amp1[0xC0B1][1]:02X} / 0x{amp2[0xC0B1][1]:02X} (Linux uses SDCA UDMPU21 0x41080100: amp1=0x01, amp2=0x05)",
            },
            "EndPoint002_aggregation_masks": {
                "windows_LL_amp1": {"GroupMask": "0xF", "GroupPosition": "0xC", "AggregationChannelMask": "0xC"},
                "windows_RR_amp2": {"GroupMask": "0xF", "GroupPosition": "0x3", "AggregationChannelMask": "0x3"},
                "linux_ipc4_alh": {"link1_amp1_mask": "0x3 (ch 0-1)", "link2_amp2_mask": "0xC (ch 2-3)", "note": "Opposite order to Windows"},
            },
        },
        "iv_sense_clock_and_power_diffs": {
            "0xC007": {"linux": f"0x{amp1[0xC007][1]:02X}", "windows": f"0x{ll_final[0xC007]:02X}", "windows_comment": "256 fs, PLL fout=12.288MHz (I-V=24KHz)"},
            "0xC008": {"linux": f"0x{amp1[0xC008][1]:02X}", "windows": f"0x{ll_final[0xC008]:02X}", "windows_comment": "256 fs"},
            "0x2F0A": {"linux": f"0x{amp1[0x2F0A][1]:02X}", "windows": f"0x{ll_final[0x2F0A]:02X}", "windows_comment": "Select v1.2 divider"},
            "0xC616": {"linux": f"0x{amp1[0xC616][1]:02X}", "windows_SM": "commented out", "windows_NoSM": "0x02"},
            "0x41001A08_PDE24": {"linux_playback_ipc3": f"0x{amp1[0x41001A08][1]:02X} (PS3 off)", "windows_playback": "0x00 (PS0 on during EP001_Open)"},
        },
    }

    return {
        "csv_row_counts": {
            "alc1316_LL_0AFE1028_CODEC_Init.csv": len(init_csv_rows),
            "windows_runtime_writes_0AFF.csv": len(runtime_csv_rows),
            "windows_vs_linux_vs_live_rt1316.csv": len(vs_linux_rows),
            "windows_vs_live_amp_registers.csv": len(vs_live_rows),
        },
        "dell_0b00_bq_table": {
            "source": str(COEFF_HEADER.relative_to(STACK_ROOT)) + ":163-298",
            "total_register_writes": len(bq_writes),
            "total_bytes_triples": len(bq_writes) * 3,
            "matches_windows_CODEC_Init_LL_and_RR_100pct": bq_matches_win,
            "live_amp1_matching_writes": bq_matches_amp1,
            "live_amp2_matching_writes": bq_matches_amp2,
            "contains_0xC202_enable": (0xC202 in bq_map),
            "layout_breakdown": [
                {"range": "0xC203..0xC216", "count": 20, "role": "SPOL BQ1 (woofer LPF) 5 x 4B Q5.27 BE"},
                {"range": "0xC217..0xC22A", "count": 20, "role": "SPOL BQ2 (subsonic HPF) 5 x 4B Q5.27 BE"},
                {"range": "0xC22B..0xC23E", "count": 20, "role": "SPOR BQ1 (tweeter HPF) 5 x 4B Q5.27 BE"},
                {"range": "0xC23F..0xC252", "count": 20, "role": "SPOR BQ2 (subsonic HPF) 5 x 4B Q5.27 BE"},
                {"range": "0xC00B", "count": 1, "role": "0x30 = enable EQ power clock"},
                {"range": "0xC380..0xC3A3", "count": 36, "role": "4 x 9B tuning-tool UI metadata (Fc, BW, Q, Gain, Type)"},
                {"range": "0xC261..0xC270", "count": 16, "role": "Pre/Post vol L/R (4 x 4B Q5.27 = 0x08000000 = 1.0)"},
                {"range": "0xC200", "count": 1, "role": "0xC0 = bit7 update pulse + bit6 default 1"},
            ],
            "note_on_0xD470_range": "0xD470..0xD497 belongs to RT1320 (rt1320-sdw.c:479), NOT RT1316. RT1316 BQ coefficients live at 0xC203..0xC252.",
        },
        "crossover_activation_bug": {
            "open_sequence": [
                {"reg": "0xC202", "val": "0xC0", "source": "EP001_Open/OpenCtl.txt:3", "comment": "enable BQ1, BQ2 (bit7=BQ1, bit6=BQ2)"},
                {"reg": "0xC200", "val": "0xC0", "source": "EP001_Open/OpenCtl.txt:4", "comment": "[7]=1 parameter update pulse"},
            ],
            "close_sequence": [
                {"reg": "0xC202", "val": "0x00", "source": "EP001_Open/CloseCtl.txt:8", "comment": "disable BQ1, BQ2"},
                {"reg": "0xC200", "val": "0xC0", "source": "EP001_Open/CloseCtl.txt:9", "comment": "[7]=1 parameter update pulse"},
            ],
            "live_linux_state": {
                "amp1_0xC202": f"0x{amp1[0xC202][1]:02X} (amp1.registers.txt:{amp1[0xC202][0]})",
                "amp2_0xC202": f"0x{amp2[0xC202][1]:02X} (amp2.registers.txt:{amp2[0xC202][0]})",
                "amp1_0xC200": f"0x{amp1[0xC200][1]:02X} (amp1.registers.txt:{amp1[0xC200][0]})",
                "amp2_0xC200": f"0x{amp2[0xC200][1]:02X} (amp2.registers.txt:{amp2[0xC200][0]})",
                "amp1_0xC201_status": f"0x{amp1[0xC201][1]:02X} (amp1.registers.txt:{amp1[0xC201][0]})",
                "amp2_0xC201_status": f"0x{amp2[0xC201][1]:02X} (amp2.registers.txt:{amp2[0xC201][0]})",
                "amp1_0xC174_silicon_rev": f"0x{amp1[0xC174][1]:02X}",
                "amp2_0xC174_silicon_rev": f"0x{amp2[0xC174][1]:02X}",
            },
            "regcache_sync_hazard": "0xC200 is non-volatile in stock rt1316-sdw.c:166-180, so regcache_sync replays cached 0xC0 BEFORE 0xC203..0xC252 in ascending order. Fix: mark 0xC200 volatile and pulse explicitly after writing coefficients & 0xC202.",
        },
        "biquad_stages": biquad_stages,
        "cascaded_channel_responses_at_48kHz": {
            "SPOL_woofer_shipped_44k1_coeffs": biquad_cascade_stats(spol_44k_coeffs, 48000.0),
            "SPOR_tweeter_shipped_44k1_coeffs": biquad_cascade_stats(spor_44k_coeffs, 48000.0),
            "SPOL_woofer_corrected_48k_coeffs": biquad_cascade_stats(spol_48k_coeffs, 48000.0),
            "SPOR_tweeter_corrected_48k_coeffs": biquad_cascade_stats(spor_48k_coeffs, 48000.0),
        },
        "pre_post_vol_words": vol_words,
        "iv_sense": iv_sense,
        "gain_staging": gain_staging,
        "forbidden_registers": forbidden_registers,
    }


# ---------------------------------------------------------------------------
# 3. Parse Dell RTSPKPT speaker-protection blob CSV
# ---------------------------------------------------------------------------

def analyze_rtspkpt() -> dict[str, Any]:
    csv_path = WIN_TABLES / "rtspkpt_DELL_0AFE1028_float32_nonzero.csv"
    rows = list(csv.DictReader(csv_path.open()))
    by_off = {
        int(r["offset_in_RTSPKPT"], 16): {
            "hex_le": r["hex_le"],
            "f32": float(r["as_float32"]),
            "u32": int(r["as_u32"]),
        }
        for r in rows
    }

    block_bases = [0x30, 0x21D4, 0x4378]
    stride = 0x21A4
    rec_stride = 0xAC

    # Verify all 3 populated blocks are byte-identical in 0x00..0x158
    block_diffs = {}
    for bi, base in enumerate(block_bases[1:], 1):
        diffs = sum(1 for rel in range(0, 2 * rec_stride, 4) if by_off.get(block_bases[0] + rel) != by_off.get(base + rel))
        block_diffs[f"block_{bi}_vs_block_0"] = diffs

    # Side-by-side comparison of RecA (+0x00) vs RecB (+0xAC) inside Block 0
    base0 = block_bases[0]
    rec_a = {off - base0: by_off[off] for off in by_off if base0 <= off < base0 + rec_stride}
    rec_b = {off - (base0 + rec_stride): by_off[off] for off in by_off if base0 + rec_stride <= off < base0 + 2 * rec_stride}

    # Semantic annotations strictly labeled PROVEN_RAW vs INFERRED_CANDIDATE vs UNKNOWN
    hints = {
        0x00: ("u32=13163", "INFERRED_LOW", "Codec/profile ID (ALC1316 variant code 13163)"),
        0x04: ("u32=4", "INFERRED_LOW", "Channel/mode count (4 speakers)"),
        0x0C: ("f32=100.0", "INFERRED_LOW", "Thermal ceiling candidate (100 degC, repeated x3 at +0x0C/+0x10/+0x14)"),
        0x10: ("f32=100.0", "INFERRED_LOW", "Thermal ceiling candidate (100 degC)"),
        0x14: ("f32=100.0", "INFERRED_LOW", "Thermal ceiling candidate (100 degC)"),
        0x18: ("f32 DIFF", "UNKNOWN", "Per-driver constant: RecA=0.00096 vs RecB=0.00039 (2.46x ratio; woofer vs tweeter candidate — e.g. excursion/compliance/gain, semantics UNPROVEN)"),
        0x1C: ("f32=0.00025", "UNKNOWN", "Shared small constant 2.5e-4"),
        0x20: ("f32=1.0", "UNKNOWN", "Unity scale/flag"),
        0x24: ("f32 DIFF", "UNKNOWN", "Per-driver constant: RecA=0.00016 vs RecB=0.00008 (2.00x ratio; semantics UNPROVEN)"),
        0x28: ("f32=0.375", "UNKNOWN", "Shared constant 0.375 (3/8)"),
        0x2C: ("u32=1", "UNKNOWN", "Boolean/enum flag = 1"),
        0x30: ("f32 DIFF", "UNKNOWN", "Per-driver constant: RecA=0.39 vs RecB=0.10 (3.90x ratio; semantics UNPROVEN)"),
        0x34: ("f32=4.0", "INFERRED_LOW", "Nominal DC resistance candidate Re0 = 4.0 ohm (matches 2.8^2/4 = 1.96 W ~ 2.0 W Dell spec)"),
        0x38: ("f32=0.0625", "UNKNOWN", "Shared constant 1/16 = 0.0625"),
        0x44: ("u32=900", "UNKNOWN", "Integer parameter 900 (e.g. 900 Hz or 900 ms/samples)"),
        0x48: ("f32=500.0", "UNKNOWN", "Float parameter 500.0 (e.g. 500 Hz or 500 ms)"),
        0x4C: ("u32=2", "UNKNOWN", "Integer parameter 2"),
        0x50: ("f32=24.6", "UNKNOWN", "Float parameter 24.6 (note: 24.6 + 20.4 = 45.0; thermal/gain constant candidate)"),
        0x54: ("f32=20.4", "UNKNOWN", "Float parameter 20.4"),
        0x58: ("u32=1", "UNKNOWN", "Flag = 1"),
        0x5C: ("f32=0.1", "UNKNOWN", "Float parameter 0.1"),
        0x60: ("f32=300.0", "UNKNOWN", "Float parameter 300.0 (e.g. time/freq constant)"),
        0x64: ("u32=1", "UNKNOWN", "Flag = 1"),
        0x68: ("f32=2.0", "INFERRED_LOW", "Float parameter 2.0 (matches 2.0 W avg driver rating OR 2.0 s fast coil time constant)"),
        0x6C: ("f32=0.1", "UNKNOWN", "Float parameter 0.1"),
        0x70: ("f32=0.1", "UNKNOWN", "Float parameter 0.1"),
        0x74: ("f32=0.1", "UNKNOWN", "Float parameter 0.1"),
        0x78: ("f32 DIFF", "INFERRED_LOW", "Per-driver constant: RecA=0.003 vs RecB=0.00328 (near copper TCR alpha ~0.00393/K, or inductance/step; semantics UNPROVEN)"),
        0x80: ("f32=25.0", "INFERRED_LOW", "Ambient reference temperature candidate T0 = 25.0 degC"),
        0x84: ("f32=100.0", "INFERRED_LOW", "Max coil temperature ceiling candidate Tmax = 100.0 degC"),
        0x88: ("f32=80.0", "INFERRED_LOW", "Staged thermal knee candidate #1 = 80.0 degC"),
        0x8C: ("f32=70.0", "INFERRED_LOW", "Staged thermal knee candidate #2 = 70.0 degC"),
        0x90: ("f32=60.0", "INFERRED_LOW", "Staged thermal knee candidate #3 = 60.0 degC"),
        0x94: ("f32=170.0", "UNKNOWN", "Threshold/scaling constant 170.0"),
        0x98: ("f32=340.0", "UNKNOWN", "Threshold/scaling constant 340.0 (= 2 x 170)"),
        0x9C: ("f32=340.0", "UNKNOWN", "Threshold/scaling constant 340.0"),
        0xA0: ("u32 DIFF", "UNKNOWN", "RecA=1 vs RecB=0"),
        0xA4: ("u32 DIFF", "UNKNOWN", "RecA=1 vs RecB=0"),
    }

    fields_table = []
    per_driver_diffs = []
    for rel in sorted(set(rec_a) | set(rec_b)):
        va = rec_a.get(rel)
        vb = rec_b.get(rel)
        same = va == vb
        hint = hints.get(rel, ("", "UNKNOWN", ""))
        entry = {
            "rel_offset": f"+0x{rel:02X}",
            "abs_offset_block0_recA": f"0x{base0 + rel:04X}",
            "abs_offset_block0_recB": f"0x{base0 + rec_stride + rel:04X}",
            "identical": same,
            "recA": va,
            "recB": vb,
            "status": hint[1],
            "note": hint[2],
        }
        fields_table.append(entry)
        if not same:
            per_driver_diffs.append(entry)

    return {
        "source_csv": str(csv_path.relative_to(STACK_ROOT)),
        "consumer_dll_inferred": "RtkIntU64.dll (classes: DCResistance, Excursion, Temperature, GainTemperature, SmartVolume, ADFImpedance)",
        "nonzero_u32_rows": len(rows),
        "blob_size_bytes": 34492,
        "block_structure": {
            "populated_blocks": 3,
            "block_bases_hex": [f"0x{b:04X}" for b in block_bases],
            "block_stride_bytes": stride,
            "record_stride_bytes": rec_stride,
            "records_per_block": 2,
            "block_equality_diffs": block_diffs,
            "tail_msvc_debug_fill": "0xCDCDCDCD (uninitialized MSVC heap fill at block+0x2188/+0x218C/+0x2194 proving raw C struct dump)",
            "trailing_datestamp": {"offset": "0x86B8", "u32": by_off[0x86B8]["u32"], "interpreted_date": "2016-04-14"},
        },
        "per_driver_differences_recA_vs_recB": per_driver_diffs,
        "all_record_fields": fields_table,
        "candidate_physical_constants": {
            "nominal_Re_ohm_candidate": {"value": 4.0, "offset": "+0x34", "status": "INFERRED_LOW", "cross_check": "2.8 Vrms^2 / 4 ohm = 1.96 W ~ 2.0 W rated avg"},
            "T0_ambient_C_candidate": {"value": 25.0, "offset": "+0x80", "status": "INFERRED_LOW"},
            "Tmax_ceiling_C_candidate": {"value": 100.0, "offset": "+0x0C/+0x10/+0x14/+0x84", "status": "INFERRED_LOW"},
            "staged_thermal_thresholds_C_candidates": {"values": [80.0, 70.0, 60.0], "offsets": ["+0x88", "+0x8C", "+0x90"], "status": "INFERRED_LOW"},
            "rated_power_or_tau_candidate": {"value": 2.0, "offset": "+0x68", "status": "INFERRED_LOW"},
            "tcr_or_driver_coeff_candidates": {"recA": 0.003, "recB": 0.00328, "offset": "+0x78", "status": "UNKNOWN"},
            "excursion_or_model_diff_candidates": {
                "+0x18": {"recA": 0.00096, "recB": 0.00039, "ratio": round(0.00096 / 0.00039, 3)},
                "+0x24": {"recA": 0.00016, "recB": 0.00008, "ratio": 2.0},
                "+0x30": {"recA": 0.39, "recB": 0.10, "ratio": 3.9},
            },
            "caveat": "No field names exist in RTSPKPT; exact semantics require RtkIntU64.dll static disassembly. Treat all values as working bounds until measured via IPC4 I-sense.",
        },
    }


# ---------------------------------------------------------------------------
# 4. Parse Waves StradaleMLK BPAM (.mps) + Cross-check against records CSVs
# ---------------------------------------------------------------------------

def load_waves_param_names() -> dict[int, str]:
    p = DIFF_DIR / "work/waves_param_ids.csv"
    return {
        int(r["r8d_id"]): r["name"].replace("MA_DATA::PARAM_", "")
        for r in csv.DictReader(p.open())
        if r["r8d_id"]
    }


def decode_linklaws_blob(blob: bytes, param_names: dict[int, str]) -> list[dict[str, Any]]:
    records = []
    off = 0
    while off < len(blob):
        rlen = struct.unpack_from("<I", blob, off)[0]
        rec = blob[off : off + rlen]
        ver, src_pid, _u1, dst_pid, slen = struct.unpack_from("<5I", rec, 4)
        interp = rec[24 : 24 + slen].rstrip(b"\x00").decode("ascii", errors="replace")
        npts = struct.unpack_from("<I", rec, 24 + slen)[0]
        xs = struct.unpack_from(f"<{npts}f", rec, 28 + slen)
        ys = struct.unpack_from(f"<{npts}f", rec, 28 + slen + 4 * npts)
        records.append({
            "version": ver,
            "src_param_id": src_pid,
            "src_param_name": param_names.get(src_pid, f"ID_{src_pid}"),
            "dst_param_id": dst_pid,
            "dst_param_name": param_names.get(dst_pid, f"ID_{dst_pid}"),
            "interpolation": interp,
            "breakpoints": [
                {"system_vol_permille": round(x, 1), "system_vol_pct": round(x / 10.0, 1), "value": round(y, 4)}
                for x, y in zip(xs, ys)
            ],
        })
        off += rlen
    return records


def parse_bpam_file(fn: Path, param_names: dict[int, str]) -> dict[str, Any]:
    d = fn.read_bytes()
    assert d[:4] == b"BPAM", f"Not BPAM: {fn}"
    dev_cnt = d[38]
    p = 39
    devs: dict[int, str] = {}
    for _ in range(dev_cnt):
        did = d[p]
        slen = d[p + 1]
        devs[did] = d[p + 2 : p + 2 + slen].decode("ascii")
        p += 2 + slen
    c3_len = struct.unpack_from("<I", d, p)[0]
    p += 4
    set_name = d[p + 2 : p + 2 + d[p + 1]].decode("ascii")
    p += c3_len
    c4_len = struct.unpack_from("<I", d, p)[0]
    p += 4
    blk_cnt = struct.unpack_from("<H", d, p + 1)[0]
    p += 3
    blocks = []
    for bi in range(blk_cnt):
        blen = struct.unpack_from("<H", d, p)[0]
        body = d[p + 2 : p + 2 + blen]
        dev_id = body[0]
        stream_cat = body[1]
        flags = struct.unpack_from("<I", body, 2)[0]
        param_cnt = struct.unpack_from("<H", body, 6)[0]
        layer_id = struct.unpack_from("<H", body, 8)[0]
        is_base = struct.unpack_from("<H", body, 10)[0]
        nlen = body[12]
        bname = body[13 : 13 + nlen].decode("ascii", errors="replace")
        po = 13 + nlen + (1 if 13 + nlen < len(body) and body[13 + nlen] == 0 else 0)
        params: dict[str, float] = {}
        param_ids: dict[str, int] = {}
        linklaws: list[dict[str, Any]] = []
        for _ in range(param_cnt):
            pid = struct.unpack_from("<H", body, po)[0]
            ptype = body[po + 2]
            psize = struct.unpack_from("<H", body, po + 3)[0]
            payload = body[po + 5 : po + 5 + psize]
            po += 5 + psize
            pname = param_names.get(pid, f"ID_{pid}")
            if ptype == 0 and psize == 8:
                params[pname] = struct.unpack("<d", payload)[0]
                param_ids[pname] = pid
            elif ptype == 3 and b"Linear" in payload:
                linklaws = decode_linklaws_blob(payload, param_names)
        assert po == len(body), f"Unparsed tail {len(body) - po}B in {fn.name} block {bi}"
        blocks.append({
            "block_index": bi,
            "offset_hex": f"0x{p:X}",
            "dev_id": dev_id,
            "dev_name": devs.get(dev_id, f"DEV_{dev_id}"),
            "stream_cat": stream_cat,
            "flags_hex": f"0x{flags:08X}",
            "layer": layer_id,
            "is_base": bool(is_base),
            "name": bname,
            "param_count": param_cnt,
            "params": params,
            "linklaws": linklaws,
        })
        p += 2 + blen
    assert p == len(d), f"File end mismatch {p} != {len(d)} in {fn.name}"
    return {"file": str(fn.relative_to(STACK_ROOT)), "set_name": set_name, "devices": devs, "block_count": blk_cnt, "blocks": blocks}


def extract_peq_bands(params: dict[str, float], channel: str = "L") -> list[dict[str, Any]]:
    type_names = {0: "peak", 1: "low_shelf", 2: "high_shelf", 3: "low_pass", 4: "high_pass"}
    out = []
    for i in range(1, 11):
        if channel == "L":
            st = params.get(f"BAND_{i}_STATE")
            ty = params.get(f"BAND_{i}_TYPE")
            fr = params.get(f"BAND_{i}_FREQ")
            gn = params.get(f"BAND_{i}_GAIN")
            qv = params.get(f"BAND_{i}_Q")
        else:
            st = params.get(f"BAND{i}_STATE_R")
            ty = params.get(f"BAND{i}_TYPE_R")
            fr = params.get(f"BAND{i}_FREQ_R")
            gn = params.get(f"BAND{i}_GAIN_R")
            qv = params.get(f"BAND{i}_Q_R")
        if st is None and fr is None:
            continue
        t_int = int(ty) if ty is not None else 0
        out.append({
            "band": i,
            "enabled": bool(st) if st is not None else False,
            "type_id": t_int,
            "type_name": type_names.get(t_int, f"type_{t_int}"),
            "freq_hz": round(fr, 2) if fr is not None else None,
            "gain_db": round(gn, 2) if gn is not None else 0.0,
            "q": round(qv, 4) if qv is not None else None,
        })
    return out


def extract_mvbands(params: dict[str, float]) -> list[dict[str, Any]]:
    out = []
    for i in range(1, 11):
        on = params.get(f"MVBAND{i}_ONOFF", 0.0)
        mk = params.get(f"MVBAND{i}_MAKEUP", 0.0)
        ratio = params.get(f"MVBAND{i}_COMP_RATIO", 1.0)
        if on or abs(mk) > 1e-6 or abs(ratio - 1.0) > 1e-6:
            out.append({
                "band": i,
                "on": bool(on),
                "freq_hz": round(params.get(f"MVBAND{i}_FREQUENCY", 0.0), 2),
                "q": round(params.get(f"MVBAND{i}_Q", 0.0), 3),
                "makeup_db": round(mk, 2),
                "comp_on": bool(params.get(f"MVBAND{i}_COMP_ONOFF", 0.0)),
                "threshold_db": round(params.get(f"MVBAND{i}_COMP_THR", 0.0), 2),
                "ratio": round(ratio, 3),
                "attack_ms": round(params.get(f"MVBAND{i}_COMP_ATK", 0.0), 2),
                "release_ms": round(params.get(f"MVBAND{i}_COMP_REL", 0.0), 2),
                "ref_mode": int(params.get(f"MVBAND{i}_REF_MODE", 0.0)),
            })
    return out


def eval_peq_curve(bands: list[dict[str, Any]], freqs: list[float]) -> dict[str, float]:
    curve = {}
    for f in freqs:
        tot = 0.0
        for b in bands:
            if b["enabled"] and b["freq_hz"] and b["q"]:
                tot += waves_peq_band_mag_db(b["type_id"], b["freq_hz"], b["gain_db"], b["q"], f)
        curve[f"{f:g}Hz"] = round(tot, 2)
    return curve


def analyze_waves_stradale() -> dict[str, Any]:
    pnames = load_waves_param_names()

    # Cross-verify raw CSV row counts
    csv_counts = {}
    for stage in ("EFX", "MFX", "SFX"):
        csv_p = WIN_TABLES / f"waves_StradaleMLK_ADL_MAPro11_DSP_{stage}_Render.records.csv"
        with csv_p.open() as f:
            csv_counts[stage] = sum(1 for _ in f) - 1

    efx_doc = parse_bpam_file(WIN_MAXX_TREE / "StradaleMLK_ADL_MAPro11_DSP_EFX_Render.mps", pnames)
    mfx_doc = parse_bpam_file(WIN_MAXX_TREE / "StradaleMLK_ADL_MAPro11_DSP_MFX_Render.mps", pnames)
    sfx_doc = parse_bpam_file(WIN_MAXX_TREE / "StradaleMLK_ADL_MAPro11_DSP_SFX_Render.mps", pnames)

    efx_spk = efx_doc["blocks"][0]       # dev=0 Internal Speaker, Speaker protection
    efx_off = efx_doc["blocks"][1]       # dev=0 Internal Speaker, Master Off
    efx_sec = efx_doc["blocks"][25]      # dev=7 IS - secondary, Speaker protection
    mfx_def = mfx_doc["blocks"][0]       # dev=0 Internal Speaker, Default
    mfx_off = mfx_doc["blocks"][1]       # dev=0 Internal Speaker, Master Off
    mfx_nx = mfx_doc["blocks"][163]      # dev=0 Internal Speaker, NX MFX Subtype (cat=4 Default)
    sfx_def = sfx_doc["blocks"][4]       # dev=0 Internal Speaker, NxS Active [Default]
    sfx_nxs = sfx_doc["blocks"][100]     # dev=0 Internal Speaker, Nx Speakers On
    sfx_n360 = sfx_doc["blocks"][105]    # dev=0 Internal Speaker, NxS N360 On
    sfx_um = sfx_doc["blocks"][110]      # dev=0 Internal Speaker, UM228 On

    def pick(p: dict[str, float], keys: list[str]) -> dict[str, float]:
        return {k: round(p[k], 6) for k in keys if k in p}

    eval_freqs = [30, 50, 64, 80, 100, 150, 200, 250, 300, 350, 450, 600, 800, 920, 1000, 1400, 1800, 2700, 3120, 4000, 6000, 8000, 10000, 13200, 16000]

    efx_peq_l = extract_peq_bands(efx_spk["params"], "L")
    efx_peq_r = extract_peq_bands(efx_spk["params"], "R")
    mfx_def_peq = extract_peq_bands(mfx_def["params"], "L")
    mfx_off_peq = extract_peq_bands(mfx_off["params"], "L")
    mfx_nx_peq = extract_peq_bands(mfx_nx["params"], "L")
    sfx_n360_peq = extract_peq_bands(sfx_n360["params"], "L")

    # Compute cascaded EFX[0] + MFX[0] static PEQ transfer curve for L and R
    cascaded_L = eval_peq_curve(efx_peq_l + mfx_def_peq, eval_freqs)
    cascaded_R = eval_peq_curve(efx_peq_r + mfx_def_peq, eval_freqs)

    return {
        "preset_set_names": {
            "EFX": efx_doc["set_name"],
            "MFX": mfx_doc["set_name"],
            "SFX": sfx_doc["set_name"],
        },
        "source_csv_record_counts": csv_counts,
        "bpam_block_counts": {
            "EFX": efx_doc["block_count"],
            "MFX": mfx_doc["block_count"],
            "SFX": sfx_doc["block_count"],
        },
        "waves_flag_polarity_finding": {
            "status": "INFERRED_VERY_HIGH",
            "MASTERBP": "0 = active, 1 = master bypassed (proven by SFX Nx Off MASTERBP=1 and EFX Headphones MASTERBP=1)",
            "legacy_module_BYPASS_flags": "PEQ_BYPASS, MAXX_BASS_BYPASS, MAXX_TREBLE_BYPASS, MAXX_STEREO_BYPASS, MAXX_VOLUME_BYPASS use 1 = ENABLED / ACTIVE and 0 = BYPASSED / OFF (proven by 5-way invariant across MFX Default vs Master Off, EFX Speaker protection, TV Notification, and SFX NxS N360 On).",
        },
        "EFX_Internal_Speaker": {
            "block_0_Speaker_protection": {
                "offset": efx_spk["offset_hex"],
                "flags": pick(efx_spk["params"], [
                    "MASTERBP", "PEQ_BYPASS", "MAXX_BASS_BYPASS", "MAXX_TREBLE_BYPASS",
                    "MAXX_STEREO_BYPASS", "MAXX_VOLUME_BYPASS", "MVBAND_ACTIVE", "MULTITRIM_ACTIVE",
                    "SV_COMPENSATION_LIMITER_ENABLE", "SMARTAMP_ONOFF", "IVOLUME_THERMAL_ON_OFF",
                    "IVOLUME_EXCURSION_ON_OFF", "IVOLUME_PANEL_THERMAL_ON_OFF", "SMARTAMP_PILOT_ONOFF",
                ]),
                "output_trim_db": efx_spk["params"].get("VOLUMN_TRIM_OUTPUT"),
                "peq_L_bands": efx_peq_l,
                "peq_R_bands": efx_peq_r,
                "peq_L_response_db": eval_peq_curve(efx_peq_l, eval_freqs),
                "peq_R_response_db": eval_peq_curve(efx_peq_r, eval_freqs),
                "thermal_limiter_placeholders": pick(efx_spk["params"], [
                    "IVOLUME_THERMAL_ATT", "IVOLUME_THERMAL_REL", "IVOLUME_PANEL_TMAX",
                    "IVOLUME_THERMAL_LIMITER_MAX_GR", "IVOLUME_THERMAL_LIMITER_RELEASE_TIME",
                    "SMARTAMP_PILOT_FREQ", "SMARTAMP_DCREM_FREQ",
                ]),
            },
            "block_1_Master_Off": {
                "offset": efx_off["offset_hex"],
                "params": pick(efx_off["params"], [
                    "MASTERBP", "MAXX_BASS_BYPASS", "MAXX_BASS_FREQUENCY", "MAXX_BASS_INTENSITY",
                    "MAXX_TREBLE_BYPASS", "MAXX_TREBLE_FREQUENCY", "MAXX_TREBLE_INTENSITY",
                    "MAXX_STEREO_BYPASS", "MAXX_STEREO_INTENSITY", "MAXX_VOLUME_BYPASS",
                ]),
            },
            "block_25_IS_secondary_Speaker_protection": {
                "offset": efx_sec["offset_hex"],
                "peq_L_bands": extract_peq_bands(efx_sec["params"], "L"),
                "peq_R_bands": extract_peq_bands(efx_sec["params"], "R"),
                "maxx_volume_slider": efx_sec["params"].get("MAXX_VOLUME_SLIDER"),
            },
        },
        "MFX_Internal_Speaker": {
            "block_0_Default": {
                "offset": mfx_def["offset_hex"],
                "flags": pick(mfx_def["params"], [
                    "MASTERBP", "PEQ_BYPASS", "MAXX_BASS_BYPASS", "MAXX_TREBLE_BYPASS",
                    "MAXX_STEREO_BYPASS", "MAXX_VOLUME_BYPASS", "MVBAND_ACTIVE",
                    "LEVELLER_ACTIVE", "VOLUME_COMPRESSOR_ACTIVE", "CENTER_ACTIVE",
                    "EXCITER_ACTIVE", "LINKLAWS_ACTIVE", "VARISLOPE_XOV_ACTIVE",
                    "MULTITRIM_ACTIVE", "SV_COMPENSATION_LIMITER_ENABLE",
                ]),
                "peq_bands": mfx_def_peq,
                "peq_response_db": eval_peq_curve(mfx_def_peq, eval_freqs),
                "maxxbass": pick(mfx_def["params"], [
                    "MAXX_BASS_FREQUENCY", "MAXX_BASS_INTENSITY", "MAXXBASS_AMOUNT",
                    "MAXXBASS_INPUT_HP_RATIO", "MAXXBASS_CUTOFF_RATIO", "MAXXBASS_INTENSITY_RATIO",
                    "MAXXBASS_OUTPUT_LP_RATIO_A", "MAXXBASS_OUTPUT_LP_RATIO_B",
                    "MAXXBASS_COMP_INTENSITY", "MAXX_BASS_HP_ACTIVE", "MAXX_BASS_HP_FREQ",
                    "MAXX_BASS_WIDEBAND_MODE", "LIGHT_MAXXBASS",
                ]),
                "maxxtreble_and_exciter": pick(mfx_def["params"], [
                    "MAXX_TREBLE_FREQUENCY", "MAXX_TREBLE_INTENSITY",
                    "EXCITER_ACTIVE", "EXCITER_WET_GAIN",
                ]),
                "spatial_and_center": pick(mfx_def["params"], [
                    "MAXX_STEREO_INTENSITY", "STEREO_SPAN", "CENTER_ACTIVE",
                    "CENTER_GAIN_C", "CENTER_GAIN_LR",
                ]),
                "dynamics_and_limiter": pick(mfx_def["params"], [
                    "MAXX_VOLUME_SLIDER", "VOLUME_LOW_GAIN", "VOLUME_COMPRESSOR_THRESHOLD",
                    "VOLUME_COMPRESSOR_RATIO", "VOLUME_COMPRESSOR_ATTACK", "VOLUME_COMPRESSOR_RELEASE",
                    "VOLUME_COMPRESSOR_MAKEUP", "VOLUME_HARD_KNEE", "VOLUME_DYN_RANGE", "VOLUME_GATE",
                    "LEVELLER_THRESHOLD", "LEVELLER_MAKEUP", "LEVELLER_CURVE",
                    "LEVELER_ATTACK", "LEVELER_RELEASE", "VOLUME_SYSTEM_VOLUME_ENABLE",
                ]),
                "varislope_crossover": pick(mfx_def["params"], [
                    "VARISLOPE_XOV_ACTIVE", "VARISLOPE_XOV_BAND0_ON", "VARISLOPE_XOV_BAND0_FREQ",
                    "VARISLOPE_XOV_BAND0_TYPE", "VARISLOPE_XOV_BAND0_SLOPE",
                    "VARISLOPE_XOV_BAND1_FREQ", "VARISLOPE_XOV_BAND2_FREQ", "VARISLOPE_XOV_BAND3_FREQ",
                ]),
                "multiband_dynamics": extract_mvbands(mfx_def["params"]),
                "volume_adaptive_linklaws": mfx_def["linklaws"],
            },
            "block_1_Master_Off": {
                "offset": mfx_off["offset_hex"],
                "flags": pick(mfx_off["params"], [
                    "MASTERBP", "PEQ_BYPASS", "MAXX_BASS_BYPASS", "MAXX_TREBLE_BYPASS",
                    "MAXX_STEREO_BYPASS", "MAXX_VOLUME_BYPASS", "MVBAND_ACTIVE",
                    "LEVELLER_ACTIVE", "VOLUME_COMPRESSOR_ACTIVE", "CENTER_ACTIVE", "EXCITER_ACTIVE",
                ]),
                "peq_bands": mfx_off_peq,
                "maxxbass": pick(mfx_off["params"], [
                    "MAXX_BASS_FREQUENCY", "MAXX_BASS_INTENSITY", "MAXXBASS_AMOUNT",
                    "MAXXBASS_INPUT_HP_RATIO", "MAXXBASS_CUTOFF_RATIO", "MAXXBASS_INTENSITY_RATIO",
                    "MAXXBASS_OUTPUT_LP_RATIO_A", "MAXXBASS_OUTPUT_LP_RATIO_B",
                    "MAXXBASS_COMP_INTENSITY", "MAXX_BASS_ORIGINAL_BASS",
                ]),
                "maxxtreble_and_stereo": pick(mfx_off["params"], [
                    "MAXX_TREBLE_FREQUENCY", "MAXX_TREBLE_INTENSITY",
                    "MAXX_STEREO_INTENSITY", "CENTER_GAIN_C", "CENTER_GAIN_LR",
                ]),
                "multiband_dynamics_stored": extract_mvbands(mfx_off["params"]),
            },
            "block_163_NX_MFX_Subtype": {
                "offset": mfx_nx["offset_hex"],
                "output_trim_db": mfx_nx["params"].get("VOLUMN_TRIM_OUTPUT"),
                "peq_bands": mfx_nx_peq,
                "maxxbass": pick(mfx_nx["params"], [
                    "MAXX_BASS_FREQUENCY", "MAXX_BASS_INTENSITY", "MAXXBASS_AMOUNT",
                    "MAXXBASS_INPUT_HP_RATIO", "MAXXBASS_CUTOFF_RATIO", "MAXXBASS_INTENSITY_RATIO",
                    "MAXXBASS_OUTPUT_LP_RATIO_A", "MAXXBASS_OUTPUT_LP_RATIO_B", "MAXXBASS_COMP_INTENSITY",
                ]),
                "maxxtreble_and_stereo": pick(mfx_nx["params"], [
                    "MAXX_TREBLE_FREQUENCY", "MAXX_TREBLE_INTENSITY", "MAXX_STEREO_INTENSITY",
                ]),
                "multiband_dynamics": extract_mvbands(mfx_nx["params"]),
            },
        },
        "SFX_Internal_Speaker": {
            "block_4_NxS_Active_Default": {
                "offset": sfx_def["offset_hex"],
                "spatial_and_xtc": pick(sfx_def["params"], [
                    "MASTERBP", "AMBISONICSV2_ACTIVE", "AMBISONICSV2_DIRECT_ONLY",
                    "AMBISONICSV2_GAIN", "AMBISONICSV2_ROOM_NUM", "AMBISONICSV2_STEREO_ANGLE",
                    "AMBISONICSV2_SURROUND_ANGLE", "AMBISONICSV2_REMOVE_SPEAKER_DELAY",
                    "XTC_ACTIVE", "XTC_SPEAKER_DISTANCE", "XTC_SPEAKER_EAR_DISTANCE",
                    "XTC_CAM_TO_SPEAKERS_Y_OFS", "XTC_HEADTRACK", "XTC_LF_SPLIT_FREQ", "XTC_LF_SPLIT_Q",
                ]),
            },
            "block_100_Nx_Speakers_On": {
                "offset": sfx_nxs["offset_hex"],
                "spatial_and_xtc": pick(sfx_nxs["params"], [
                    "XTC_ACTIVE", "XTC_SPEAKER_DISTANCE", "XTC_SPEAKER_EAR_DISTANCE",
                    "XTC_CAM_TO_SPEAKERS_Y_OFS", "XTC_HEADTRACK", "XTC_LF_SPLIT_FREQ", "XTC_LF_SPLIT_Q",
                    "AMBISONICSV2_GAIN", "AMBISONICSV2_STEREO_ANGLE", "AMBISONICSV2_SURROUND_ANGLE",
                    "AMBISONICSV2_ROOM_NUM", "AMBISONICSV2_REMOVE_SPEAKER_DELAY",
                    "AMBISONICSV2_GAIN_SOURCE_CENTER", "AMBISONICSV2_GAIN_SOURCE_LR",
                    "AMBISONICSV2_GAIN_SOURCE_LRS", "AMBISONICSV2_GAIN_SOURCE_LRS2",
                ]),
            },
            "block_105_NxS_N360_On": {
                "offset": sfx_n360["offset_hex"],
                "flags_and_bass": pick(sfx_n360["params"], [
                    "N360_ACTIVE", "XTC_ACTIVE", "PEQ_BYPASS",
                    "N360_BASS_CUT", "N360_BASS_GAIN", "N360_BASS_FB_PANNING",
                    "AMBISONICSV2_ROOM_NUM", "AMBISONICSV2_STEREO_ANGLE", "AMBISONICSV2_SURROUND_ANGLE",
                ]),
                "peq_bands": sfx_n360_peq,
            },
            "block_110_UM228_On": {
                "offset": sfx_um["offset_hex"],
                "flags": pick(sfx_um["params"], [
                    "SURROUND_UPMIX_ACTIVE", "PEQ_BYPASS", "AMBISONICSV2_ROOM_NUM", "AMBISONICSV2_STEREO_ANGLE",
                ]),
            },
        },
        "cascaded_static_peq_EFX0_plus_MFX0_db": {
            "L_channel": cascaded_L,
            "R_channel": cascaded_R,
        },
        "chassis_resonance_notch_summary_for_lead2_and_lead3": {
            "description": "Repeated high-Q cuts across EFX[0] L/R and MFX[0]/MFX[1] that reveal physical Dell XPS 17 9720 chassis & driver resonances",
            "notch_clusters_hz": [
                {"band_hz": "224-290 Hz", "efx_R_cuts_db": [-4.1, -6.9], "q": 6.0, "note": "Right enclosure low-mid cavity mode"},
                {"band_hz": "344-382 Hz", "efx_L_cut_db": -7.5, "efx_R_cut_db": -8.4, "mfx0_cut_db": -6.5, "q_range": [4.66, 5.98], "note": "Strongest shared chassis resonance (both sides!)"},
                {"band_hz": "449-618 Hz", "efx_L_cut_db": -3.9, "efx_R_cut_db": -6.4, "q_range": [3.0, 3.63], "note": "Lower-mid boxiness"},
                {"band_hz": "778-928 Hz", "efx_L_cuts_db": [-4.9, -4.8], "efx_R_cuts_db": [-5.0, -7.6], "q_range": [3.0, 6.0], "note": "Tweeter crossover transition zone / upper-woofer breakup"},
                {"band_hz": "1017-1397 Hz", "efx_L_cut_db": -2.6, "efx_R_cuts_db": [-3.2, -5.2], "mfx0_cut_db": -4.0, "q_range": [2.09, 5.98], "note": "Nasal/shouty 1.0-1.5 kHz peak (matches Blue Yeti +4.2 dB baseline peak!)"},
                {"band_hz": "1734-1878 Hz", "efx_L_cut_db": -6.7, "efx_R_cut_db": -6.4, "q": 3.0, "note": "Upper-mid glare peak"},
                {"band_hz": "2715-3135 Hz", "efx_L_cut_db": -8.8, "efx_R_cut_db": -9.5, "mfx0_cut_db": -3.2, "q_range": [3.0, 6.0], "note": "Deepest notch in entire stack (-8.8/-9.5 dB tweeter/grille resonance at 3.1 kHz!)"},
                {"band_hz": "13.2-17.2 kHz", "efx_L_shelf_db": -4.9, "mfx0_cut_db": -7.1, "note": "Ultrasonic/air harshness taming"},
            ],
        },
    }


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    alc = analyze_alc1316_hardware()
    rtspkpt = analyze_rtspkpt()
    waves = analyze_waves_stradale()

    summary = {
        "schema_version": "1.0.0",
        "target_platform": {
            "model": "Dell XPS 17 9720",
            "dmi_sku": "0AFF",
            "subsys_id": "0AFF1028",
            "windows_codec_folder_sku": "0AFE1028",
            "codename": "Stradale / StradaleMLK",
            "amplifiers": "Dual Realtek ALC1316 / ALC1319D (rt1316-sdca) on SoundWire Links 1 (LL) & 2 (RR)",
            "drivers": "2 x 2W avg (2.5W peak) woofers + 2 x 2W avg (2.5W peak) tweeters",
            "geometry_m": {"speaker_span_S": 0.340, "waves_xtc_ear_distance_D": 0.720, "nominal_desk_D": 0.550},
        },
        "alc1316_silicon": alc,
        "rtspkpt_speaker_protection": rtspkpt,
        "waves_stradale_dsp": waves,
    }

    OUT_JSON.write_text(json.dumps(summary, indent=2) + "\n")

    # Crisp stdout verification report
    print("=== XPS 17 9720 SILICON & WINDOWS AUDIT VERIFICATION ===")
    print(f"Wrote: {OUT_JSON} ({OUT_JSON.stat().st_size} bytes)")
    tbl = alc["dell_0b00_bq_table"]
    print(f"Dell BQ table: {tbl['total_register_writes']} writes ({tbl['total_bytes_triples']} B), "
          f"win_match={tbl['matches_windows_CODEC_Init_LL_and_RR_100pct']}, "
          f"live_amp1={tbl['live_amp1_matching_writes']}/134, live_amp2={tbl['live_amp2_matching_writes']}/134, "
          f"has_0xC202={tbl['contains_0xC202_enable']}")
    for k, st in alc["biquad_stages"].items():
        s44 = st["shipped_44k1_designed"]
        s48 = st["corrected_48k_designed"]
        print(f"  {k:24s}: UI Fc={st['ui_design_fields']['Fc_hz']}Hz Q={st['ui_design_fields']['Q_derived_Fc_over_BW']:.4f} "
              f"lsb_err@44.1k={s44['rbj_44100Hz_max_lsb_error']} "
              f"peak@48k={s44['single_stage_at_48kHz']['peak_gain_db']:+.2f}dB@{s44['single_stage_at_48kHz']['peak_freq_hz']}Hz "
              f"-3dB={s44['single_stage_at_48kHz']['minus_3db_crossings']} "
              f"| 48k_corr -3dB={s48['single_stage_at_48kHz']['minus_3db_crossings']}")
    print(f"Live 0xC202: amp1={alc['crossover_activation_bug']['live_linux_state']['amp1_0xC202']}, "
          f"amp2={alc['crossover_activation_bug']['live_linux_state']['amp2_0xC202']}")
    print(f"IV-sense 0x3004/0x3005: Linux amp1={alc['iv_sense']['slot_tags_0x3004_0x3005']['linux_amp1']}, "
          f"amp2={alc['iv_sense']['slot_tags_0x3004_0x3005']['linux_amp2']} | "
          f"Windows LL={alc['iv_sense']['slot_tags_0x3004_0x3005']['windows_LL_amp1_final']}, "
          f"RR={alc['iv_sense']['slot_tags_0x3004_0x3005']['windows_RR_amp2_final']}")
    print(f"RTSPKPT: {rtspkpt['nonzero_u32_rows']} nonzero u32 rows, 3 identical blocks (diffs={rtspkpt['block_structure']['block_equality_diffs']}), "
          f"{len(rtspkpt['per_driver_differences_recA_vs_recB'])} per-driver diffs")
    print(f"Waves BPAM parsed 100% cleanly: EFX={waves['bpam_block_counts']['EFX']}, "
          f"MFX={waves['bpam_block_counts']['MFX']}, SFX={waves['bpam_block_counts']['SFX']} blocks")
    print(f"Waves MFX[0] LINKLAWS decoded: {len(waves['MFX_Internal_Speaker']['block_0_Default']['volume_adaptive_linklaws'])} curves")
    print(f"Waves SFX[100] XTC geometry: S={waves['SFX_Internal_Speaker']['block_100_Nx_Speakers_On']['spatial_and_xtc']['XTC_SPEAKER_DISTANCE']}cm, "
          f"D={waves['SFX_Internal_Speaker']['block_100_Nx_Speakers_On']['spatial_and_xtc']['XTC_SPEAKER_EAR_DISTANCE']}cm, "
          f"LF_split={waves['SFX_Internal_Speaker']['block_100_Nx_Speakers_On']['spatial_and_xtc']['XTC_LF_SPLIT_FREQ']}Hz")
    return 0


if __name__ == "__main__":
    sys.exit(main())

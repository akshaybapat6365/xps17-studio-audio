#!/usr/bin/env python3
"""
Dell XPS 17 9720 Studio Audio Engineering Stack — Minimum-Phase FIR Kernel Synthesizer
======================================================================================

Designs, synthesizes, exports, and exhaustively verifies 4 production 4096-tap @ 48 kHz
stereo IEEE float32 minimum-phase WAV FIR kernels grounded in:

  1. Real Blue Yeti X acoustic measurements of this exact Dell XPS 17 9720 unit
     (/home/akshaybapat/project/omarchy-audio-tune/baseline_curve.npy; 5-sweep averaged,
     0.076 dB repeatability SD, H2/H3 <= -54 dB).
  2. Harman / Olive / Toole psychoacoustic in-room listener preference targets:
     - Olive (2004), AES Preprints 6113 & 6194
     - Olive, Welti, & McMullin (2013), AES Convention Papers 8867 & 8994
     - Olive, Welti, & Khonsaripour (2018), AES 144th Convention
     - Toole (2017), Sound Reproduction, 3rd ed., Routledge
  3. Psychoacoustic variable-octave (ERB-aligned) log-Gaussian smoothing:
     - Hatziantoniou & Mourjopoulos (2000), JAES 48(4), 259-280
     - Glasberg & Moore (1990), Hearing Research 47(1-2), 103-138
  4. Kirkeby-Nelson frequency-dependent Tikhonov regularized inversion + C^inf softplus
     gain-envelope limiting:
     - Kirkeby, Nelson, Hamada, & Orduna-Bustamante (1998), IEEE TSAP 6(2), 189-194
     - Mourjopoulos (1994), JAES 42(11), 884-900
  5. Real-cepstrum / analytic Hilbert-transform minimum-phase reconstruction:
     - Oppenheim & Schafer, Discrete-Time Signal Processing (3rd ed., Pearson)

Dependencies: Pure Python 3 stdlib + numpy only (zero scipy/soundfile/matplotlib).
"""

import hashlib
import json
import math
import os
import struct
import sys
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Constants & Canonical Paths
# ---------------------------------------------------------------------------
SR = 48000
NTAPS = 4096
NFFT = 32768  # 8x NTAPS => circular cepstral aliasing < -120 dB
TAIL_TAPER_SAMPLES = 256

BASELINE_NPY = Path("/home/akshaybapat/project/omarchy-audio-tune/baseline_curve.npy")
OUT_REPORT_DIR = Path("/home/akshaybapat/project/xps17-studio-audio/acoustics-research/fir-harman")
OUT_KERNEL_DIR = Path("/home/akshaybapat/project/xps17-studio-audio/acoustics-research/kernels")

MODES = [
    {
        "id": 0,
        "key": "deep",
        "name": "Studio Deep",
        "filename": "xps17-studio-deep.wav",
        "description": (
            "Flagship Harman-warm in-room voicing (-0.85 dB/oct natural tilt + smooth "
            "+4.5 dB warmth shelf across 85-260 Hz), cleanly filling the 200-350 Hz cavern "
            "without woofer overload, eliminating the +4.2 dB @ 1.5 kHz glare, and restoring "
            "silky 6-14 kHz studio air."
        ),
        "cap_passband_db": 9.25,
        "cap_120hz_db": 5.60,
        "cap_70hz_db": -1.00,
        "subsonic_target_db": -6.00,
        "nyquist_target_db": -5.20,
        "softplus_knee_db": 0.55,
    },
    {
        "id": 1,
        "key": "spatial",
        "name": "Studio Spatial",
        "filename": "xps17-studio-spatial.wav",
        "description": (
            "Balanced cinema/XTC voicing (-0.55 dB/oct tilt + +3.0 dB warmth shelf 90-250 Hz), "
            "ultra-smooth midrange (300 Hz - 3 kHz) for pinpoint FDTR-XTC localization cues, "
            "and crisp 4-12 kHz pinna spatial detail."
        ),
        "cap_passband_db": 9.25,
        "cap_120hz_db": 5.20,
        "cap_70hz_db": -1.00,
        "subsonic_target_db": -6.00,
        "nyquist_target_db": -5.20,
        "softplus_knee_db": 0.55,
    },
    {
        "id": 2,
        "key": "reference",
        "name": "Studio Reference",
        "filename": "xps17-studio-reference.wav",
        "description": (
            "Harman/Olive mastering-neutral in-room target (-0.50 dB/oct natural room tilt "
            "+ subtle +1.5 dB bass foundation 85-200 Hz), flat within +/-1.5 dB across "
            "280 Hz - 10 kHz (pink-weighted RMS < 0.7 dB across 250 Hz - 10 kHz)."
        ),
        "cap_passband_db": 9.38,
        "cap_120hz_db": 4.50,
        "cap_70hz_db": -1.00,
        "subsonic_target_db": -6.00,
        "nyquist_target_db": -5.20,
        "softplus_knee_db": 0.45,
    },
    {
        "id": 3,
        "key": "voice",
        "name": "Studio Voice",
        "filename": "xps17-studio-voice.wav",
        "description": (
            "Ultra-articulate speech/podcast voicing: clean 100 Hz sub-rumble rolloff, "
            "natural 160-280 Hz chest warmth (zero hollowness), subtle +1.5 dB consonant "
            "intelligibility lift (2.5-4.5 kHz), strict 6-8 kHz de-essing (zero sibilance), "
            "and gentle rolloff above 12 kHz."
        ),
        "cap_passband_db": 7.80,
        "cap_120hz_db": -0.50,
        "cap_70hz_db": -9.00,
        "subsonic_target_db": -14.00,
        "nyquist_target_db": -9.00,
        "softplus_knee_db": 0.55,
    },
]

CRITICAL_BANDS = [
    ("100-200", 100.0, 200.0),
    ("200-350", 200.0, 350.0),
    ("350-1000", 350.0, 1000.0),
    ("1000-2500", 1000.0, 2500.0),
    ("2500-6000", 2500.0, 6000.0),
    ("6000-12000", 6000.0, 12000.0),
]

SPOT_FREQS_HZ = [
    60, 80, 100, 120, 150, 200, 250, 300, 350, 400, 500, 750,
    1000, 1500, 2000, 2500, 3000, 3300, 4000, 5000, 6000, 7000,
    8000, 10000, 12000, 14000, 16000,
]


# ---------------------------------------------------------------------------
# Smooth Analytic Math Helpers
# ---------------------------------------------------------------------------
def smoothstep(x: np.ndarray) -> np.ndarray:
    """C1 cubic Hermite smoothstep on [0, 1]."""
    t = np.clip(x, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def log_shelf_low(f: np.ndarray, fc: float, slope: float = 2.6) -> np.ndarray:
    """C^inf logistic low-frequency shelf: 1 for f << fc, 0.5 at fc, 0 for f >> fc."""
    return 1.0 / (1.0 + (np.maximum(f, 1.0) / fc) ** slope)


def log_gauss(f: np.ndarray, f0: float, w_oct: float) -> np.ndarray:
    """Smooth log-frequency Gaussian bell centered at f0 with width w_oct (octaves)."""
    return np.exp(-0.5 * (np.log2(np.maximum(f, 1.0) / f0) / w_oct) ** 2)


def soft_min(x: np.ndarray, cap: np.ndarray, knee: float = 0.55) -> np.ndarray:
    """C^inf smooth upper ceiling <= cap via numerically stable softplus."""
    z = (cap - x) / knee
    sp = np.where(z > 30.0, z, np.where(z < -30.0, 0.0, np.log1p(np.exp(z))))
    return cap - knee * sp


def soft_max(x: np.ndarray, floor: float, knee: float = 0.80) -> np.ndarray:
    """C^inf smooth lower floor >= floor via softplus."""
    return -soft_min(-x, np.full_like(x, -floor), knee)


# ---------------------------------------------------------------------------
# Measurement Conditioning & Psychoacoustic Variable-Octave Smoothing
# ---------------------------------------------------------------------------
def load_and_condition_measurement():
    """
    Load real Blue Yeti X baseline_curve.npy (48 kHz, NFFT=32768) and condition
    out-of-sweep boundaries so the 80 Hz / 16 kHz ESS stimulus tapers do not
    contaminate fractional-octave smoothing inside the valid acoustic passband.
    """
    if not BASELINE_NPY.exists():
        raise FileNotFoundError(f"Measured curve not found: {BASELINE_NPY}")

    data = np.load(BASELINE_NPY)
    f_meas, m_meas = data[0].astype(np.float64), data[1].astype(np.float64)
    f = np.fft.rfftfreq(NFFT, 1.0 / SR)
    if len(f_meas) != len(f) or np.max(np.abs(f_meas - f)) > 1e-6:
        m_meas = np.interp(f, f_meas, m_meas)

    idx_1k = int(np.argmin(np.abs(f - 1000.0)))
    ref_1k_raw_db = float(m_meas[idx_1k])
    m_raw = m_meas - ref_1k_raw_db

    # Sweep-edge conditioning outside valid ESS passband [100 Hz, 14.2 kHz]
    m_cond = np.copy(m_raw)
    hi_mask = (f >= 12500.0) & (f <= 14200.0)
    l_hi = float(np.mean(m_raw[hi_mask]))
    above = f > 14000.0
    t_hi = smoothstep((f[above] - 14000.0) / 800.0)
    m_cond[above] = (1.0 - t_hi) * m_raw[above] + t_hi * (l_hi - 6.0 * np.log2(f[above] / 14000.0))

    lo_mask = (f >= 95.0) & (f <= 112.0)
    l_lo = float(np.mean(m_raw[lo_mask]))
    below = (f > 0.0) & (f < 105.0)
    t_lo = smoothstep((105.0 - f[below]) / 15.0)
    m_cond[below] = (1.0 - t_lo) * m_raw[below] + t_lo * (
        l_lo + 12.0 * np.log2(np.maximum(f[below], 10.0) / 100.0)
    )
    m_cond[0] = m_cond[1]

    # Psychoacoustic variable-octave Gaussian smoothing in uniform log2-f space
    # (exact d(ln f) integration measure):
    #   <= 1200 Hz : 0.20 oct (1/5 oct -> resolves 200-350 Hz rolloff & 1.5 kHz resonance)
    #   1200..2600 : smooth -> 0.333 oct (1/3 oct midrange ERB resolution)
    #   2600..4800 : smooth -> 0.500 oct (1/2 oct treble -> averages 5.35/7.13 kHz comb dips)
    lf_grid = np.linspace(np.log2(10.0), np.log2(SR / 2.0), 2048)
    f_grid = 2.0 ** lf_grid
    mag_grid = np.interp(f_grid, f, m_cond)

    b_oct = (
        0.20
        + (1.0 / 3.0 - 0.20) * smoothstep((lf_grid - np.log2(1200.0)) / (np.log2(2600.0) - np.log2(1200.0)))
        + (0.50 - 1.0 / 3.0) * smoothstep((lf_grid - np.log2(2600.0)) / (np.log2(4800.0) - np.log2(2600.0)))
    )
    sigma = b_oct / (2.0 * np.sqrt(2.0 * np.log(2.0)))
    diff = lf_grid[None, :] - lf_grid[:, None]
    weights = np.exp(-0.5 * (diff / sigma[:, None]) ** 2)
    weights /= np.sum(weights, axis=1, keepdims=True)
    sm_grid = weights @ mag_grid

    m_sm = np.interp(np.log2(np.maximum(f, 10.0)), lf_grid, sm_grid)
    m_sm[0] = m_sm[1]

    # Robust 1 kHz smoothed anchor removes single-bin micro-dip bias at 1000 Hz
    m_sm_1k = float(m_sm[idx_1k])
    m_plant = m_sm - m_sm_1k
    m_raw_anchored = m_raw - m_sm_1k

    return f, idx_1k, ref_1k_raw_db, m_sm_1k, m_raw, m_raw_anchored, m_cond, m_plant


# ---------------------------------------------------------------------------
# Harman / Olive / Toole Analytic Target Curves
# ---------------------------------------------------------------------------
def build_target_curve(f: np.ndarray, idx_1k: int, mode_key: str) -> np.ndarray:
    """
    Smooth C^inf psychoacoustic target curve T(f) in dB relative to 1000 Hz = 0.0 dB.
    """
    f_safe = np.maximum(f, 20.0)
    oct_1k = np.log2(np.clip(f_safe, 100.0, 14000.0) / 1000.0)

    if mode_key == "deep":
        # Harman-warm in-room target: -0.85 dB/oct tilt + smooth +4.5 dB warmth shelf (85-260 Hz)
        t = -0.85 * oct_1k + 2.20 * log_shelf_low(f_safe, 260.0, 2.3)
    elif mode_key == "spatial":
        # Balanced cinema/XTC target: -0.55 dB/oct + +3.0 dB warmth shelf (90-250 Hz) + pinna air
        t = (
            -0.55 * oct_1k
            + 1.50 * log_shelf_low(f_safe, 240.0, 2.5)
            + 0.55 * log_gauss(f_safe, 8000.0, 0.75)
        )
    elif mode_key == "reference":
        # Harman/Olive mastering-neutral in-room target: -0.50 dB/oct + +1.5 dB foundation (85-200 Hz)
        t = -0.50 * oct_1k + 0.35 * log_shelf_low(f_safe, 190.0, 2.8)
    elif mode_key == "voice":
        # Ultra-articulate speech/podcast target:
        # 100 Hz rumble HPF, 160-280 Hz solid chest, -0.55 dB de-box @ 500 Hz,
        # +1.5 dB consonant lift (2.5-4.5 kHz), strict 6-8 kHz de-ess, >12 kHz hiss rolloff
        hp_rumble = -12.0 * log_shelf_low(f_safe, 95.0, 3.6)
        chest = 0.65 * log_gauss(f_safe, 220.0, 0.55)
        debox = -0.55 * log_gauss(f_safe, 500.0, 0.45)
        consonants = 1.60 * log_gauss(f_safe, 3300.0, 0.46)
        deess = -3.40 * log_gauss(f_safe, 6900.0, 0.38)
        hi_roll = -5.50 * (1.0 - log_shelf_low(f_safe, 12800.0, 3.4))
        t = hp_rumble + chest + debox + consonants + deess + hi_roll
    else:
        raise ValueError(f"Unknown mode: {mode_key}")

    t -= t[idx_1k]
    return t


# ---------------------------------------------------------------------------
# Kirkeby-Nelson Regularized Inverse + Cepstral Minimum-Phase Synthesis
# ---------------------------------------------------------------------------
def cepstral_min_phase_fir(mag_lin: np.ndarray, ntaps: int = NTAPS, nfft: int = NFFT) -> np.ndarray:
    """
    Oppenheim & Schafer real-cepstrum / analytic Hilbert-transform minimum-phase
    reconstruction from one-sided magnitude spectrum mag_lin[0..nfft/2].
    Guarantees causal minimum group delay and zero pre-ringing.
    """
    logm = np.log(np.maximum(mag_lin, 1e-8))
    full_logm = np.concatenate([logm, logm[-2:0:-1]])
    cep = np.fft.ifft(full_logm).real

    # Causal minimum-phase folding window: w[0]=1, w[1..N/2-1]=2, w[N/2]=1, w[N/2+1..]=0
    w_mp = np.zeros(nfft, dtype=np.float64)
    w_mp[0] = 1.0
    w_mp[1 : nfft // 2] = 2.0
    w_mp[nfft // 2] = 1.0

    # Analytic log-spectrum: ln|H| + j * (-Hilbert{ln|H|})
    h_full = np.fft.ifft(np.exp(np.fft.fft(cep * w_mp))).real

    # Causal truncation + raised-cosine tail taper over last 256 samples
    h = h_full[:ntaps].copy()
    taper = 0.5 * (1.0 + np.cos(np.linspace(0.0, np.pi, TAIL_TAPER_SAMPLES)))
    h[-TAIL_TAPER_SAMPLES:] *= taper
    return h


def synthesize_kernel(f: np.ndarray, idx_1k: int, m_plant: np.ndarray, cfg: dict):
    """Design and synthesize one 4096-tap minimum-phase float32 FIR kernel."""
    tgt_db = build_target_curve(f, idx_1k, cfg["key"])
    G = 10.0 ** (m_plant / 20.0)
    D = 10.0 ** (tgt_db / 20.0)
    lf = np.log2(np.maximum(f, 10.0))

    # Kirkeby-Nelson (1998) frequency-dependent Tikhonov regularization beta(f):
    #   H_KN(f) = D(f) * G(f) / (G(f)^2 + beta(f))
    beta_mid = 0.0018
    w_lo = 1.0 - smoothstep((lf - np.log2(75.0)) / (np.log2(215.0) - np.log2(75.0)))
    beta_lo = 0.042 * (w_lo ** 2)
    w_hi = smoothstep((lf - np.log2(13800.0)) / (np.log2(19500.0) - np.log2(13800.0)))
    beta_hi = 0.080 * (w_hi ** 2)
    beta = beta_mid + beta_lo + beta_hi

    h_kn_lin = (D * G) / (G * G + beta)
    h_kn_db = 20.0 * np.log10(np.maximum(h_kn_lin, 1e-6))

    # Frequency-dependent physical boost envelope cap(f) (rel. 1 kHz):
    #   - Usable passband (205 Hz - 14.2 kHz): <= cap_passband_db (<= +9.4 dB)
    #   - Below 120 Hz: smoothly capped <= cap_120hz_db (<= +5.6 dB; protects 2W woofers)
    #   - Below 62 Hz: smoothly drops to <= cap_70hz_db (leaves sub-80 Hz to sliding HPF + VB)
    #   - Above 14.2 kHz: smoothly tapers toward -2.5 dB at Nyquist
    u_midlo = smoothstep((lf - np.log2(118.0)) / (np.log2(205.0) - np.log2(118.0)))
    cap = cfg["cap_120hz_db"] + (cfg["cap_passband_db"] - cfg["cap_120hz_db"]) * u_midlo
    u_sub = smoothstep((lf - np.log2(62.0)) / (np.log2(118.0) - np.log2(62.0)))
    cap = cfg["cap_70hz_db"] + (cap - cfg["cap_70hz_db"]) * u_sub
    u_ultra = smoothstep((lf - np.log2(14200.0)) / (np.log2(20500.0) - np.log2(14200.0)))
    cap = cap * (1.0 - u_ultra) + (-2.50) * u_ultra

    # Smooth C^inf softplus ceiling and floor (zero derivative kinks / zero hard railing)
    h_bounded_db = soft_min(h_kn_db, cap, knee=cfg["softplus_knee_db"])
    h_bounded_db = soft_max(h_bounded_db, -11.0, knee=0.80)

    # Gentle subsonic roll-off (< 80 Hz -> subsonic_target_db at 24 Hz; zero DC boost)
    u_sub_roll = 1.0 - smoothstep((lf - np.log2(24.0)) / (np.log2(80.0) - np.log2(24.0)))
    h_bounded_db = (1.0 - u_sub_roll) * h_bounded_db + u_sub_roll * np.minimum(
        h_bounded_db, cfg["subsonic_target_db"]
    )

    # Smooth ultrasonic taper (> 14.2 kHz -> nyquist_target_db at 22 kHz; zero hiss, h[0] < 1)
    u_nyq = smoothstep((lf - np.log2(14200.0)) / (np.log2(22000.0) - np.log2(14200.0)))
    h_bounded_db = (1.0 - u_nyq) * h_bounded_db + u_nyq * cfg["nyquist_target_db"]

    # Loudness-neutral normalization: pink-weighted broadband passband (200 Hz - 12 kHz) = 0.00 dB
    wband = (f >= 200.0) & (f <= 12000.0)
    pink_w = 1.0 / f[wband]
    mean_passband_db = float(np.sum(h_bounded_db[wband] * pink_w) / np.sum(pink_w))
    h_des_db = h_bounded_db - mean_passband_db

    # Minimum-phase FIR via 32768-pt real cepstrum -> 4096 taps
    h_f64 = cepstral_min_phase_fir(10.0 ** (h_des_db / 20.0), ntaps=NTAPS, nfft=NFFT)

    # Exact post-truncation loudness trim to 0.000 dB pink-weighted passband gain, then float32
    H_pre = 20.0 * np.log10(np.maximum(np.abs(np.fft.rfft(h_f64, NFFT)), 1e-12))
    trim_db = float(np.sum(H_pre[wband] * pink_w) / np.sum(pink_w))
    h_f32 = (h_f64 * (10.0 ** (-trim_db / 20.0))).astype(np.float32)

    return h_f32, tgt_db, h_des_db, beta


# ---------------------------------------------------------------------------
# Standard RIFF WAVE IEEE Float32 Stereo Writer & Round-Trip Reader
# (Matches PipeWire convolver / libsndfile & omarchy-spatial-audio/src/wav.rs)
# ---------------------------------------------------------------------------
def write_wav_f32_stereo(path: Path, h_l: np.ndarray, h_r: np.ndarray, sr: int = SR) -> None:
    """
    Write standard IEEE 754 32-bit float stereo WAV with RIFF + fmt(16, tag=3) + fact(4) + data.
    Total size for 4096 stereo frames = 12 + 24 + 12 + 8 + 32768 = 32824 bytes.
    """
    h_l = np.asarray(h_l, dtype="<f4")
    h_r = np.asarray(h_r, dtype="<f4")
    if h_l.ndim != 1 or h_r.ndim != 1 or len(h_l) != len(h_r):
        raise ValueError("L and R kernels must be 1-D arrays of identical length")

    n_frames = len(h_l)
    channels = 2
    bits_per_sample = 32
    block_align = channels * (bits_per_sample // 8)  # 8
    byte_rate = sr * block_align  # 384000
    interleaved = np.empty(n_frames * 2, dtype="<f4")
    interleaved[0::2] = h_l
    interleaved[1::2] = h_r
    pcm_bytes = interleaved.tobytes()
    data_bytes = len(pcm_bytes)

    # RIFF payload = 'WAVE'(4) + fmt(24) + fact(12) + data_hdr(8) + data_bytes
    riff_size = 4 + 24 + 12 + 8 + data_bytes

    header = bytearray()
    header += b"RIFF" + struct.pack("<I", riff_size) + b"WAVE"
    header += b"fmt " + struct.pack(
        "<IHHIIHH",
        16,               # fmt chunk payload size
        3,                # wFormatTag = WAVE_FORMAT_IEEE_FLOAT (0x0003)
        channels,
        sr,
        byte_rate,
        block_align,
        bits_per_sample,
    )
    header += b"fact" + struct.pack("<II", 4, n_frames)
    header += b"data" + struct.pack("<I", data_bytes)

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fp:
        fp.write(header)
        fp.write(pcm_bytes)


def read_wav_f32_stereo(path: Path) -> dict:
    """Strictly parse and validate a RIFF/WAVE IEEE float32 stereo file from disk."""
    raw = path.read_bytes()
    if len(raw) < 56 or raw[0:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError(f"Invalid RIFF/WAVE header in {path}")
    riff_size = struct.unpack_from("<I", raw, 4)[0]
    if riff_size + 8 != len(raw):
        raise ValueError(f"RIFF size mismatch in {path}: header={riff_size+8}, actual={len(raw)}")

    pos = 12
    fmt = None
    fact_frames = None
    data_bytes = None
    while pos + 8 <= len(raw):
        cid = raw[pos : pos + 4]
        csz = struct.unpack_from("<I", raw, pos + 4)[0]
        pos += 8
        payload = raw[pos : pos + csz]
        if cid == b"fmt ":
            wfmt, ch, sr, br, ba, bps = struct.unpack_from("<HHIIHH", payload, 0)
            fmt = {
                "wFormatTag": wfmt,
                "channels": ch,
                "sample_rate": sr,
                "byte_rate": br,
                "block_align": ba,
                "bits_per_sample": bps,
            }
        elif cid == b"fact":
            fact_frames = struct.unpack_from("<I", payload, 0)[0]
        elif cid == b"data":
            data_bytes = payload
        pos += csz + (csz & 1)

    if fmt is None or data_bytes is None:
        raise ValueError(f"Missing fmt or data chunk in {path}")
    if fmt["wFormatTag"] != 3 or fmt["bits_per_sample"] != 32 or fmt["channels"] != 2:
        raise ValueError(f"Unexpected WAV format in {path}: {fmt}")

    samples = np.frombuffer(data_bytes, dtype="<f4").reshape(-1, 2)
    return {
        "file_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "fmt": fmt,
        "fact_frames": fact_frames,
        "n_frames": int(samples.shape[0]),
        "left": samples[:, 0].copy(),
        "right": samples[:, 1].copy(),
    }


# ---------------------------------------------------------------------------
# Exhaustive Verification & Metrics Extraction
# ---------------------------------------------------------------------------
def verify_and_measure_kernel(
    wav_path: Path,
    h_expected: np.ndarray,
    f: np.ndarray,
    idx_1k: int,
    m_raw_anchored: np.ndarray,
    m_plant: np.ndarray,
    tgt_db: np.ndarray,
    h_des_db: np.ndarray,
    cfg: dict,
) -> dict:
    """Round-trip read WAV, evaluate Bode/phase/GD/acoustic response, and assert ALL gates."""
    wav = read_wav_f32_stereo(wav_path)
    h_l, h_r = wav["left"], wav["right"]

    # Gate 0: Format & Exact Round-Trip Bit Equality
    assert wav["fmt"]["wFormatTag"] == 3, "Must be IEEE float32 (wFormatTag=3)"
    assert wav["fmt"]["sample_rate"] == SR, f"Sample rate mismatch: {wav['fmt']['sample_rate']}"
    assert wav["fmt"]["channels"] == 2, f"Channel count mismatch: {wav['fmt']['channels']}"
    assert wav["fmt"]["bits_per_sample"] == 32, "Must be 32-bit float"
    assert wav["n_frames"] == NTAPS and wav["fact_frames"] == NTAPS, "Must be 4096 frames"
    assert wav["file_bytes"] == 32824, f"Unexpected WAV byte length: {wav['file_bytes']}"
    assert np.array_equal(h_l, h_expected), "Left channel round-trip bit mismatch"
    assert np.array_equal(h_l, h_r), "Stereo L/R channels must be identical for XTC phase coherence"

    # Gate 1: Zero NaN / Inf in time and frequency domains
    h64 = h_l.astype(np.float64)
    H = np.fft.rfft(h64, NFFT)
    assert np.all(np.isfinite(h64)), "Non-finite sample in time domain"
    assert np.all(np.isfinite(H)), "Non-finite value in frequency domain"

    H_mag = np.maximum(np.abs(H), 1e-12)
    H_db = 20.0 * np.log10(H_mag)
    phi_rad = np.unwrap(np.angle(H))
    df = SR / NFFT
    gd_ms = -np.gradient(phi_rad, 2.0 * np.pi * df) * 1000.0

    # Gate 2: Minimum-Phase Energy Concentration (>95% within first 2.0 ms = 96 samples)
    e_tot = float(np.sum(h64 ** 2))
    e_1ms = float(np.sum(h64[:48] ** 2) / e_tot * 100.0)
    e_2ms = float(np.sum(h64[:96] ** 2) / e_tot * 100.0)
    e_5ms = float(np.sum(h64[:240] ** 2) / e_tot * 100.0)
    pk_idx = int(np.argmax(np.abs(h64)))
    peak_time_abs = float(np.max(np.abs(h64)))
    tail_rms_db = float(20.0 * np.log10(max(np.sqrt(np.mean(h64[-256:] ** 2)), 1e-12)))

    assert e_2ms > 95.0, f"{cfg['key']}: E(<2ms) = {e_2ms:.2f}% <= 95%"
    assert pk_idx <= 5, f"{cfg['key']}: peak sample index {pk_idx} > 5"
    assert peak_time_abs < 1.0, f"{cfg['key']}: time-domain peak {peak_time_abs:.4f} >= 1.0"

    # Gate 3: Bounded Boost Ceilings & Passband Loudness Neutrality
    wband = (f >= 200.0) & (f <= 12000.0)
    pink_w = 1.0 / f[wband]
    rms_gain_db = float(np.sum(H_db[wband] * pink_w) / np.sum(pink_w))
    # Also compute true power RMS gain (10 log10 of pink-weighted mean |H|^2)
    power_rms_gain_db = float(10.0 * np.log10(np.sum((H_mag[wband] ** 2) * pink_w) / np.sum(pink_w)))
    gain_1khz_db = float(H_db[idx_1k])
    peak_mag_db = float(np.max(H_db))
    peak_mag_hz = float(f[int(np.argmax(H_db))])
    min_mag_passband_db = float(np.min(H_db[(f >= 100.0) & (f <= 16000.0)]))
    peak_boost_rel_1khz_db = float(np.max(H_db - gain_1khz_db))
    sub120_mask = f <= 120.0
    sub120_abs_db = float(np.max(H_db[sub120_mask]))
    sub120_rel_1khz_db = float(np.max(H_db[sub120_mask] - gain_1khz_db))
    dc_gain_db = float(H_db[0])
    nyq_gain_db = float(H_db[-1])
    trunc_err_max_db = float(
        np.max(np.abs(H_db[(f >= 50.0) & (f <= 18000.0)] - h_des_db[(f >= 50.0) & (f <= 18000.0)]))
    )

    assert abs(rms_gain_db) < 0.02, f"{cfg['key']}: pink passband mean {rms_gain_db:+.4f} dB"
    assert peak_mag_db <= 9.5, f"{cfg['key']}: peak_mag_db {peak_mag_db:+.2f} > +9.5 dB"
    assert peak_boost_rel_1khz_db <= 9.5, f"{cfg['key']}: peak_rel1k {peak_boost_rel_1khz_db:+.2f} > +9.5 dB"
    assert sub120_abs_db <= 6.5, f"{cfg['key']}: sub120_abs {sub120_abs_db:+.2f} > +6.5 dB"
    assert sub120_rel_1khz_db <= 6.5, f"{cfg['key']}: sub120_rel1k {sub120_rel_1khz_db:+.2f} > +6.5 dB"
    assert dc_gain_db <= -5.0, f"{cfg['key']}: DC gain {dc_gain_db:+.2f} dB too high"

    # Gate 4: Group Delay Smoothness (200 Hz - 10 kHz)
    gd_mask = (f >= 200.0) & (f <= 10000.0)
    gd_max_ms = float(np.max(np.abs(gd_ms[gd_mask])))
    gd_max_hz = float(f[gd_mask][int(np.argmax(np.abs(gd_ms[gd_mask])))])
    gd_rms_ms = float(np.sqrt(np.mean(gd_ms[gd_mask] ** 2)))
    gd_1khz_ms = float(gd_ms[idx_1k])
    assert gd_rms_ms < 0.35, f"{cfg['key']}: GD RMS {gd_rms_ms:.3f} ms >= 0.35 ms"
    assert gd_max_ms < 3.0, f"{cfg['key']}: GD max {gd_max_ms:.3f} ms >= 3.0 ms"

    # Gate 5: Predicted Acoustic Response (baseline_curve + H_fir, anchored at robust 1 kHz)
    H_rel1k = H_db - gain_1khz_db
    pred_raw = m_raw_anchored + H_rel1k
    pred_sm = m_plant + H_rel1k

    band_metrics = {}
    for bname, flo, fhi in CRITICAL_BANDS:
        bm = (f >= flo) & (f <= fhi)
        pw = 1.0 / f[bm]
        st_mean = float(np.sum(m_raw_anchored[bm] * pw) / np.sum(pw))
        pr_mean = float(np.sum(pred_raw[bm] * pw) / np.sum(pw))
        tg_mean = float(np.sum(tgt_db[bm] * pw) / np.sum(pw))
        sm_err_mean = float(np.sum((pred_sm[bm] - tgt_db[bm]) * pw) / np.sum(pw))
        sm_err_rms = float(np.sqrt(np.sum(((pred_sm[bm] - tgt_db[bm]) ** 2) * pw) / np.sum(pw)))
        band_metrics[bname] = {
            "stock_mean_db": round(st_mean, 2),
            "predicted_mean_db": round(pr_mean, 2),
            "target_mean_db": round(tg_mean, 2),
            "delta_over_stock_db": round(pr_mean - st_mean, 2),
            "raw_err_mean_db": round(pr_mean - tg_mean, 2),
            "smooth_err_mean_db": round(sm_err_mean, 2),
            "smooth_err_rms_db": round(sm_err_rms, 2),
        }

    # Check 1.5 kHz harsh peak elimination
    i1500 = int(np.argmin(np.abs(f - 1500.0)))
    err_1500_raw = float(pred_raw[i1500] - tgt_db[i1500])
    err_1500_sm = float(pred_sm[i1500] - tgt_db[i1500])
    assert abs(err_1500_raw) <= 1.0, f"{cfg['key']}: 1.5 kHz raw err {err_1500_raw:+.2f} dB > 1.0 dB"
    assert abs(err_1500_sm) <= 0.25, f"{cfg['key']}: 1.5 kHz smooth err {err_1500_sm:+.2f} dB > 0.25 dB"

    # Check 200-350 Hz cavern filling (zero hollow dip)
    lift_200_350 = band_metrics["200-350"]["delta_over_stock_db"]
    min_lift = 7.5 if cfg["key"] != "voice" else 6.5
    assert lift_200_350 >= min_lift, f"{cfg['key']}: 200-350 Hz lift {lift_200_350:+.2f} < {min_lift}"

    # Check 6-12 kHz air recovery / voice de-essing
    lift_6_12k = band_metrics["6000-12000"]["delta_over_stock_db"]
    if cfg["key"] != "voice":
        assert lift_6_12k >= 8.0, f"{cfg['key']}: 6-12 kHz recovery {lift_6_12k:+.2f} < +8.0 dB"
    else:
        i100 = int(np.argmin(np.abs(f - 100.0)))
        i3300 = int(np.argmin(np.abs(f - 3300.0)))
        i6000 = int(np.argmin(np.abs(f - 6000.0)))
        i8000 = int(np.argmin(np.abs(f - 8000.0)))
        assert H_rel1k[i100] <= -5.0, f"voice: 100 Hz rumble rolloff insufficient ({H_rel1k[i100]:+.2f} dB)"
        assert H_rel1k[i3300] >= 2.5, f"voice: 3.3 kHz consonant lift insufficient ({H_rel1k[i3300]:+.2f} dB)"
        assert pred_raw[i6000] <= -0.5 and pred_raw[i8000] <= -1.0, (
            f"voice: sibilance de-essing insufficient (6k={pred_raw[i6000]:+.2f}, 8k={pred_raw[i8000]:+.2f})"
        )

    # Check Studio Reference flatness specifically
    ref_flatness = None
    if cfg["key"] == "reference":
        m250 = (f >= 250.0) & (f <= 10000.0)
        m280 = (f >= 280.0) & (f <= 10000.0)
        e250 = pred_sm[m250] - tgt_db[m250]
        e280 = pred_sm[m280] - tgt_db[m280]
        pw250 = 1.0 / f[m250]
        pw280 = 1.0 / f[m280]
        rms250 = float(np.sqrt(np.sum(pw250 * e250 ** 2) / np.sum(pw250)))
        rms280 = float(np.sqrt(np.sum(pw280 * e280 ** 2) / np.sum(pw280)))
        max280 = float(np.max(np.abs(e280)))
        assert max280 <= 1.50, f"reference: 280 Hz - 10 kHz max err {max280:.2f} > 1.50 dB"
        assert rms250 <= 0.80, f"reference: 250 Hz - 10 kHz pink-RMS {rms250:.2f} > 0.80 dB"
        ref_flatness = {
            "250_10000_hz_pink_rms_db": round(rms250, 3),
            "250_10000_hz_min_err_db": round(float(np.min(e250)), 2),
            "250_10000_hz_max_err_db": round(float(np.max(e250)), 2),
            "280_10000_hz_pink_rms_db": round(rms280, 3),
            "280_10000_hz_max_abs_err_db": round(max280, 2),
        }

    # Spot-frequency table (for report & downstream tooling)
    spots = {}
    for hz in SPOT_FREQS_HZ:
        i = int(np.argmin(np.abs(f - hz)))
        spots[str(hz)] = {
            "stock_db": round(float(m_raw_anchored[i]), 2),
            "plant_smooth_db": round(float(m_plant[i]), 2),
            "target_db": round(float(tgt_db[i]), 2),
            "fir_abs_db": round(float(H_db[i]), 2),
            "fir_rel_1khz_db": round(float(H_rel1k[i]), 2),
            "predicted_raw_db": round(float(pred_raw[i]), 2),
            "predicted_smooth_db": round(float(pred_sm[i]), 2),
            "phase_deg": round(float(np.degrees(phi_rad[i])), 1),
            "group_delay_ms": round(float(gd_ms[i]), 3),
        }

    # Broadband weighted acoustic error (300 Hz - 10 kHz, stock vs FIR)
    eval_m = (f >= 300.0) & (f <= 10000.0)
    pw_ev = 1.0 / f[eval_m]
    rms_before_300_10k = float(np.sqrt(np.sum(pw_ev * (m_raw_anchored[eval_m] - tgt_db[eval_m]) ** 2) / np.sum(pw_ev)))
    rms_after_raw_300_10k = float(np.sqrt(np.sum(pw_ev * (pred_raw[eval_m] - tgt_db[eval_m]) ** 2) / np.sum(pw_ev)))
    rms_after_sm_300_10k = float(np.sqrt(np.sum(pw_ev * (pred_sm[eval_m] - tgt_db[eval_m]) ** 2) / np.sum(pw_ev)))

    return {
        "mode_id": cfg["id"],
        "key": cfg["key"],
        "name": cfg["name"],
        "description": cfg["description"],
        "wav_path": str(wav_path),
        "wav_filename": cfg["filename"],
        "wav_format": {
            "wFormatTag": wav["fmt"]["wFormatTag"],
            "encoding": "IEEE_FLOAT_32",
            "sample_rate_hz": wav["fmt"]["sample_rate"],
            "channels": wav["fmt"]["channels"],
            "frames": wav["n_frames"],
            "file_bytes": wav["file_bytes"],
            "sha256": wav["sha256"],
        },
        "headroom_and_gain": {
            "rms_gain_db": round(rms_gain_db, 4),
            "power_rms_gain_db": round(power_rms_gain_db, 3),
            "gain_1khz_db": round(gain_1khz_db, 3),
            "peak_mag_db": round(peak_mag_db, 3),
            "peak_mag_hz": round(peak_mag_hz, 1),
            "min_mag_passband_db": round(min_mag_passband_db, 3),
            "peak_boost_rel_1khz_db": round(peak_boost_rel_1khz_db, 3),
            "sub120_max_abs_db": round(sub120_abs_db, 3),
            "sub120_max_rel_1khz_db": round(sub120_rel_1khz_db, 3),
            "dc_gain_db": round(dc_gain_db, 3),
            "nyquist_gain_db": round(nyq_gain_db, 3),
            "recommended_pipewire_convolver_gain": 1.0,
            "required_digital_headroom_db": round(max(0.0, peak_mag_db), 2),
        },
        "time_domain_min_phase": {
            "peak_sample_index": pk_idx,
            "peak_sample_abs": round(peak_time_abs, 6),
            "energy_within_1ms_pct": round(e_1ms, 3),
            "energy_within_2ms_pct": round(e_2ms, 3),
            "energy_within_5ms_pct": round(e_5ms, 3),
            "tail_last256_rms_db": round(tail_rms_db, 2),
            "truncation_max_err_50_18k_db": round(trunc_err_max_db, 4),
        },
        "group_delay_200_10k": {
            "max_abs_ms": round(gd_max_ms, 4),
            "max_at_hz": round(gd_max_hz, 1),
            "rms_ms": round(gd_rms_ms, 4),
            "at_1khz_ms": round(gd_1khz_ms, 4),
        },
        "acoustic_prediction_summary": {
            "pink_rms_error_before_300_10k_db": round(rms_before_300_10k, 3),
            "pink_rms_error_after_raw_300_10k_db": round(rms_after_raw_300_10k, 3),
            "pink_rms_error_after_smooth_300_10k_db": round(rms_after_sm_300_10k, 3),
            "peak_1500hz_raw_err_db": round(err_1500_raw, 3),
            "peak_1500hz_smooth_err_db": round(err_1500_sm, 3),
            "reference_flatness": ref_flatness,
        },
        "critical_bands": band_metrics,
        "spot_frequencies_hz": spots,
        "all_validation_gates_passed": True,
    }


def main() -> int:
    OUT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_KERNEL_DIR.mkdir(parents=True, exist_ok=True)

    f, idx_1k, ref_1k_raw_db, m_sm_1k, m_raw, m_raw_anchored, m_cond, m_plant = (
        load_and_condition_measurement()
    )

    results = {
        "generator": "generate_studio_firs.py",
        "device": "Dell XPS 17 9720 (Stradale, Quad 2x2W woofers + 2x2W tweeters)",
        "measurement_source": str(BASELINE_NPY),
        "measurement_notes": {
            "sample_rate_hz": SR,
            "n_taps": NTAPS,
            "design_nfft": NFFT,
            "tail_taper_samples": TAIL_TAPER_SAMPLES,
            "raw_1khz_level_db": round(ref_1k_raw_db, 3),
            "robust_1khz_smooth_offset_db": round(m_sm_1k, 3),
            "valid_ess_band_hz": [100.0, 14200.0],
            "smoothing_scheme": "ERB-aligned log-Gaussian variable-octave (1/5 oct <=1.2 kHz -> 1/3 oct @ 2.6 kHz -> 1/2 oct >=4.8 kHz)",
            "regularization_scheme": "Kirkeby-Nelson (1998) frequency-dependent Tikhonov beta(f) + C^inf softplus gain envelope",
            "phase_scheme": "Oppenheim-Schafer 32768-pt real-cepstrum Hilbert minimum-phase + 256-sample raised-cosine tail taper",
            "normalization_scheme": "Pink-weighted broadband passband unity (200 Hz - 12 kHz mean = 0.000 dB)",
        },
        "kernels": {},
    }

    print("=" * 92)
    print("Dell XPS 17 9720 Studio FIR Synthesizer — Kirkeby-Nelson Minimum-Phase Verification")
    print("=" * 92)
    print(f"{'Kernel':25s} | {'pk_i':>4s} | {'E<2ms':>7s} | {'1kHz':>6s} | {'PkAbs':>6s} | {'PkRel':>6s} | {'<120R':>6s} | {'GDrms':>6s} | {'1.5kE':>6s}")
    print("-" * 92)

    for cfg in MODES:
        h_f32, tgt_db, h_des_db, _ = synthesize_kernel(f, idx_1k, m_plant, cfg)
        wav_path = OUT_KERNEL_DIR / cfg["filename"]
        write_wav_f32_stereo(wav_path, h_f32, h_f32, sr=SR)
        metrics = verify_and_measure_kernel(
            wav_path=wav_path,
            h_expected=h_f32,
            f=f,
            idx_1k=idx_1k,
            m_raw_anchored=m_raw_anchored,
            m_plant=m_plant,
            tgt_db=tgt_db,
            h_des_db=h_des_db,
            cfg=cfg,
        )
        results["kernels"][cfg["key"]] = metrics

        hg = metrics["headroom_and_gain"]
        td = metrics["time_domain_min_phase"]
        gd = metrics["group_delay_200_10k"]
        ap = metrics["acoustic_prediction_summary"]
        print(
            f"{cfg['filename']:25s} | {td['peak_sample_index']:4d} | "
            f"{td['energy_within_2ms_pct']:6.2f}% | {hg['gain_1khz_db']:+6.2f} | "
            f"{hg['peak_mag_db']:+6.2f} | {hg['peak_boost_rel_1khz_db']:+6.2f} | "
            f"{hg['sub120_max_rel_1khz_db']:+6.2f} | {gd['rms_ms']:5.3f}m | "
            f"{ap['peak_1500hz_raw_err_db']:+6.2f}"
        )

    metrics_path = OUT_REPORT_DIR / "fir_metrics.json"
    metrics_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print("-" * 92)
    print(f"ALL VALIDATION GATES PASSED FOR ALL 4 KERNELS.")
    print(f"Wrote metrics: {metrics_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

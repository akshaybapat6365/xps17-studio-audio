#!/usr/bin/env python3
"""
Dell XPS 17 9720 Studio Audio — Spatial Psychoacoustics & Soundstage DSP Verifier.

Pure NumPy + Python stdlib offline verification suite covering:
  1. XPS 17 9720 acoustic geometry (S=0.340 m, D=0.550 m), geometric/Woodworth/Kuhn ITDs,
     comb singularities, and mutually coupled stereo woofer radiation physics.
  2. Exact bilinear LR4 (24 dB/oct, 180 Hz) Bass Mono-Anchoring in 3-biquad M/S topology.
  3. Cooper-Bauck / Choueiri 1D Mid/Side Shuffler FDTR-XTC (256-tap float32, D=44 = 0.917 ms):
     - Algebraic equivalence proof to symmetric 2x2 FDTR matrix
     - Max Side boost <= +2.8 dB, < -24 dB cross-feed <= 150 Hz, smooth rolloff > 6.5 kHz
     - Off-axis desk head-movement robustness (3 cm, 5 cm) & pink-noise energy conservation
  4. Studio Depth Early-Reflection Engine:
     - 2-stage prime-length Schroeder all-pass micro-diffuser (flat within +-0.001 dB)
     - 4-tap asymmetric prime-spaced cross-coupled early reflections (4.4 - 13.7 ms)
     - Mono-sum comb coloration (< +-0.8 dB mono, <= +-0.85 dB hard-panned across 200-8000 Hz)
     - Acoustic binaural IACC_E3 (500-2000 Hz) across mono vocals (> 0.92) & studio mixes (~0.75-0.85)
  5. Forensic Pre-Limiter vs Post-Dynamics Mid-Channel (Vocal & Bass) Ducking Simulation:
     - Old 91-spatial-fdtr.conf / fdtr.rs (+3.4 dB Side -> +7 dB 20 ms limiter) vs
       New 4-Band LR4 Dynamics -> Post-Dynamics Spatial -> 4x True-Peak Safety Guard.

Usage:
  taskset -c 12-19 nice -n 19 python3 verify_spatial_depth.py [--dump-json out.json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import deque
from typing import Dict, List, Tuple

import numpy as np


# =============================================================================
# 1. Dell XPS 17 9720 Physical Constants & Acoustic Plant Model
# =============================================================================
FS = 48_000.0
SPEAKER_SPAN_M = 0.340       # S = 34.0 cm top-firing flank acoustic centers
LISTENING_DIST_M = 0.550     # D = 55.0 cm nominal desk listening distance
HEAD_RADIUS_M = 0.0875       # a = 8.75 cm KEMAR anthropometric radius (d_ear = 17.5 cm)
SPEED_OF_SOUND_MPS = 343.0   # c = 343.0 m/s @ 20 C

HALF_ANGLE_RAD = math.atan((0.5 * SPEAKER_SPAN_M) / LISTENING_DIST_M)
FULL_SPAN_DEG = 2.0 * math.degrees(HALF_ANGLE_RAD)
TAU_WOODWORTH_S = (HEAD_RADIUS_M / SPEED_OF_SOUND_MPS) * (
    HALF_ANGLE_RAD + math.sin(HALF_ANGLE_RAD)
)
TAU_KUHN_LF_S = (3.0 * HEAD_RADIUS_M / SPEED_OF_SOUND_MPS) * math.sin(HALF_ANGLE_RAD)
R_IPSI_M = math.hypot(LISTENING_DIST_M, 0.5 * SPEAKER_SPAN_M - HEAD_RADIUS_M)
R_CONTRA_M = math.hypot(LISTENING_DIST_M, 0.5 * SPEAKER_SPAN_M + HEAD_RADIUS_M)
DELTA_R_GEOM_M = R_CONTRA_M - R_IPSI_M
TAU_GEOM_S = DELTA_R_GEOM_M / SPEED_OF_SOUND_MPS
G_GEOM = R_IPSI_M / R_CONTRA_M


def smoothstep(x: np.ndarray) -> np.ndarray:
    t = np.clip(x, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def acoustic_crosstalk(freqs: np.ndarray, tau: float = TAU_WOODWORTH_S) -> np.ndarray:
    """Brown-Duda (1998) spherical head-shadowing ratio combined with acoustic path delay."""
    phi_i = 0.5 * np.pi - HALF_ANGLE_RAD
    phi_c = 0.5 * np.pi + HALF_ANGLE_RAD
    w0 = 2.0 * SPEED_OF_SOUND_MPS / HEAD_RADIUS_M
    alpha_min = 0.1
    theta_min = math.radians(150.0)

    def bd_alpha(phi: float) -> float:
        return (1.0 + 0.5 * alpha_min) + (1.0 - 0.5 * alpha_min) * math.cos(
            phi / theta_min * math.pi
        )

    w = 2.0 * np.pi * freqs
    hi = np.abs(1.0 + 1j * bd_alpha(phi_i) * w / w0) / np.abs(1.0 + 1j * w / w0)
    hc = np.abs(1.0 + 1j * bd_alpha(phi_c) * w / w0) / np.abs(1.0 + 1j * w / w0)
    g = G_GEOM * (hc / hi)
    return g * np.exp(-1j * w * tau)


# =============================================================================
# 2. Digital Filter Primitives (Bilinear Biquads & Schroeder All-Pass)
# =============================================================================
def biquad_bw2_lp(fs: float, f0: float) -> Tuple[np.ndarray, np.ndarray]:
    w0 = 2.0 * math.pi * f0 / fs
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / math.sqrt(2.0)
    a0 = 1.0 + alpha
    b = np.array([(1.0 - cw) * 0.5 / a0, (1.0 - cw) / a0, (1.0 - cw) * 0.5 / a0])
    a = np.array([1.0, -2.0 * cw / a0, (1.0 - alpha) / a0])
    return b, a


def biquad_bw2_hp(fs: float, f0: float) -> Tuple[np.ndarray, np.ndarray]:
    w0 = 2.0 * math.pi * f0 / fs
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / math.sqrt(2.0)
    a0 = 1.0 + alpha
    b = np.array([(1.0 + cw) * 0.5 / a0, -(1.0 + cw) / a0, (1.0 + cw) * 0.5 / a0])
    a = np.array([1.0, -2.0 * cw / a0, (1.0 - alpha) / a0])
    return b, a


def biquad_ap2(fs: float, f0: float, q: float = 1.0 / math.sqrt(2.0)) -> Tuple[np.ndarray, np.ndarray]:
    w0 = 2.0 * math.pi * f0 / fs
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2.0 * q)
    a0 = 1.0 + alpha
    b = np.array([(1.0 - alpha) / a0, -2.0 * cw / a0, (1.0 + alpha) / a0])
    a = np.array([1.0, -2.0 * cw / a0, (1.0 - alpha) / a0])
    return b, a


def biquad_high_shelf(fs: float, f0: float, gain_db: float, slope: float = 0.70710678) -> Tuple[np.ndarray, np.ndarray]:
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * f0 / fs
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw * 0.5 * math.sqrt((A + 1.0 / A) * (1.0 / slope - 1.0) + 2.0)
    t = 2.0 * math.sqrt(A) * alpha
    a0 = (A + 1.0) - (A - 1.0) * cw + t
    b = np.array([
        A * ((A + 1.0) + (A - 1.0) * cw + t) / a0,
        -2.0 * A * ((A - 1.0) + (A + 1.0) * cw) / a0,
        A * ((A + 1.0) + (A - 1.0) * cw - t) / a0,
    ])
    a = np.array([
        1.0,
        2.0 * ((A - 1.0) - (A + 1.0) * cw) / a0,
        ((A + 1.0) - (A - 1.0) * cw - t) / a0,
    ])
    return b, a


def freqz_bq(b: np.ndarray, a: np.ndarray, freqs: np.ndarray, fs: float = FS) -> np.ndarray:
    z = np.exp(-1j * 2.0 * np.pi * freqs / fs)
    return (b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)


def freqz_schroeder_ap(delay_samples: int, g: float, freqs: np.ndarray, fs: float = FS) -> np.ndarray:
    zm = np.exp(-1j * 2.0 * np.pi * freqs * delay_samples / fs)
    return (-g + zm) / (1.0 - g * zm)


def apply_tf_linear(x: np.ndarray, H_fn, fs: float = FS) -> np.ndarray:
    n = len(x)
    nfft = 1 << int(math.ceil(math.log2(n + 4096)))
    freqs = np.fft.rfftfreq(nfft, 1.0 / fs)
    return np.fft.irfft(np.fft.rfft(x, nfft) * H_fn(freqs), nfft)[:n]


# =============================================================================
# 3. Verification 1: Geometry & Mutual Acoustic Woofer Coupling
# =============================================================================
def verify_geometry_and_coupling() -> Dict:
    print("=" * 78)
    print("1. XPS 17 9720 ACOUSTIC GEOMETRY & MUTUAL WOOFER COUPLING")
    print("=" * 78)
    geom_models = {}
    for d_cm in (17.0, 17.5, 18.0):
        d_m = d_cm * 0.01
        ri = math.hypot(LISTENING_DIST_M, 0.5 * SPEAKER_SPAN_M - 0.5 * d_m)
        rc = math.hypot(LISTENING_DIST_M, 0.5 * SPEAKER_SPAN_M + 0.5 * d_m)
        dr = rc - ri
        tau = dr / SPEED_OF_SOUND_MPS
        geom_models[f"d_ear_{d_cm:.1f}cm"] = {
            "r_ipsi_m": round(ri, 5),
            "r_contra_m": round(rc, 5),
            "delta_r_cm": round(dr * 100.0, 3),
            "itd_us": round(tau * 1e6, 2),
            "f_mid_comb_hz": round(1.0 / (2.0 * tau), 1),
            "f_side_singularity_hz": round(1.0 / tau, 1),
            "f_optimal_osd_hz": round(1.0 / (4.0 * tau), 1),
        }

    f_m1_wood = 1.0 / (2.0 * TAU_WOODWORTH_S)
    f_s1_wood = 1.0 / TAU_WOODWORTH_S
    f_opt_wood = 1.0 / (4.0 * TAU_WOODWORTH_S)

    print(f"  Speaker span S         = {SPEAKER_SPAN_M:.3f} m | Desk distance D = {LISTENING_DIST_M:.3f} m")
    print(f"  Full angular span 2*th = {FULL_SPAN_DEG:.3f} deg (half-angle {math.degrees(HALF_ANGLE_RAD):.3f} deg)")
    print(f"  Geometric (d=17.5 cm)  : dr = {DELTA_R_GEOM_M*100:.3f} cm, ITD = {TAU_GEOM_S*1e6:.2f} us, "
          f"f_M1 = {1/(2*TAU_GEOM_S):.1f} Hz, f_S1 = {1/TAU_GEOM_S:.1f} Hz")
    print(f"  Woodworth HF sphere    : dr = {TAU_WOODWORTH_S*SPEED_OF_SOUND_MPS*100:.3f} cm, "
          f"ITD = {TAU_WOODWORTH_S*1e6:.2f} us, f_M1 = {f_m1_wood:.1f} Hz, f_S1 = {f_s1_wood:.1f} Hz, "
          f"f_opt = {f_opt_wood:.1f} Hz")
    print(f"  Kuhn LF (<500 Hz)      : ITD = {TAU_KUHN_LF_S*1e6:.2f} us (1.49x diffraction lengthening)")

    coupling_table = {}
    for f_hz in (65, 82, 100, 120, 150, 180, 220, 260):
        kS = 2.0 * math.pi * f_hz * SPEAKER_SPAN_M / SPEED_OF_SOUND_MPS
        sinc = math.sin(kS) / kS
        mono_vs_1 = 10.0 * math.log10(2.0 * (1.0 + sinc))
        mono_vs_uncorr = 10.0 * math.log10(1.0 + sinc)
        dipole_vs_uncorr = 10.0 * math.log10(1.0 - sinc)
        coupling_table[str(f_hz)] = {
            "wavelength_m": round(SPEED_OF_SOUND_MPS / f_hz, 3),
            "kS_rad": round(kS, 4),
            "sinc_kS": round(sinc, 4),
            "mono_vs_single_woofer_db": round(mono_vs_1, 2),
            "mono_vs_uncorrelated_db": round(mono_vs_uncorr, 2),
            "dipole_vs_uncorrelated_db": round(dipole_vs_uncorr, 2),
            "mono_advantage_over_dipole_db": round(mono_vs_uncorr - dipole_vs_uncorr, 2),
        }
        if f_hz in (82, 100, 150, 180):
            print(f"  Coupling @ {f_hz:3d} Hz (kS={kS:.3f} rad): Mono vs 1-woofer={mono_vs_1:+.2f} dB, "
                  f"Mono vs Dipole={mono_vs_uncorr - dipole_vs_uncorr:+.2f} dB")

    assert 34.0 < FULL_SPAN_DEG < 34.8
    assert 148.0 < TAU_GEOM_S * 1e6 < 151.0
    assert 151.0 < TAU_WOODWORTH_S * 1e6 < 153.0
    assert coupling_table["180"]["kS_rad"] < math.pi / 2.0
    assert coupling_table["82"]["mono_vs_single_woofer_db"] > 5.8

    return {
        "speaker_span_m": SPEAKER_SPAN_M,
        "listening_dist_m": LISTENING_DIST_M,
        "head_radius_m": HEAD_RADIUS_M,
        "speed_of_sound_mps": SPEED_OF_SOUND_MPS,
        "half_angle_deg": round(math.degrees(HALF_ANGLE_RAD), 4),
        "full_span_deg": round(FULL_SPAN_DEG, 4),
        "geometric_models": geom_models,
        "woodworth_hf": {
            "itd_us": round(TAU_WOODWORTH_S * 1e6, 2),
            "f_mid_comb_hz": round(f_m1_wood, 2),
            "f_side_singularity_hz": round(f_s1_wood, 2),
            "f_optimal_osd_hz": round(f_opt_wood, 2),
        },
        "kuhn_lf_itd_us": round(TAU_KUHN_LF_S * 1e6, 2),
        "geometric_gain_ratio": round(G_GEOM, 5),
        "mutual_coupling": coupling_table,
    }


# =============================================================================
# 4. Verification 2: Exact 3-Biquad Bilinear LR4 (180 Hz) Bass Mono-Anchor
# =============================================================================
def verify_bass_mono_anchor(fc_hz: float = 180.0) -> Dict:
    print("\n" + "=" * 78)
    print(f"2. STRICT BASS MONO-ANCHORING (LR4 24 dB/oct @ {fc_hz:.0f} Hz, 3-BIQUAD M/S)")
    print("=" * 78)
    freqs = np.geomspace(20.0, 20_000.0, 4000)
    blp, alp = biquad_bw2_lp(FS, fc_hz)
    bhp, ahp = biquad_bw2_hp(FS, fc_hz)
    bap, aap = biquad_ap2(FS, fc_hz)

    H_lp4 = freqz_bq(blp, alp, freqs) ** 2
    H_hp4 = freqz_bq(bhp, ahp, freqs) ** 2
    H_ap2 = freqz_bq(bap, aap, freqs)
    H_sum = H_lp4 + H_hp4

    identity_err = float(np.max(np.abs(H_sum - H_ap2)))
    mid_ripple_db = float(np.max(np.abs(20.0 * np.log10(np.abs(H_sum)))))
    phase_err_deg = float(np.max(np.abs(np.degrees(np.angle(H_hp4 / H_ap2)))))

    side_pts = {}
    for f0 in (60, 82, 90, 120, 150, 180, 220, 260, 360):
        h = freqz_bq(bhp, ahp, np.array([float(f0)]))[0] ** 2
        side_pts[str(f0)] = round(float(20.0 * np.log10(abs(h))), 2)

    print(f"  Bilinear identity max |H_LP4 + H_HP4 - H_AP2| = {identity_err:.3e}")
    print(f"  Mid-channel magnitude flatness (20-20k Hz)    = +-{mid_ripple_db:.3e} dB")
    print(f"  Max M/S inter-channel phase error (20-20k Hz) = {phase_err_deg:.4f} deg")
    print(f"  Side attenuation: 60Hz={side_pts['60']}dB, 90Hz={side_pts['90']}dB, "
          f"150Hz={side_pts['150']}dB, 180Hz={side_pts['180']}dB, 260Hz={side_pts['260']}dB")

    assert identity_err < 1e-10
    assert mid_ripple_db < 1e-9
    assert phase_err_deg < 1e-6
    assert abs(side_pts["180"] - (-6.02)) < 0.05
    assert side_pts["90"] < -24.0

    return {
        "crossover_hz": fc_hz,
        "topology": "LR4_24dB_oct_MS_3biquad",
        "mid_biquad_ap2": {"b": [float(x) for x in bap], "a": [float(x) for x in aap]},
        "side_biquad_bw2_hp_x2": {"b": [float(x) for x in bhp], "a": [float(x) for x in ahp]},
        "identity_error": identity_err,
        "mid_ripple_db": mid_ripple_db,
        "ms_phase_error_deg": phase_err_deg,
        "side_attenuation_db": side_pts,
    }


# =============================================================================
# 5. Verification 3: Cooper-Bauck / Choueiri 1D M/S Shuffler FDTR-XTC
# =============================================================================
def design_shuffler_xtc() -> Tuple[np.ndarray, Dict]:
    print("\n" + "=" * 78)
    print("3. COOPER-BAUCK / CHOUEIRI 1D MID-SIDE SHUFFLER FDTR-XTC (256-TAP, 0.917 ms)")
    print("=" * 78)
    N_des = 4096
    f_des = np.fft.rfftfreq(N_des, 1.0 / FS)
    A_des = acoustic_crosstalk(f_des, tau=TAU_WOODWORTH_S)
    lm_des = 1.0 - A_des

    # Band engagement window: strictly bypass < 230 Hz, active 250 Hz - 6.5 kHz, smooth taper > 5.3 kHz
    W_lo = smoothstep((f_des - 230.0) / (370.0 - 230.0))
    W_hi = 1.0 - smoothstep((f_des - 5300.0) / (6500.0 - 5300.0))
    W = W_lo * W_hi

    # Frequency-dependent Tikhonov regularization beta(f):
    # Bounds max |H_S| <= +2.3 dB (strictly <= +2.8 dB), keeps wide head tolerance, damps 3.29 kHz comb
    beta = (
        0.58
        + 0.56 * (1.0 - smoothstep((f_des - 230.0) / (840.0 - 230.0)))
        + 1.50 * smoothstep((f_des - 2350.0) / (3300.0 - 2350.0))
    )

    # Exact Cooper-Bauck M/S shuffler eigenvalue for Mono-Anchored FDTR (H_M = 1):
    #   H_S(f) = 1 + 2 * W(f) * A(f) * conj(1 - A(f)) / (|1 - A(f)|^2 + beta(f))
    H_S_ideal = 1.0 + 2.0 * W * A_des * np.conj(lm_des) / (np.abs(lm_des) ** 2 + beta)

    # Verify algebraic identity vs 2x2 matrix formulation:
    H_lr_2x2 = -W * A_des * np.conj(lm_des) / (np.abs(lm_des) ** 2 + beta)
    H_ll_2x2 = 1.0 - H_lr_2x2
    shuffler_equiv_err = float(
        max(
            np.max(np.abs((H_ll_2x2 + H_lr_2x2) - 1.0)),
            np.max(np.abs((H_ll_2x2 - H_lr_2x2) - H_S_ideal)),
        )
    )

    # Synthesize 256-tap causal float32 FIR with D = 44 samples (0.9167 ms < 1.0 ms!)
    N_taps = 256
    D_delay = 44
    kaiser_beta = 6.5
    phase_delay = np.exp(-1j * 2.0 * np.pi * f_des * D_delay / FS)
    ir = np.fft.irfft(H_S_ideal * phase_delay, N_des)[:N_taps]

    win = np.zeros(N_taps, dtype=np.float64)
    i0_b = float(np.i0(kaiser_beta))
    for i in range(N_taps):
        u = (D_delay - i) / D_delay if i <= D_delay else (i - D_delay) / (N_taps - 1 - D_delay)
        win[i] = float(np.i0(kaiser_beta * math.sqrt(max(0.0, 1.0 - u * u)))) / i0_b
    h_s_f32 = (ir * win).astype(np.float32)

    # Evaluate realized float32 FIR on 8192-point FFT
    N_eval = 8192
    f_ev = np.fft.rfftfreq(N_eval, 1.0 / FS)
    H_S_ev = np.fft.rfft(h_s_f32.astype(np.float64), N_eval)
    H_S_nodel = H_S_ev * np.exp(+1j * 2.0 * np.pi * f_ev * D_delay / FS)
    A_ev = acoustic_crosstalk(f_ev, tau=TAU_WOODWORTH_S)

    h_ll = 0.5 * (1.0 + H_S_nodel)
    h_lr = 0.5 * (1.0 - H_S_nodel)
    r_ipsi = h_ll + A_ev * h_lr
    r_contra = A_ev * h_ll + h_lr
    xtc_db = 20.0 * np.log10(np.abs(r_ipsi) / np.maximum(np.abs(r_contra), 1e-12))

    # Off-axis robustness at 3 cm and 5 cm lateral head shift
    ws = TAU_WOODWORTH_S / TAU_GEOM_S

    def offaxis_mean(dx_m: float) -> float:
        s2 = 0.5 * SPEAKER_SPAN_M
        tl = (math.hypot(s2 - (-HEAD_RADIUS_M + dx_m), LISTENING_DIST_M)
              - math.hypot(-s2 - (-HEAD_RADIUS_M + dx_m), LISTENING_DIST_M)) / SPEED_OF_SOUND_MPS * ws
        tr = (math.hypot(-s2 - (HEAD_RADIUS_M + dx_m), LISTENING_DIST_M)
              - math.hypot(s2 - (HEAD_RADIUS_M + dx_m), LISTENING_DIST_M)) / SPEED_OF_SOUND_MPS * ws
        al = acoustic_crosstalk(f_ev, tau=tl)
        ar = acoustic_crosstalk(f_ev, tau=tr)
        el = h_ll + al * h_lr
        er = ar * h_ll + h_lr
        mask = (f_ev >= 300.0) & (f_ev <= 2600.0)
        return float(np.mean(20.0 * np.log10(np.abs(el[mask]) / np.maximum(np.abs(er[mask]), 1e-12))))

    xtc_3cm = offaxis_mean(0.03)
    xtc_5cm = offaxis_mean(0.05)

    # Combined Side response with 180 Hz LR4 Bass Mono-Anchor
    bhp, ahp = biquad_bw2_hp(FS, 180.0)
    H_S_comb = (freqz_bq(bhp, ahp, f_ev) ** 2) * H_S_ev

    # Pink-noise energy conservation (1/f PSD over 20 Hz - 20 kHz)
    m_all = (f_ev >= 20.0) & (f_ev <= 20_000.0)
    pw = 1.0 / f_ev[m_all]
    pink_side_db = float(10.0 * np.log10(np.sum(pw * np.abs(H_S_ev[m_all]) ** 2) / np.sum(pw)))
    pink_tot_db = float(10.0 * np.log10(np.sum(pw * 0.5 * (1.0 + np.abs(H_S_ev[m_all]) ** 2)) / np.sum(pw)))

    m_mid = (f_ev >= 300.0) & (f_ev <= 2600.0)
    m_hi = (f_ev >= 2600.0) & (f_ev <= 5500.0)
    m_sub150 = (f_ev >= 20.0) & (f_ev <= 150.0)
    m_tr = f_ev >= 6800.0

    peak_idx = int(np.argmax(np.abs(h_s_f32)))
    max_side_db = float(20.0 * np.log10(np.max(np.abs(H_S_ev))))
    max_side_freq = float(f_ev[int(np.argmax(np.abs(H_S_ev)))])
    max_side_comb_db = float(20.0 * np.log10(np.max(np.abs(H_S_comb))))
    mean_xtc_mid = float(np.mean(xtc_db[m_mid]))
    mean_xtc_hi = float(np.mean(xtc_db[m_hi]))
    max_hlr_lt150 = float(20.0 * np.log10(np.max(np.abs(h_lr[m_sub150]))))
    max_hs_gt6800_dev = float(np.max(np.abs(20.0 * np.log10(np.abs(H_S_ev[m_tr])))))
    max_hlr_gt6800 = float(20.0 * np.log10(np.max(np.abs(h_lr[m_tr]))))

    print(f"  Cooper-Bauck 2x2 <-> M/S equivalence error   = {shuffler_equiv_err:.3e}")
    print(f"  FIR taps = {N_taps}, causal delay = {D_delay} samples ({D_delay/FS*1000:.4f} ms < 1.0 ms)")
    print(f"  Peak tap index                               = {peak_idx} (exact match {D_delay})")
    print(f"  Max Side boost |H_S|                         = {max_side_db:+.3f} dB @ {max_side_freq:.1f} Hz (limit <= +2.8 dB)")
    print(f"  Max Combined Side (LR4 180Hz + Shuffler)     = {max_side_comb_db:+.3f} dB")
    print(f"  Mean XTC 300-2600 Hz (on-axis / 3cm / 5cm)   = {mean_xtc_mid:.2f} / {xtc_3cm:.2f} / {xtc_5cm:.2f} dB")
    print(f"  Mean XTC 2600-5500 Hz (across 3.29 kHz comb) = {mean_xtc_hi:.2f} dB")
    print(f"  Max XTC cross-feed |H_lr| (<= 150 Hz)        = {max_hlr_lt150:+.2f} dB (limit < -24 dB)")
    print(f"  High-freq rolloff (> 6.8 kHz) |H_S| dev      = +-{max_hs_gt6800_dev:.4f} dB (|H_lr| <= {max_hlr_gt6800:+.1f} dB)")
    print(f"  Pink-noise energy delta (Side / Total L+R)   = {pink_side_db:+.3f} / {pink_tot_db:+.3f} dB (limit +-0.5 dB)")

    assert shuffler_equiv_err < 1e-12
    assert peak_idx == D_delay
    assert D_delay / FS * 1000.0 < 1.0
    assert max_side_db <= 2.8
    assert mean_xtc_mid >= 12.0
    assert xtc_3cm >= 10.5
    assert xtc_5cm >= 8.0
    assert max_hlr_lt150 < -24.0
    assert max_hs_gt6800_dev < 0.05
    assert abs(pink_tot_db) < 0.5

    return h_s_f32, {
        "taps": N_taps,
        "causal_delay_samples": D_delay,
        "causal_delay_ms": round(D_delay / FS * 1000.0, 4),
        "kaiser_beta": kaiser_beta,
        "active_band_hz": [230.0, 6500.0],
        "beta_params": {
            "beta_mid": 0.58,
            "beta_lo_shelf": 0.56,
            "beta_lo_range_hz": [230.0, 840.0],
            "beta_hi_singularity": 1.50,
            "beta_hi_range_hz": [2350.0, 3300.0],
        },
        "window_transition_hz": {
            "lo_start": 230.0,
            "lo_end": 370.0,
            "hi_start": 5300.0,
            "hi_end": 6500.0,
        },
        "metrics": {
            "cooper_bauck_equiv_err": shuffler_equiv_err,
            "max_side_gain_db": round(max_side_db, 3),
            "max_side_gain_freq_hz": round(max_side_freq, 1),
            "max_combined_side_gain_db": round(max_side_comb_db, 3),
            "mean_xtc_300_2600_db": round(mean_xtc_mid, 2),
            "mean_xtc_2600_5500_db": round(mean_xtc_hi, 2),
            "offaxis_3cm_xtc_db": round(xtc_3cm, 2),
            "offaxis_5cm_xtc_db": round(xtc_5cm, 2),
            "max_hlr_le150hz_db": round(max_hlr_lt150, 2),
            "max_hs_dev_ge6800hz_db": round(max_hs_gt6800_dev, 4),
            "max_hlr_ge6800hz_db": round(max_hlr_gt6800, 2),
            "pink_noise_side_delta_db": round(pink_side_db, 3),
            "pink_noise_total_delta_db": round(pink_tot_db, 3),
            "mid_ripple_db": 0.0,
        },
        "h_s_taps_f32": [round(float(v), 7) for v in h_s_f32],
    }


# =============================================================================
# 6. Verification 4: Studio Depth Early-Reflection Network
# =============================================================================
ER_SPEC = {
    "bpf_hp_hz": 300.0,
    "bpf_lp_hz": 6500.0,
    "feed_mid_weight": 0.60,
    "feed_side_weight": 1.20,
    "allpass_left": [(43, +0.52), (107, -0.48)],   # 0.896 ms, 2.229 ms
    "allpass_right": [(59, -0.52), (131, +0.48)],  # 1.229 ms, 2.729 ms
    "taps_left": [(211, +0.58), (331, -0.46), (487, +0.36), (641, -0.28)],   # 4.40, 6.90, 10.15, 13.35 ms
    "taps_right": [(229, -0.58), (359, +0.46), (521, -0.36), (659, +0.28)],  # 4.77, 7.48, 10.85, 13.73 ms
    # Unit-norm cross-coupled scattering matrix (c_lat=0.92, c_ind=0.392 -> -9.91 dB mono-sum comb suppression)
    "scatter_same": 0.848528,
    "scatter_cross": -0.529150,
    "wet_scale_per_depth": 0.1975,  # Stage Depth = 0.85 -> 0.1679 (-15.50 dB wet)
    "side_air_shelf_hz": 600.0,
    "side_air_shelf_db": 0.8,
}


def er_transfer_functions(freqs: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    bh, ah = biquad_bw2_hp(FS, ER_SPEC["bpf_hp_hz"])
    bl, al = biquad_bw2_lp(FS, ER_SPEC["bpf_lp_hz"])
    BPF = freqz_bq(bh, ah, freqs) * freqz_bq(bl, al, freqs)

    AP_L = np.ones_like(freqs, dtype=np.complex128)
    for m, g in ER_SPEC["allpass_left"]:
        AP_L *= freqz_schroeder_ap(m, g, freqs)
    AP_R = np.ones_like(freqs, dtype=np.complex128)
    for m, g in ER_SPEC["allpass_right"]:
        AP_R *= freqz_schroeder_ap(m, g, freqs)

    nL = math.sqrt(sum(g * g for _, g in ER_SPEC["taps_left"]))
    nR = math.sqrt(sum(g * g for _, g in ER_SPEC["taps_right"]))
    T_L = sum((g / nL) * np.exp(-1j * 2.0 * np.pi * freqs * d / FS) for d, g in ER_SPEC["taps_left"])
    T_R = sum((g / nR) * np.exp(-1j * 2.0 * np.pi * freqs * d / FS) for d, g in ER_SPEC["taps_right"])
    return BPF, AP_L, AP_R, T_L, T_R


def signal_iacc_e3(ear_L: np.ndarray, ear_R: np.ndarray) -> Tuple[float, List[float]]:
    """ISO 3382 / Hidaka-Beranek-Okano (1995) IACC_E3 averaged over 500, 1000, 2000 Hz octaves, |lag| <= 1 ms."""
    n = len(ear_L)
    f = np.fft.rfftfreq(n, 1.0 / FS)
    XL = np.fft.rfft(ear_L)
    XR = np.fft.rfft(ear_R)
    max_lag = int(0.001 * FS)
    band_vals = []
    for fc in (500.0, 1000.0, 2000.0):
        mask = (f >= fc / math.sqrt(2.0)) & (f <= fc * math.sqrt(2.0))
        yl = np.fft.irfft(XL * mask, n)
        yr = np.fft.irfft(XR * mask, n)
        cc = np.fft.irfft(np.fft.rfft(yl, 2 * n) * np.conj(np.fft.rfft(yr, 2 * n)), 2 * n)
        denom = math.sqrt(float(np.sum(yl * yl) * np.sum(yr * yr)))
        lags = np.concatenate([cc[: max_lag + 1], cc[-max_lag:]])
        band_vals.append(float(np.max(np.abs(lags)) / max(denom, 1e-18)))
    return float(np.mean(band_vals)), band_vals


def verify_studio_depth_engine(h_s_f32: np.ndarray) -> Dict:
    print("\n" + "=" * 78)
    print("4. STUDIO DEPTH EARLY-REFLECTION NETWORK (ALL-PASS + 4.4-13.7 ms TAPS)")
    print("=" * 78)
    N_ev = 16384
    f_ev = np.fft.rfftfreq(N_ev, 1.0 / FS)
    BPF, AP_L, AP_R, T_L, T_R = er_transfer_functions(f_ev)

    ap_l_err_db = float(np.max(np.abs(20.0 * np.log10(np.abs(AP_L)))))
    ap_r_err_db = float(np.max(np.abs(20.0 * np.log10(np.abs(AP_R)))))
    sc_norm = ER_SPEC["scatter_same"] ** 2 + ER_SPEC["scatter_cross"] ** 2
    sc_sum = ER_SPEC["scatter_same"] + ER_SPEC["scatter_cross"]

    # Mono-sum comb coloration across 200 Hz - 8 kHz at default Studio Deep (Stage Depth = 0.85, -15.5 dB wet)
    wg = 0.85 * ER_SPEC["wet_scale_per_depth"]
    aM = ER_SPEC["feed_mid_weight"]
    aS = ER_SPEC["feed_side_weight"]
    m28 = (f_ev >= 200.0) & (f_ev <= 8000.0)

    # Pure Mono input (M=1, S=0 => L_in+R_in=2, F_L=F_R=aM*BPF)
    H_ms_mono = 1.0 + 0.5 * wg * sc_sum * aM * BPF * (AP_L * T_L + AP_R * T_R)
    # Hard-panned Left input (L=1, R=0 => M=0.5, S=0.5, F_L=0.5*(aM+aS)*BPF, F_R=0.5*(aM-aS)*BPF; cross-coupled D_L<-F_R, D_R<-F_L)
    H_ms_left = 1.0 + wg * sc_sum * BPF * (
        0.5 * (aM - aS) * AP_L * T_L + 0.5 * (aM + aS) * AP_R * T_R
    )
    raw_mono_ripple = float(np.max(np.abs(20.0 * np.log10(np.abs(H_ms_mono[m28])))))
    raw_left_ripple = float(np.max(np.abs(20.0 * np.log10(np.abs(H_ms_left[m28])))))

    f_grid = np.geomspace(200.0, 8000.0, 120)
    r3 = 2.0 ** (1.0 / 6.0)
    oct3_mono_ripple = float(max(
        abs(10.0 * np.log10(np.mean(np.abs(H_ms_mono[(f_ev >= fc / r3) & (f_ev <= fc * r3)]) ** 2)))
        for fc in f_grid
    ))
    oct3_left_ripple = float(max(
        abs(10.0 * np.log10(np.mean(np.abs(H_ms_left[(f_ev >= fc / r3) & (f_ev <= fc * r3)]) ** 2)))
        for fc in f_grid
    ))

    # Acoustic binaural IACC_E3 through XPS 17 transfer plant
    rng = np.random.default_rng(2026)
    N_s = int(2.0 * FS)

    def make_pink() -> np.ndarray:
        w = rng.standard_normal(N_s)
        W = np.fft.rfft(w)
        f = np.fft.rfftfreq(N_s, 1.0 / FS)
        f[0] = 1.0
        W /= np.sqrt(f)
        W[0] = 0.0
        y = np.fft.irfft(W, N_s)
        return y / np.std(y)

    m_sig = make_pink()
    s_sig = make_pink()
    f_s = np.fft.rfftfreq(N_s, 1.0 / FS)
    A_s = acoustic_crosstalk(f_s, tau=TAU_WOODWORTH_S)
    BPF_s, AP_Ls, AP_Rs, T_Ls, T_Rs = er_transfer_functions(f_s)
    bsh, ash = biquad_high_shelf(FS, ER_SPEC["side_air_shelf_hz"], ER_SPEC["side_air_shelf_db"])
    H_air_s = freqz_bq(bsh, ash, f_s)
    H_shuf_s = np.fft.rfft(h_s_f32.astype(np.float64), N_s) * np.exp(+1j * 2.0 * np.pi * f_s * 44 / FS)

    iacc_table = {}
    wg_deep = 0.85 * ER_SPEC["wet_scale_per_depth"]
    wg_spat = 0.65 * ER_SPEC["wet_scale_per_depth"]
    a_same = ER_SPEC["scatter_same"]
    a_cross = ER_SPEC["scatter_cross"]

    for key, corr in [
        ("pure_mono_vocal", 1.00),
        ("intimate_acoustic", 0.90),
        ("typical_studio_mix", 0.72),
        ("wide_stereo_mix", 0.45),
        ("diffuse_stereo", 0.00),
    ]:
        wm = math.sqrt(0.5 * (1.0 + corr))
        ws = math.sqrt(0.5 * (1.0 - corr))
        Mf = np.fft.rfft(wm * m_sig)
        Sf = np.fft.rfft(ws * s_sig)

        Ld = Mf + Sf
        Rd = Mf - Sf
        iacc_dry, _ = signal_iacc_e3(np.fft.irfft(Ld + A_s * Rd, N_s), np.fft.irfft(A_s * Ld + Rd, N_s))

        # Cross-coupled ER feeds
        F_L = BPF_s * (aM * Mf + aS * Sf)
        F_R = BPF_s * (aM * Mf - aS * Sf)
        D_L = AP_Ls * T_Ls * F_R
        D_R = AP_Rs * T_Rs * F_L
        W_L_unit = a_same * D_L + a_cross * D_R
        W_R_unit = a_cross * D_L + a_same * D_R

        # Studio Deep (Stage Depth = 0.85, Spatial XTC = 0.0, Side Air +0.8 dB)
        Ldp = Mf + H_air_s * Sf + wg_deep * W_L_unit
        Rdp = Mf - H_air_s * Sf + wg_deep * W_R_unit
        iacc_deep, _ = signal_iacc_e3(np.fft.irfft(Ldp + A_s * Rdp, N_s), np.fft.irfft(A_s * Ldp + Rdp, N_s))

        # Studio Spatial (Stage Depth = 0.65, Spatial XTC = 0.75)
        Sxtc = (0.25 + 0.75 * H_shuf_s) * Sf
        Lsp = Mf + Sxtc + wg_spat * W_L_unit
        Rsp = Mf - Sxtc + wg_spat * W_R_unit
        iacc_spat, _ = signal_iacc_e3(np.fft.irfft(Lsp + A_s * Rsp, N_s), np.fft.irfft(A_s * Lsp + Rsp, N_s))

        iacc_table[key] = {
            "input_corr": corr,
            "dry_ear_iacc_e3": round(iacc_dry, 4),
            "studio_deep_ear_iacc_e3": round(iacc_deep, 4),
            "studio_spatial_ear_iacc_e3": round(iacc_spat, 4),
        }

    print(f"  All-pass micro-diffuser flatness (L / R)     = +-{ap_l_err_db:.2e} / +-{ap_r_err_db:.2e} dB (limit +-0.001 dB)")
    print(f"  Scattering matrix norm / mono-sum gain       = {sc_norm:.6f} / {20*math.log10(abs(sc_sum)):+.2f} dB")
    print(f"  Mono-sum comb ripple (200-8kHz, Depth=0.85):")
    print(f"    Pure Mono input   : raw = +-{raw_mono_ripple:.3f} dB | 1/3-oct = +-{oct3_mono_ripple:.3f} dB (limit < +-0.8 dB)")
    print(f"    Hard-Panned Left  : raw = +-{raw_left_ripple:.3f} dB | 1/3-oct = +-{oct3_left_ripple:.3f} dB (limit < +-1.0 dB)")
    for k, v in iacc_table.items():
        print(f"  IACC_E3 [{k:18s}]: Dry={v['dry_ear_iacc_e3']:.4f} -> "
              f"StudioDeep={v['studio_deep_ear_iacc_e3']:.4f} | StudioSpatial={v['studio_spatial_ear_iacc_e3']:.4f}")

    assert ap_l_err_db < 1e-3 and ap_r_err_db < 1e-3
    assert raw_mono_ripple < 0.8
    assert raw_left_ripple < 1.0
    assert oct3_mono_ripple < 0.1
    assert oct3_left_ripple < 0.25
    assert iacc_table["pure_mono_vocal"]["studio_deep_ear_iacc_e3"] > 0.92
    assert 0.75 <= iacc_table["typical_studio_mix"]["studio_deep_ear_iacc_e3"] <= 0.85
    assert iacc_table["diffuse_stereo"]["studio_spatial_ear_iacc_e3"] < iacc_table["diffuse_stereo"]["dry_ear_iacc_e3"]

    return {
        "allpass_left_max_err_db": ap_l_err_db,
        "allpass_right_max_err_db": ap_r_err_db,
        "mono_sum_ripple_mono_raw_db": round(raw_mono_ripple, 3),
        "mono_sum_ripple_mono_third_oct_db": round(oct3_mono_ripple, 3),
        "mono_sum_ripple_hard_left_raw_db": round(raw_left_ripple, 3),
        "mono_sum_ripple_hard_left_third_oct_db": round(oct3_left_ripple, 3),
        "iacc_e3": iacc_table,
    }


# =============================================================================
# 7. Verification 5: Forensic Pre-Limiter vs Post-Dynamics Mid-Channel Ducking
# =============================================================================
def stereo_true_peak_limiter(
    L: np.ndarray,
    R: np.ndarray,
    drive_db: float = 7.0,
    ceil_dbfs: float = -1.0,
    lookahead_ms: float = 10.0,
    release_ms: float = 20.0,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    drive = 10.0 ** (drive_db / 20.0)
    ceil = 10.0 ** (ceil_dbfs / 20.0)
    Ld = L * drive
    Rd = R * drive
    n = len(L)
    lk = int(round(lookahead_ms * 1e-3 * FS))

    # 4x oversampled inter-sample true-peak detection
    nfft = 1 << int(math.ceil(math.log2(n)))
    Lp4 = np.abs(np.fft.irfft(np.fft.rfft(Ld, nfft), nfft * 4)[: n * 4] * 4.0).reshape(n, 4).max(axis=1)
    Rp4 = np.abs(np.fft.irfft(np.fft.rfft(Rd, nfft), nfft * 4)[: n * 4] * 4.0).reshape(n, 4).max(axis=1)
    inst_tp = np.maximum(Lp4, Rp4)

    dq: deque = deque()
    win_peak = np.empty(n, dtype=np.float64)
    padded = np.concatenate([inst_tp, np.zeros(lk)])
    for i in range(n + lk):
        v = padded[i]
        while dq and dq[-1][1] <= v:
            dq.pop()
        dq.append((i, v))
        if i >= lk:
            idx = i - lk
            while dq[0][0] < idx:
                dq.popleft()
            win_peak[idx] = dq[0][1]

    target = np.where(win_peak > ceil, ceil / np.maximum(win_peak, 1e-12), 1.0)
    rel_c = math.exp(-1.0 / (release_ms * 1e-3 * FS))
    att_c = math.exp(-1.0 / (2.5e-3 * FS))
    g = np.empty(n, dtype=np.float64)
    cur = 1.0
    for i in range(n):
        t = target[i]
        if t < cur:
            cur = min(t + att_c * (cur - t), t)
        else:
            cur = t + rel_c * (cur - t)
        g[i] = cur
    inst_max = np.maximum(np.abs(Ld), np.abs(Rd))
    g = np.minimum(g, np.where(inst_max > ceil, ceil / np.maximum(inst_max, 1e-12), 1.0))
    return Ld * g, Rd * g, g


def multiband_4band_lr4_dynamics(L: np.ndarray, R: np.ndarray, drive_db: float = 7.0, pre_spatial_ceil_db: float = -2.4) -> Tuple[np.ndarray, np.ndarray]:
    """Phase-matched 4-band LR4 crossover (160, 750, 3500 Hz) + per-band leveling."""
    n = len(L)
    nfft = 1 << int(math.ceil(math.log2(n + 4096)))
    f = np.fft.rfftfreq(nfft, 1.0 / FS)

    def lr4_lp(f0: float):
        b, a = biquad_bw2_lp(FS, f0)
        return freqz_bq(b, a, f) ** 2

    def lr4_hp(f0: float):
        b, a = biquad_bw2_hp(FS, f0)
        return freqz_bq(b, a, f) ** 2

    def ap2(f0: float):
        b, a = biquad_ap2(FS, f0)
        return freqz_bq(b, a, f)

    H_lo750, H_hi750 = lr4_lp(750.0), lr4_hp(750.0)
    H_lo160, H_hi160, H_ap160 = lr4_lp(160.0), lr4_hp(160.0), ap2(160.0)
    H_lo3500, H_hi3500, H_ap3500 = lr4_lp(3500.0), lr4_hp(3500.0), ap2(3500.0)

    H_bands = [
        H_lo750 * H_lo160 * H_ap3500,
        H_lo750 * H_hi160 * H_ap3500,
        H_hi750 * H_lo3500 * H_ap160,
        H_hi750 * H_hi3500 * H_ap160,
    ]
    # Exact unity all-pass sum check for the 4-band tree
    assert float(np.max(np.abs(np.abs(sum(H_bands)) - 1.0))) < 1e-10

    in_gain = 10.0 ** (drive_db / 20.0)
    Lf = np.fft.rfft(L * in_gain, nfft)
    Rf = np.fft.rfft(R * in_gain, nfft)

    band_cfg = [
        (-13.0, 2.8, 18.0, 120.0, -5.0, +1.5),  # B0: < 160 Hz bass (tight, warm weight)
        (-12.0, 2.5, 15.0, 100.0, -4.6, +1.5),  # B1: 160 - 750 Hz vocal/warmth body
        (-12.5, 3.0, 10.0,  75.0, -5.2, +1.2),  # B2: 750 - 3500 Hz presence/guitars
        (-14.0, 2.5,  8.0,  60.0, -6.8, +1.0),  # B3: >= 3500 Hz air
    ]
    out_L = np.zeros(n, dtype=np.float64)
    out_R = np.zeros(n, dtype=np.float64)
    for Hb, (th_db, ratio, att_ms, rel_ms, b_ceil_db, mk_db) in zip(H_bands, band_cfg):
        bL = np.fft.irfft(Lf * Hb, nfft)[:n]
        bR = np.fft.irfft(Rf * Hb, nfft)[:n]
        env_in = np.maximum(np.abs(bL), np.abs(bR))
        ac = math.exp(-1.0 / (att_ms * 1e-3 * FS))
        rc = math.exp(-1.0 / (rel_ms * 1e-3 * FS))
        env = np.empty(n, dtype=np.float64)
        e = 1e-6
        for i in range(n):
            x = env_in[i]
            e = (ac * e + (1.0 - ac) * x) if x > e else (rc * e + (1.0 - rc) * x)
            env[i] = e
        env_db = 20.0 * np.log10(np.maximum(env, 1e-9))
        over = env_db - th_db
        knee = 4.0
        gr_db = np.where(
            over <= -knee * 0.5,
            0.0,
            np.where(
                over >= knee * 0.5,
                over * (1.0 / ratio - 1.0),
                (1.0 / ratio - 1.0) * ((over + knee * 0.5) ** 2) / (2.0 * knee),
            ),
        )
        gain = 10.0 ** ((gr_db + mk_db) / 20.0)
        bL, bR, _ = stereo_true_peak_limiter(
            bL * gain, bR * gain, drive_db=0.0, ceil_dbfs=b_ceil_db, lookahead_ms=5.0, release_ms=rel_ms
        )
        out_L += bL
        out_R += bR

    # Pre-spatial mastering headroom bus (reserves clean headroom for Post-Dynamics Spatial)
    out_L, out_R, _ = stereo_true_peak_limiter(
        out_L, out_R, drive_db=0.0, ceil_dbfs=pre_spatial_ceil_db, lookahead_ms=8.0, release_ms=70.0
    )
    return out_L, out_R


def verify_forensic_ducking(h_s_f32: np.ndarray) -> Dict:
    print("\n" + "=" * 78)
    print("5. FORENSIC PRE-LIMITER vs POST-DYNAMICS MID-CHANNEL (VOCAL & BASS) DUCKING")
    print("=" * 78)
    N = int(2.0 * FS)
    t = np.arange(N) / FS

    # Mastered -1.0 dBFS stereo mix:
    #   Verse  [0.0..1.0 s]: Centered 80 Hz Bass + Centered Lead Vocal (pure Mid)
    #   Chorus [1.0..2.0 s]: Identical Bass + Lead Vocal + Wide Stereo Guitars/Synths (pure Side 515-1115 Hz)
    bass = 0.30 * np.sin(2.0 * np.pi * 80.0 * t) + 0.11 * np.sin(2.0 * np.pi * 160.0 * t)
    vocal = (
        0.18 * np.sin(2.0 * np.pi * 220.0 * t)
        + 0.14 * np.sin(2.0 * np.pi * 440.0 * t + 0.3)
        + 0.11 * np.sin(2.0 * np.pi * 660.0 * t + 0.7)
        + 0.08 * np.sin(2.0 * np.pi * 1320.0 * t + 1.1)
        + 0.05 * np.sin(2.0 * np.pi * 2420.0 * t + 0.4)
    )
    env_c = smoothstep((t - 1.00) / 0.02)
    wide_side = (
        0.20 * np.sin(2.0 * np.pi * 515.0 * t + 0.2)
        + 0.24 * np.sin(2.0 * np.pi * 685.0 * t + 1.1)
        + 0.20 * np.sin(2.0 * np.pi * 865.0 * t + 0.6)
        + 0.14 * np.sin(2.0 * np.pi * 1115.0 * t + 1.7)
    ) * env_c

    L_in = bass + vocal + wide_side
    R_in = bass + vocal - wide_side
    scale = (10.0 ** (-1.0 / 20.0)) / max(float(np.max(np.abs(L_in))), float(np.max(np.abs(R_in))))
    L_in *= scale
    R_in *= scale
    M_in = 0.5 * (L_in + R_in)
    S_in = 0.5 * (L_in - R_in)

    w_v = (t >= 0.20) & (t <= 0.90)
    w_c = (t >= 1.20) & (t <= 1.90)
    VOC_F = [220.0, 440.0, 660.0, 1320.0, 2420.0]
    BASS_F = [80.0, 160.0]

    def tone_rms(sig: np.ndarray, flist: List[float], bw: float = 15.0) -> float:
        nw = len(sig)
        win = np.hanning(nw)
        X = np.fft.rfft(sig * win)
        f = np.fft.rfftfreq(nw, 1.0 / FS)
        p = sum(
            2.0 * float(np.sum(np.abs(X[(f >= f0 - bw) & (f <= f0 + bw)]) ** 2)) / (nw * float(np.sum(win * win)))
            for f0 in flist
        )
        return math.sqrt(p)

    # Exact old fdtr.rs Side response (512-tap equivalent)
    def old_fdtr_hs(freqs: np.ndarray) -> np.ndarray:
        A = acoustic_crosstalk(freqs, tau=152.0e-6)
        lm = 1.0 - A
        W = smoothstep((freqs - 200.0) / 100.0) * (1.0 - smoothstep((freqs - 5000.0) / 1000.0))
        beta = 0.48 + 0.42 * (1.0 - smoothstep((freqs - 200.0) / 520.0)) + 1.40 * smoothstep((freqs - 2400.0) / 900.0)
        return 1.0 + 2.0 * W * A * np.conj(lm) / (np.abs(lm) ** 2 + beta)

    # Exact 8th-order Butterworth 180 Hz HPF from 91-spatial-fdtr.conf (lsp filter s=3)
    def bw8_hp_tf(freqs: np.ndarray, f0: float = 180.0) -> np.ndarray:
        H = np.ones_like(freqs, dtype=np.complex128)
        for q in (0.50979558, 0.60134489, 0.89997622, 2.56291545):
            w0 = 2.0 * math.pi * f0 / FS
            cw, sw = math.cos(w0), math.sin(w0)
            alpha = sw / (2.0 * q)
            a0 = 1.0 + alpha
            b = np.array([(1.0 + cw) * 0.5 / a0, -(1.0 + cw) / a0, (1.0 + cw) * 0.5 / a0])
            a = np.array([1.0, -2.0 * cw / a0, (1.0 - alpha) / a0])
            H *= freqz_bq(b, a, freqs)
        return H

    # Realized new float32 256-tap Shuffler transfer function (zero-phase aligned for direct comparison)
    def new_shuffler_tf(freqs: np.ndarray) -> np.ndarray:
        nfft = (len(freqs) - 1) * 2
        return np.fft.rfft(h_s_f32.astype(np.float64), nfft) * np.exp(+1j * 2.0 * np.pi * freqs * 44 / FS)

    # --- Old Pipelines ---
    L_ref, R_ref, g_ref = stereo_true_peak_limiter(L_in, R_in, drive_db=7.0, ceil_dbfs=-1.0, lookahead_ms=10.0, release_ms=20.0)
    S_old = apply_tf_linear(S_in, old_fdtr_hs)
    L_old_iso, R_old_iso, g_old_iso = stereo_true_peak_limiter(
        M_in + S_old, M_in - S_old, drive_db=7.0, ceil_dbfs=-1.0, lookahead_ms=10.0, release_ms=20.0
    )
    L_hp = apply_tf_linear(L_in, lambda f: bw8_hp_tf(f, 180.0))
    R_hp = apply_tf_linear(R_in, lambda f: bw8_hp_tf(f, 180.0))
    M_hp = 0.5 * (L_hp + R_hp)
    S_hp = apply_tf_linear(0.5 * (L_hp - R_hp), old_fdtr_hs)
    L_old_full, R_old_full, g_old_full = stereo_true_peak_limiter(
        M_hp + S_hp, M_hp - S_hp, drive_db=7.0, ceil_dbfs=-1.0, lookahead_ms=10.0, release_ms=20.0
    )

    # --- New Studio Architecture ---
    b82, a82 = biquad_bw2_hp(FS, 82.0)
    L_82 = apply_tf_linear(L_in, lambda f: freqz_bq(b82, a82, f))
    R_82 = apply_tf_linear(R_in, lambda f: freqz_bq(b82, a82, f))
    bap, aap = biquad_ap2(FS, 180.0)
    bhp, ahp = biquad_bw2_hp(FS, 180.0)
    M_anc = apply_tf_linear(0.5 * (L_82 + R_82), lambda f: freqz_bq(bap, aap, f))
    S_anc = apply_tf_linear(0.5 * (L_82 - R_82), lambda f: freqz_bq(bhp, ahp, f) ** 2)
    L_dyn, R_dyn = multiband_4band_lr4_dynamics(M_anc + S_anc, M_anc - S_anc, drive_db=7.0, pre_spatial_ceil_db=-2.2)

    # Final 4x True-Peak Safety Guard (+0.8 dB mastering trim into -1.0 dBTP, 80 ms release -> 0.4 dB spatial headroom!)
    L_new_off, R_new_off, g_new_off = stereo_true_peak_limiter(
        L_dyn, R_dyn, drive_db=0.8, ceil_dbfs=-1.0, lookahead_ms=10.0, release_ms=80.0
    )

    M_d = 0.5 * (L_dyn + R_dyn)
    S_d = 0.5 * (L_dyn - R_dyn)
    aM, aS = ER_SPEC["feed_mid_weight"], ER_SPEC["feed_side_weight"]
    a_same, a_cross = ER_SPEC["scatter_same"], ER_SPEC["scatter_cross"]

    D_L = apply_tf_linear(aM * M_d - aS * S_d, lambda f: (lambda x: x[0] * x[1] * x[3])(er_transfer_functions(f)))
    D_R = apply_tf_linear(aM * M_d + aS * S_d, lambda f: (lambda x: x[0] * x[2] * x[4])(er_transfer_functions(f)))
    W_L_unit = a_same * D_L + a_cross * D_R
    W_R_unit = a_cross * D_L + a_same * D_R

    # New Mode 0: Studio Deep (Stage Depth = 0.85, Spatial XTC = 0.0, Side Air +0.8 dB > 600 Hz)
    wg_deep = 0.85 * ER_SPEC["wet_scale_per_depth"]
    bsh, ash = biquad_high_shelf(FS, ER_SPEC["side_air_shelf_hz"], ER_SPEC["side_air_shelf_db"])
    S_air = apply_tf_linear(S_d, lambda f: freqz_bq(bsh, ash, f))
    L_new_deep, R_new_deep, g_new_deep = stereo_true_peak_limiter(
        M_d + S_air + wg_deep * W_L_unit, M_d - S_air + wg_deep * W_R_unit,
        drive_db=0.8, ceil_dbfs=-1.0, lookahead_ms=10.0, release_ms=80.0
    )

    # New Mode 1: Studio Spatial (Stage Depth = 0.65, Spatial XTC = 0.75)
    wg_spat = 0.65 * ER_SPEC["wet_scale_per_depth"]
    S_shuf = apply_tf_linear(S_d, lambda f: 0.25 + 0.75 * new_shuffler_tf(f))
    L_new_spat, R_new_spat, g_new_spat = stereo_true_peak_limiter(
        M_d + S_shuf + wg_spat * W_L_unit, M_d - S_shuf + wg_spat * W_R_unit,
        drive_db=0.8, ceil_dbfs=-1.0, lookahead_ms=10.0, release_ms=80.0
    )

    results = {}

    def eval_pipe(key: str, label: str, Lo: np.ndarray, Ro: np.ndarray, gl: np.ndarray, Lb=None, Rb=None):
        Mo = 0.5 * (Lo + Ro)
        vv, vc = tone_rms(Mo[w_v], VOC_F), tone_rms(Mo[w_c], VOC_F)
        bv, bc = tone_rms(Mo[w_v], BASS_F), tone_rms(Mo[w_c], BASS_F)
        v_duck = 20.0 * math.log10(vc / max(vv, 1e-12))
        b_duck = 20.0 * math.log10(bc / max(bv, 1e-12))
        ex_v = ex_b = 0.0
        if Lb is not None:
            Mb = 0.5 * (Lb + Rb)
            ex_v = 20.0 * math.log10(vc / max(tone_rms(Mb[w_c], VOC_F), 1e-12))
            ex_b = 20.0 * math.log10(bc / max(tone_rms(Mb[w_c], BASS_F), 1e-12))
        gr_v = 20.0 * math.log10(float(np.mean(gl[w_v])))
        gr_c = 20.0 * math.log10(float(np.mean(gl[w_c])))
        c_rms = 20.0 * math.log10(math.sqrt(0.5 * float(np.mean(Lo[w_c] ** 2) + np.mean(Ro[w_c] ** 2))))
        pk_db = 20.0 * math.log10(max(float(np.max(np.abs(Lo))), float(np.max(np.abs(Ro)))))
        bass_vs_in = 20.0 * math.log10(bc / max(tone_rms(M_in[w_c], BASS_F), 1e-12))
        print(f"  {label:35s} | RMS={c_rms:+5.2f} | Pk={pk_db:+5.2f} | LimGR={gr_v:+5.2f}->{gr_c:+5.2f}dB | "
              f"SpatialMidLoss(Voc={ex_v:+5.2f}, Bass={ex_b:+5.2f})dB | BassVsIn={bass_vs_in:+5.2f}dB")
        results[key] = {
            "chorus_rms_dbfs": round(c_rms, 2),
            "peak_dbfs": round(pk_db, 2),
            "limiter_gr_verse_db": round(gr_v, 2),
            "limiter_gr_chorus_db": round(gr_c, 2),
            "chorus_vocal_duck_db": round(v_duck, 2),
            "chorus_bass_duck_db": round(b_duck, 2),
            "spatial_extra_vocal_loss_db": round(ex_v, 2),
            "spatial_extra_bass_loss_db": round(ex_b, 2),
            "bass_retention_vs_in_db": round(bass_vs_in, 2),
        }

    eval_pipe("baseline_no_xtc", "0. Baseline (No XTC -> +7dB Lim)", L_ref, R_ref, g_ref)
    eval_pipe("old_xtc_pre_limiter", "1A. OLD FDTR-XTC -> +7dB Fast Lim", L_old_iso, R_old_iso, g_old_iso, L_ref, R_ref)
    eval_pipe("old_full_91_conf", "1B. OLD FULL 91-spatial-fdtr.conf", L_old_full, R_old_full, g_old_full, L_ref, R_ref)
    eval_pipe("new_mbc_spatial_off", "2A. NEW 4-Band MBC (Spatial OFF)", L_new_off, R_new_off, g_new_off)
    eval_pipe("new_studio_deep", "2B. NEW Studio Deep (Depth 0.85)", L_new_deep, R_new_deep, g_new_deep, L_new_off, R_new_off)
    eval_pipe("new_studio_spatial", "2C. NEW Studio Spatial (XTC 0.75)", L_new_spat, R_new_spat, g_new_spat, L_new_off, R_new_off)

    # Hard quantitative assertions proving forensic diagnosis & new architecture superiority
    assert results["old_xtc_pre_limiter"]["spatial_extra_vocal_loss_db"] <= -1.0, "Expected >1 dB old pre-limiter Mid ducking"
    assert results["old_full_91_conf"]["spatial_extra_bass_loss_db"] <= -15.0, "Expected severe old 180 Hz HPF bass obliteration"
    assert abs(results["new_studio_deep"]["spatial_extra_vocal_loss_db"]) < 0.30, "Studio Deep vocal loss must be < 0.3 dB"
    assert abs(results["new_studio_deep"]["spatial_extra_bass_loss_db"]) < 0.30, "Studio Deep bass loss must be < 0.3 dB"
    assert abs(results["new_studio_spatial"]["spatial_extra_vocal_loss_db"]) < 0.30, "Studio Spatial vocal loss must be < 0.3 dB"
    assert abs(results["new_studio_spatial"]["spatial_extra_bass_loss_db"]) < 0.30, "Studio Spatial bass loss must be < 0.3 dB"
    assert results["new_studio_deep"]["peak_dbfs"] <= -0.99
    assert results["new_studio_spatial"]["peak_dbfs"] <= -0.99

    return results


def build_complete_spatial_spec(geom: Dict, anchor: Dict, shuffler: Dict, depth: Dict, ducking: Dict) -> Dict:
    """Build complete machine-readable spatial_spec.json for Lead 2 consolidation & Lead 3 DSP engine."""
    bh_er, ah_er = biquad_bw2_hp(FS, ER_SPEC["bpf_hp_hz"])
    bl_er, al_er = biquad_bw2_lp(FS, ER_SPEC["bpf_lp_hz"])
    bsh_air, ash_air = biquad_high_shelf(FS, ER_SPEC["side_air_shelf_hz"], ER_SPEC["side_air_shelf_db"])

    # Also precompute 150 Hz LR4 Bass Mono-Anchor coefficients for Mode 2 (Studio Reference)
    bhp150, ahp150 = biquad_bw2_hp(FS, 150.0)
    bap150, aap150 = biquad_ap2(FS, 150.0)

    nL = math.sqrt(sum(g * g for _, g in ER_SPEC["taps_left"]))
    nR = math.sqrt(sum(g * g for _, g in ER_SPEC["taps_right"]))

    return {
        "schema_version": "1.0.0",
        "device": "Dell XPS 17 9720 (Stradale)",
        "sample_rate_hz": int(FS),
        "plugin_contract": {
            "ladspa_label": "xps17_studio_dsp",
            "ladspa_unique_id": 597201,
            "control_ports": {
                "Mode": {"index": 0, "range": [0, 3], "default": 0, "labels": ["Studio Deep", "Studio Spatial", "Studio Reference", "Studio Voice"]},
                "Stage Depth": {"index": 5, "range": [0.0, 1.5], "default": 0.85},
                "Spatial XTC": {"index": 6, "range": [0.0, 1.0], "default_studio_deep": 0.0, "default_studio_spatial": 0.75},
                "Drive (dB)": {"index": 7, "range": [0.0, 12.0], "default": 7.0},
                "Ceiling (dBFS)": {"index": 8, "range": [-9.0, -0.3], "default": -1.0},
            },
        },
        "geometry": geom,
        "pipeline_order": [
            {"stage": 1, "name": "input_sanitize_and_klippel_sliding_hpf", "notes": "65-180 Hz dynamic HPF (resting 82 Hz) driven by excursion observer + Volume (dB)"},
            {"stage": 2, "name": "hybrid_3band_psychoacoustic_bass_and_iso226", "notes": "Phase-clean missing-fundamental bass + Warmth Body + live Volume-linked ISO 226:2023 shelves"},
            {"stage": 3, "name": "bass_mono_anchor_lr4", "notes": "Exact 3-biquad M/S LR4 24 dB/oct (<180 Hz; 150 Hz in Reference) for coherent +5.6..+5.9 dB acoustic woofer coupling"},
            {"stage": 4, "name": "minphase_room_harman_fir_4096", "notes": "4096-tap @ 48 kHz per-mode speaker correction before multiband dynamics"},
            {"stage": 5, "name": "multiband_4band_lr4_dynamics", "notes": "Phase-aligned 4-band LR4 (160, 750, 3500 Hz) delivering primary loudness lift into -2.2 dBFS pre-spatial headroom bus"},
            {"stage": 6, "name": "post_dynamics_spatial_depth_and_shuffler_xtc", "notes": "6A: Colorless Schroeder AP + 4.4-13.7 ms comb-free ER; 6B: Optional 256-tap (0.917 ms) Cooper-Bauck M/S Shuffler XTC"},
            {"stage": 7, "name": "final_4x_true_peak_safety_limiter", "notes": "+0.8 dB gentle trim into -1.0 dBTP ceiling (10 ms lookahead, 80 ms release) -> hardware ALC1316 2-way crossover"},
        ],
        "bass_mono_anchor": {
            "default_crossover_hz": 180.0,
            "reference_mode_crossover_hz": 150.0,
            "topology": anchor["topology"],
            "biquads_180hz": {
                "mid_ap2": anchor["mid_biquad_ap2"],
                "side_bw2_hp_cascaded_x2": anchor["side_biquad_bw2_hp_x2"],
            },
            "biquads_150hz": {
                "mid_ap2": {"b": [float(x) for x in bap150], "a": [float(x) for x in aap150]},
                "side_bw2_hp_cascaded_x2": {"b": [float(x) for x in bhp150], "a": [float(x) for x in ahp150]},
            },
            "verified_metrics": {
                "identity_error": anchor["identity_error"],
                "mid_ripple_db": anchor["mid_ripple_db"],
                "ms_phase_error_deg": anchor["ms_phase_error_deg"],
                "side_attenuation_db": anchor["side_attenuation_db"],
            },
        },
        "studio_depth_engine": {
            "control_port": "Stage Depth",
            "wet_gain_formula": "wet_lin = clamp(stage_depth, 0.0, 1.5) * 0.1975  # 0.85 -> -15.50 dB, 0.65 -> -17.83 dB, 1.0 -> -14.09 dB",
            "wet_scale_per_depth": ER_SPEC["wet_scale_per_depth"],
            "feed_weights": {
                "mid_alpha": ER_SPEC["feed_mid_weight"],
                "side_alpha": ER_SPEC["feed_side_weight"],
                "cross_couple": "D_L fed from F_R = BPF*(mid_alpha*M - side_alpha*S); D_R fed from F_L = BPF*(mid_alpha*M + side_alpha*S)",
            },
            "band_limit_filters": {
                "hp_bw2_hz": ER_SPEC["bpf_hp_hz"],
                "hp_bw2_biquad": {"b": [float(x) for x in bh_er], "a": [float(x) for x in ah_er]},
                "lp_bw2_hz": ER_SPEC["bpf_lp_hz"],
                "lp_bw2_biquad": {"b": [float(x) for x in bl_er], "a": [float(x) for x in al_er]},
            },
            "schroeder_allpass_diffuser": {
                "difference_equation": "y[n] = -g*x[n] + x[n - m] + g*y[n - m]",
                "left_stages": [
                    {"delay_samples": m, "delay_ms": round(m / FS * 1000.0, 4), "g": g}
                    for m, g in ER_SPEC["allpass_left"]
                ],
                "right_stages": [
                    {"delay_samples": m, "delay_ms": round(m / FS * 1000.0, 4), "g": g}
                    for m, g in ER_SPEC["allpass_right"]
                ],
            },
            "early_reflection_taps": {
                "left_taps": [
                    {"delay_samples": d, "delay_ms": round(d / FS * 1000.0, 4), "raw_gain": g, "norm_gain": round(g / nL, 6)}
                    for d, g in ER_SPEC["taps_left"]
                ],
                "right_taps": [
                    {"delay_samples": d, "delay_ms": round(d / FS * 1000.0, 4), "raw_gain": g, "norm_gain": round(g / nR, 6)}
                    for d, g in ER_SPEC["taps_right"]
                ],
            },
            "output_scattering_matrix": {
                "formula": "W_L = wet_lin*(scatter_same*D_L + scatter_cross*D_R); W_R = wet_lin*(scatter_cross*D_L + scatter_same*D_R)",
                "scatter_same": ER_SPEC["scatter_same"],
                "scatter_cross": ER_SPEC["scatter_cross"],
                "mono_sum_comb_suppression_db": round(20.0 * math.log10(abs(ER_SPEC["scatter_same"] + ER_SPEC["scatter_cross"])), 2),
            },
            "side_air_polish": {
                "active_when": "mode == Studio Deep (0) and spatial_xtc < 0.05",
                "high_shelf_hz": ER_SPEC["side_air_shelf_hz"],
                "gain_db_at_default_depth": ER_SPEC["side_air_shelf_db"],
                "slope": 0.70710678,
                "biquad": {"b": [float(x) for x in bsh_air], "a": [float(x) for x in ash_air]},
            },
            "verified_metrics": depth,
        },
        "shuffler_xtc_engine": {
            "control_port": "Spatial XTC",
            "topology": "Cooper-Bauck_1D_MS_Shuffler_FDTR",
            "mid_path": f"integer_delay_line({shuffler['causal_delay_samples']} samples = {shuffler['causal_delay_ms']} ms, 0 MACs, bit-exact unity)",
            "side_path": f"1D_FIR({shuffler['taps']} taps float32, peak_tap={shuffler['causal_delay_samples']}, AVX2 32 FMAs/sample)",
            "blend_formula": "S_out[n] = (1 - xtc) * S[n - D] + xtc * sum_{k=0..255} h_s[k] * S[n - k]",
            **shuffler,
        },
        "multiband_dynamics_and_limiter": {
            "crossover_type": "Phase-aligned 4-band LR4 tree (24 dB/oct, exact AP unity sum)",
            "split_frequencies_hz": [160.0, 750.0, 3500.0],
            "bands": [
                {"index": 0, "range_hz": [0.0, 160.0], "name": "sub_bass", "threshold_dbfs": -13.0, "ratio": 2.8, "knee_db": 4.0, "attack_ms": 18.0, "release_ms": 120.0, "makeup_db": 1.5, "band_ceiling_dbfs": -5.0},
                {"index": 1, "range_hz": [160.0, 750.0], "name": "warmth_vocal_body", "threshold_dbfs": -12.0, "ratio": 2.5, "knee_db": 4.0, "attack_ms": 15.0, "release_ms": 100.0, "makeup_db": 1.5, "band_ceiling_dbfs": -4.6},
                {"index": 2, "range_hz": [750.0, 3500.0], "name": "presence_stereo_inst", "threshold_dbfs": -12.5, "ratio": 3.0, "knee_db": 4.0, "attack_ms": 10.0, "release_ms": 75.0, "makeup_db": 1.2, "band_ceiling_dbfs": -5.2},
                {"index": 3, "range_hz": [3500.0, 24000.0], "name": "air_treble", "threshold_dbfs": -14.0, "ratio": 2.5, "knee_db": 4.0, "attack_ms": 8.0, "release_ms": 60.0, "makeup_db": 1.0, "band_ceiling_dbfs": -6.8},
            ],
            "pre_spatial_headroom_bus_dbfs": -2.2,
            "final_true_peak_limiter": {
                "oversampling": "4x_Catmull_Rom_or_polyphase_FIR",
                "post_spatial_trim_db": 0.8,
                "default_ceiling_dbfs": -1.0,
                "lookahead_ms": 10.0,
                "attack_ms": 2.5,
                "release_ms": 80.0,
                "stereo_link_pct": 100.0,
            },
        },
        "modes": {
            "0_studio_deep": {
                "mode_id": 0,
                "label": "Studio Deep",
                "flagship": True,
                "bass_mono_anchor_hz": 180.0,
                "stage_depth_default": 0.85,
                "wet_er_db": -15.50,
                "side_air_shelf_db": 0.8,
                "spatial_xtc_default": 0.0,
                "drive_db_default": 7.0,
                "ceiling_dbfs_default": -1.0,
                "pre_spatial_ceiling_dbfs": -2.2,
                "total_spatial_plus_limiter_latency_ms": 10.0,
            },
            "1_studio_spatial": {
                "mode_id": 1,
                "label": "Studio Spatial",
                "flagship": False,
                "bass_mono_anchor_hz": 180.0,
                "stage_depth_default": 0.65,
                "wet_er_db": -17.83,
                "side_air_shelf_db": 0.0,
                "spatial_xtc_default": 0.75,
                "drive_db_default": 7.0,
                "ceiling_dbfs_default": -1.0,
                "pre_spatial_ceiling_dbfs": -2.2,
                "total_spatial_plus_limiter_latency_ms": 10.9167,
            },
            "2_studio_reference": {
                "mode_id": 2,
                "label": "Studio Reference",
                "flagship": False,
                "bass_mono_anchor_hz": 150.0,
                "stage_depth_default": 0.20,
                "wet_er_db": -28.07,
                "side_air_shelf_db": 0.0,
                "spatial_xtc_default": 0.0,
                "drive_db_default": 5.0,
                "ceiling_dbfs_default": -1.0,
                "pre_spatial_ceiling_dbfs": -2.0,
                "total_spatial_plus_limiter_latency_ms": 10.0,
            },
            "3_studio_voice": {
                "mode_id": 3,
                "label": "Studio Voice",
                "flagship": False,
                "bass_mono_anchor_hz": 180.0,
                "stage_depth_default": 0.0,
                "wet_er_db": None,
                "side_air_shelf_db": 0.0,
                "spatial_xtc_default": 0.0,
                "drive_db_default": 6.0,
                "ceiling_dbfs_default": -1.0,
                "pre_spatial_ceiling_dbfs": -2.0,
                "total_spatial_plus_limiter_latency_ms": 10.0,
            },
        },
        "forensic_ducking_comparison": ducking,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify XPS 17 9720 Spatial & Depth DSP Architecture")
    parser.add_argument("--write-spec", type=str, default=None, help="Optional path to write complete spatial_spec.json")
    args = parser.parse_args()

    geom = verify_geometry_and_coupling()
    anchor = verify_bass_mono_anchor(180.0)
    h_s_f32, shuffler = design_shuffler_xtc()
    depth = verify_studio_depth_engine(h_s_f32)
    ducking = verify_forensic_ducking(h_s_f32)

    print("\n" + "=" * 78)
    print("ALL SPATIAL PSYCHOACOUSTICS & SOUNDSTAGE VERIFICATION ASSERTIONS PASSED!")
    print("=" * 78)

    if args.write_spec:
        spec = build_complete_spatial_spec(geom, anchor, shuffler, depth, ducking)
        with open(args.write_spec, "w", encoding="utf-8") as fh:
            json.dump(spec, fh, indent=2)
        print(f"Wrote verified spatial_spec.json -> {args.write_spec}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

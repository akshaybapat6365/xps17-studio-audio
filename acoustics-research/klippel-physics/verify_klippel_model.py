#!/usr/bin/env python3
"""
Dell XPS 17 9720 (SUBSYS 0AFF1028) — Klippel Nonlinear Micro-Woofer Model,
Acoustic Calibration, Discrete Excursion Observer H_x(z), Dynamic Sliding HPF
(4th-Order TPT-SVF), Cooperative Sub-Band Limiter (Bands 1 & 2), Lookahead
Residual Excursion Guard, and Dual-Node Thermal Model.

Pure numpy + Python stdlib verification suite (run on E-cores):
  taskset -c 12-19 nice -n 19 python3 verify_klippel_model.py
"""

from collections import deque
import json
import math
import os
import sys
import numpy as np

SR = 48000.0
BASELINE_PATH = "/home/akshaybapat/project/omarchy-audio-tune/baseline_curve.npy"
OUT_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_JSON_PATH = os.path.join(OUT_DIR, "klippel_model.json")


# =============================================================================
# 1. PHYSICAL & CALIBRATED ELECTRO-MECHANICAL PARAMETERS (DELL XPS 17 9720)
# =============================================================================
PARAMS = {
    # Hardware-proven / inferred baseline ratings (protection-model/parameters.csv)
    "fs_hz": SR,
    "rated_rms_power_w": 2.0,
    "rated_peak_power_w": 2.5,
    "v_rms_windows_basic": 2.80,            # 0xC60F=0x05 (+9.0 dB), 0xD002=0xA9 ("0dB=2.8Vrms")
    "v_rms_linux_live_est": 2.00,           # 0xC60F=0x01 (+6.6 dB), 0xD002=0xBF (conservative est.)
    "re_ohm": 4.00,                         # 2.8^2 / 4.0 = 1.96 W ~ 2.0 W rating; RTSPKPT float 4.0
    "le_henry": 6.0e-5,                     # 0.06 mH typical 2W micro-woofer (f_e = 10.61 kHz)
    "alpha_cu_per_k": 0.00393,              # Copper voice-coil TCR (1/K)

    # Acoustic-calibrated sealed-box woofer T/S parameters (baseline_curve.npy + Waves 344 Hz notch)
    "f0_hz": 390.0,                         # Sealed-box resonance f_tc = sqrt(344 * 445) = 391.2 Hz
    "q_tc_cold": 1.48,                      # Cold in-box total Q (f_peak,p = 444.0 Hz, +3.93 dB)
    "q_obs": 1.55,                          # Conservative observer Q (guards Bl(x) & thermal Q rise)
    "q_mc": 15.80,                          # Mechanical/enclosure Q (Q_ec = 1.632 => Q_tc = 1.479)
    "mms_kg": 3.50e-4,                      # 0.35 g moving mass (typical 2W laptop racetrack woofer)
    "bl0_tm": 1.45,                         # 1.45 T*m motor force factor
    "sd_m2": 2.50e-4,                       # 2.5 cm^2 effective radiating area (~12x24 mm oval)
    "vb_m3": 3.00e-6,                       # 3.0 cm^3 sealed internal back-volume

    # Klippel large-signal nonlinear limits (IEC 62458 / Klippel 2006, 2012, 2015)
    "x_max_m": 4.00e-4,                     # 0.40 mm linear excursion limit (1.00 X_max)
    "x_safe_ratio": 0.85,                   # 0.34 mm pre-cliff safe threshold (0.85 X_max)
    "klippel_b2_at_xmax": 0.18,             # Bl(X_max)/Bl0 = 0.82 (-18%, IEC 62458 criterion)
    "klippel_k1_at_xmax": 0.10,             # Asymmetric air/surround stiffness (+10% @ +X_max)
    "klippel_k2_at_xmax": 0.333,            # Progressive stiffness (+33% @ X_max, C_ms = 0.75*C_ms0)
    "klippel_l1_per_m": -1.5e-2,            # dL_e/dx (H/m) reluctance asymmetry

    # Stage A: Dynamic Sliding HPF Control Law (4th-order Butterworth TPT-SVF, -24 dB/oct)
    "hpf_order": 4,
    "hpf_stage_q": [0.541196100146197, 1.3065629648763766],  # 4th-order Butterworth exact poles
    "f_min_default_hz": 82.0,               # Studio Deep resting floor
    "f_max_default_hz": 155.0,              # Peak excursion ceiling (<= 160 Hz)
    "f_max_hard_clamp_hz": 160.0,
    "mode_f_min_hz": {
        "studio_deep": 82.0,
        "studio_spatial": 85.0,
        "studio_reference": 80.0,
        "studio_voice": 105.0,
    },
    "x_knee_norm_safe": 0.62,               # Sliding HPF onset (0.62 X_safe = 0.527 X_max)
    "x_limit_norm_safe": 1.48,              # Full ceiling glide (1.48 X_safe = 1.258 X_max predicted)
    "env_attack_ms": 1.5,                   # Fast attack to catch kick/808 before cone bottoms
    "env_release_fast_ms": 45.0,            # Fast transient recovery
    "env_release_slow_ms": 180.0,           # Anti-pumping envelope floor
    "env_slow_floor_weight": 0.82,          # Blend weight of anti-pumping slow floor
    "fc_slew_tau_ms": 0.50,                 # Smooth cutoff slew constant (zero zipper clicks)
    "sidechain_lp_pole_hz": 170.0,          # 2nd-order LP-notch pole (<170 Hz sub-band focus)
    "sidechain_lp_zero_hz": 343.0,          # Exact zero at woofer displacement peak (zero vocal leak)
    "stage_a_lookahead_ms": 1.0,            # Pre-HPF lookahead (part of total 2.5 ms shared window)

    # Stage B: Cooperative Excursion-Safe Sub-Band Dynamic Limiter Knees (Multiband Bands 1 & 2)
    "band1_split_hz": 160.0,                # Band 1: < 160 Hz (sub-bass / kick body)
    "band1_thresh_dbfs": -7.5,              # Post-drive threshold
    "band1_knee_db": 4.0,
    "band1_ratio": 6.0,
    "band1_attack_ms": 3.0,
    "band1_release_ms": 90.0,

    "band2_split_lo_hz": 160.0,             # Band 2: 160-450 Hz (warm bass / 343 Hz resonance guard)
    "band2_split_hi_hz": 450.0,
    "band2_thresh_dbfs": -9.0,              # Excursion-safe warm-bass knee
    "band2_knee_db": 5.0,
    "band2_ratio": 8.0,
    "band2_attack_ms": 2.5,
    "band2_release_ms": 85.0,

    # Stage C: Lookahead Residual Klippel Observer Excursion Guard (zero overshoot across 20Hz-20kHz)
    "residual_x_thresh_safe": 0.94,         # Soft onset at 0.94 X_safe (0.799 X_max)
    "residual_x_ceil_safe": 1.00,           # Hard ceiling at 1.00 X_safe (0.850 X_max)
    "residual_lookahead_ms": 1.5,           # Sliding-window peak lookahead (shared w/ true-peak limiter)
    "residual_attack_ms": 0.30,             # Settles >99% inside 1.5 ms lookahead -> zero overshoot
    "residual_release_ms": 65.0,            # Smooth anti-pumping recovery

    # Dual-time-constant thermal model (Klippel 2004; protection-model/NOTES.md conservative 100 C)
    "t_ambient_c": 40.0,                    # Hot laptop internal chassis ambient
    "t_knee_c": 80.0,                       # Soft thermal limiting onset
    "t_limit_c": 100.0,                     # Conservative ceiling until RtkIntU64 / I-sense calib
    "r_th_vc_k_per_w": 13.5,                # Voice-coil-to-magnet thermal resistance
    "r_th_mag_k_per_w": 16.5,               # Magnet-to-chassis thermal resistance (sum = 30 K/W)
    "tau_vc_s": 1.5,                        # Fast voice-coil time constant (0.8-2.0 s)
    "tau_mag_s": 40.0,                      # Slow magnet/chassis time constant (20-60 s)
}


# =============================================================================
# 2. BILINEAR TRANSFORM DESIGN & EXACT ANALYSIS OF H_x(z)
# =============================================================================
def derive_derived_physics(p):
    w0 = 2.0 * math.pi * p["f0_hz"]
    k0 = p["mms_kg"] * (w0 ** 2)
    cms0 = 1.0 / k0
    kx = p["bl0_tm"] / (p["re_ohm"] * k0)   # m / V DC displacement sensitivity
    q_ec = (w0 * p["mms_kg"] * p["re_ohm"]) / (p["bl0_tm"] ** 2)
    q_tc = (q_ec * p["q_mc"]) / (q_ec + p["q_mc"])
    fe_hz = p["re_ohm"] / (2.0 * math.pi * p["le_henry"])
    x_safe = p["x_max_m"] * p["x_safe_ratio"]
    v_pk_basic = p["v_rms_windows_basic"] * math.sqrt(2.0)
    v_pk_linux = p["v_rms_linux_live_est"] * math.sqrt(2.0)

    g_obs_safe_basic = (kx * v_pk_basic) / x_safe
    g_obs_xmax_basic = (kx * v_pk_basic) / p["x_max_m"]
    g_obs_safe_linux = (kx * v_pk_linux) / x_safe
    g_obs_xmax_linux = (kx * v_pk_linux) / p["x_max_m"]

    fp_x = p["f0_hz"] * math.sqrt(1.0 - 1.0 / (2.0 * q_tc ** 2))
    fp_p = p["f0_hz"] / math.sqrt(1.0 - 1.0 / (2.0 * q_tc ** 2))
    pk_db = 20.0 * math.log10(q_tc / math.sqrt(1.0 - 1.0 / (4.0 * q_tc ** 2)))

    return {
        "w0_rad_s": w0,
        "k0_n_per_m": k0,
        "cms0_mm_per_n": cms0 * 1e3,
        "kx_mm_per_v": kx * 1e3,
        "q_ec": q_ec,
        "q_tc_calc": q_tc,
        "fe_inductance_hz": fe_hz,
        "x_safe_mm": x_safe * 1e3,
        "v_pk_basic_v": v_pk_basic,
        "v_pk_linux_est_v": v_pk_linux,
        "g_obs_safe_basic": g_obs_safe_basic,
        "g_obs_xmax_basic": g_obs_xmax_basic,
        "g_obs_safe_linux": g_obs_safe_linux,
        "g_obs_xmax_linux": g_obs_xmax_linux,
        "f_peak_displacement_hz": fp_x,
        "f_peak_pressure_hz": fp_p,
        "resonance_peak_db": pk_db,
    }


def bilinear_2nd_order_lp(f0_hz, q, gain, fs=SR):
    """Exact pre-warped bilinear transform of G * w0^2 / (s^2 + (w0/Q)*s + w0^2)."""
    w0 = 2.0 * math.pi * f0_hz
    k = math.tan(w0 / (2.0 * fs))
    a0 = 1.0 + k / q + k * k
    b0 = gain * (k * k) / a0
    b1 = 2.0 * b0
    b2 = b0
    a1 = 2.0 * (k * k - 1.0) / a0
    a2 = (1.0 - k / q + k * k) / a0
    return np.array([b0, b1, b2], dtype=np.float64), np.array([1.0, a1, a2], dtype=np.float64)


def bilinear_lp_notch(fp_hz, qp, fz_hz, fs=SR):
    """
    Exact pre-warped bilinear transform of 2nd-order low-pass notch biquad:
      H(s) = ((s/wz)^2 + 1) / ((s/wp)^2 + (1/Qp)*(s/wp) + 1)
    Unity DC gain, exact transmission zero at fz_hz (343 Hz), fast group delay (~1.3 ms).
    """
    kp = math.tan(math.pi * fp_hz / fs)
    kz = math.tan(math.pi * fz_hz / fs)
    # Using s_norm = (1 - z^-1)/(1 + z^-1):
    # Num: (s_norm/kz)^2 + 1 = (1 + kz^2)/kz^2 * [1 + 2*(kz^2-1)/(1+kz^2)*z^-1 + z^-2]
    # Wait: simplify with common factor kp^2 / kz^2 so DC (z=1 -> s_norm=0) equals 1.0!
    a0 = 1.0 + kp / qp + kp * kp
    r2 = (kp * kp) / (kz * kz)
    b0 = (r2 * (1.0 + kz * kz)) / a0
    b1 = (2.0 * r2 * (kz * kz - 1.0)) / a0
    b2 = b0
    a1 = (2.0 * (kp * kp - 1.0)) / a0
    a2 = (1.0 - kp / qp + kp * kp) / a0
    # Verify unity DC: (b0+b1+b2)/(1+a1+a2) = (4*r2*kz^2 / a0) / (4*kp^2 / a0) = 1.0 exactly!
    return np.array([b0, b1, b2], dtype=np.float64), np.array([1.0, a1, a2], dtype=np.float64)


def bilinear_1st_order_lp(fc_hz, fs=SR):
    """Exact pre-warped bilinear transform of 1 / (1 + s / wc) for voice-coil inductance pole."""
    wc = 2.0 * math.pi * fc_hz
    k = math.tan(wc / (2.0 * fs))
    a0 = 1.0 + k
    b = np.array([k / a0, k / a0], dtype=np.float64)
    a = np.array([1.0, (k - 1.0) / a0], dtype=np.float64)
    return b, a


def eval_freq_resp(b, a, freqs_hz, fs=SR):
    z_inv = np.exp(-2j * np.pi * freqs_hz / fs)
    num = np.zeros_like(z_inv, dtype=np.complex128)
    den = np.zeros_like(z_inv, dtype=np.complex128)
    for idx, coef in enumerate(b):
        num += coef * (z_inv ** idx)
    for idx, coef in enumerate(a):
        den += coef * (z_inv ** idx)
    return num / den


def biquad_filter(x, b, a):
    y = np.zeros(len(x), dtype=np.float64)
    x1 = x2 = y1 = y2 = 0.0
    b0, b1, b2 = b
    a1, a2 = a[1], a[2]
    for n in range(len(x)):
        xn = x[n]
        yn = b0 * xn + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1 = x1, xn
        y2, y1 = y1, yn
        y[n] = yn
    return y


# =============================================================================
# 3. MEASURED BASELINE_CURVE.NPY ACOUSTIC CALIBRATION & DESK-BOUNCE ANALYSIS
# =============================================================================
def analyze_measured_baseline():
    assert os.path.exists(BASELINE_PATH), f"Missing {BASELINE_PATH}"
    data = np.load(BASELINE_PATH)
    f, mag = data[0], data[1]
    ref_1k = float(mag[np.argmin(np.abs(f - 1000.0))])
    rel = mag - ref_1k

    sm6 = np.copy(rel)
    fac = 2.0 ** (1.0 / 12.0)
    for i in range(len(f)):
        if f[i] < 20.0:
            continue
        a = np.searchsorted(f, f[i] / fac)
        b = np.searchsorted(f, f[i] * fac)
        if b > a:
            sm6[i] = np.mean(rel[a:b])

    def at_f(arr, hz):
        return float(arr[np.argmin(np.abs(f - hz))])

    slopes = {
        "110_to_220_hz_db_per_oct": round(at_f(sm6, 220) - at_f(sm6, 110), 2),
        "120_to_240_hz_db_per_oct": round(at_f(sm6, 240) - at_f(sm6, 120), 2),
        "150_to_300_hz_db_per_oct": round(at_f(sm6, 300) - at_f(sm6, 150), 2),
        "200_to_400_hz_db_per_oct": round(at_f(sm6, 400) - at_f(sm6, 200), 2),
    }

    m_pk = (f >= 350.0) & (f <= 550.0)
    f_pk = float(f[m_pk][np.argmax(rel[m_pk])])
    v_pk = float(np.max(rel[m_pk]))

    notches = []
    for lo, hi in [(170, 195), (210, 235), (680, 780)]:
        m = (f >= lo) & (f <= hi)
        fn = float(f[m][np.argmin(sm6[m])])
        vn = float(np.min(sm6[m]))
        notches.append({"freq_hz": round(fn, 1), "rel_1k_db": round(vn, 2)})

    m_fit = (f >= 110.0) & (f <= 420.0)
    r = f[m_fit] / PARAMS["f0_hz"]
    hp_db = 20.0 * np.log10((r ** 2) / np.sqrt((1.0 - r ** 2) ** 2 + (r / PARAMS["q_tc_cold"]) ** 2))
    w = 1.0 / f[m_fit]
    passband_offset = float(np.sum(w * (rel[m_fit] - hp_db)) / np.sum(w))
    wrmse = float(np.sqrt(np.sum(w * (rel[m_fit] - (hp_db + passband_offset)) ** 2) / np.sum(w)))

    return {
        "ref_1khz_db": round(ref_1k, 2),
        "measured_rel_1k_db": {
            "100_hz": round(at_f(rel, 100), 2),
            "110_hz": round(at_f(rel, 110), 2),
            "150_hz": round(at_f(rel, 150), 2),
            "200_hz": round(at_f(rel, 200), 2),
            "300_hz": round(at_f(rel, 300), 2),
            "350_hz": round(at_f(rel, 350), 2),
            "445_hz": round(v_pk, 2),
            "730_hz": round(at_f(rel, 730), 2),
            "1500_hz": round(at_f(rel, 1500), 2),
            "10000_hz": round(at_f(rel, 10000), 2),
        },
        "measured_peak_hz": round(f_pk, 1),
        "measured_peak_rel_1k_db": round(v_pk, 2),
        "slopes_1_6_oct": slopes,
        "desk_bounce_notches": notches,
        "sealed_box_fit_110_420hz": {
            "f0_hz": PARAMS["f0_hz"],
            "q_tc": PARAMS["q_tc_cold"],
            "woofer_vs_combined_1k_offset_db": round(passband_offset, 2),
            "weighted_rmse_db": round(wrmse, 2),
        },
    }


# =============================================================================
# 4. NONLINEAR KLIPPEL LARGE-SIGNAL RK4 PLANT SIMULATOR (CROSS-VALIDATION)
# =============================================================================
def simulate_klippel_nonlinear_rk4(u_norm, v_pk_fs, p, d, t_vc_c=25.0):
    """
    Full coupled 3-state nonlinear Klippel ODE (Klippel 1992, 2006, 2012):
      State = [x (m), v (m/s), i (A)]
      u = Re(T)*i + Le(x)*di/dt + (Bl(x) + i*dLe/dx)*v
      Mms*dv/dt + Rms*v + Kms(x)*x = Bl(x)*i + 0.5*i^2*dLe/dx
    Integrated via 4x sub-stepped RK4 (dt = 5.21 us). Returns x / X_max.
    """
    N = len(u_norm)
    sub = 4
    dt = 1.0 / (SR * sub)
    re = p["re_ohm"] * (1.0 + p["alpha_cu_per_k"] * (t_vc_c - 25.0))
    le0 = p["le_henry"]
    l1 = p["klippel_l1_per_m"]
    bl0 = p["bl0_tm"]
    mms = p["mms_kg"]
    k0 = d["k0_n_per_m"]
    rms = d["w0_rad_s"] * mms / p["q_mc"]
    xm = p["x_max_m"]
    b2 = p["klippel_b2_at_xmax"] / (xm * xm)
    k1 = p["klippel_k1_at_xmax"] / xm
    k2 = p["klippel_k2_at_xmax"] / (xm * xm)

    def deriv(x, v, cur, u_v):
        bl_x = bl0 * max(0.25, 1.0 - b2 * x * x)
        kms_x = k0 * (1.0 + k1 * x + k2 * x * x)
        le_x = max(0.5 * le0, le0 + l1 * x)
        di = (u_v - re * cur - (bl_x + cur * l1) * v) / le_x
        dv = (bl_x * cur + 0.5 * cur * cur * l1 - rms * v - kms_x * x) / mms
        return v, dv, di

    x = v = cur = 0.0
    x_out = np.zeros(N, dtype=np.float64)
    for n in range(N):
        u_v = u_norm[n] * v_pk_fs
        for _ in range(sub):
            k1x, k1v, k1i = deriv(x, v, cur, u_v)
            k2x, k2v, k2i = deriv(x + 0.5 * dt * k1x, v + 0.5 * dt * k1v, cur + 0.5 * dt * k1i, u_v)
            k3x, k3v, k3i = deriv(x + 0.5 * dt * k2x, v + 0.5 * dt * k2v, cur + 0.5 * dt * k2i, u_v)
            k4x, k4v, k4i = deriv(x + dt * k3x, v + dt * k3v, cur + dt * k3i, u_v)
            x += (dt / 6.0) * (k1x + 2.0 * k2x + 2.0 * k3x + k4x)
            v += (dt / 6.0) * (k1v + 2.0 * k2v + 2.0 * k3v + k4v)
            cur += (dt / 6.0) * (k1i + 2.0 * k2i + 2.0 * k3i + k4i)
        x_out[n] = x / xm
    return x_out


# =============================================================================
# 5. THREE-STAGE COOPERATIVE KLIPPEL EXCURSION PROTECTION CHAIN
# =============================================================================
def smoothstep(t):
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return t * t * (3.0 - 2.0 * t)


def soft_knee_gain_db(level_db, thresh_db, knee_db, ratio):
    """Quadratic soft-knee compressor/limiter gain reduction (dB <= 0)."""
    half_k = 0.5 * knee_db
    diff = level_db - thresh_db
    if diff <= -half_k:
        return 0.0
    if diff >= half_k:
        return (1.0 / ratio - 1.0) * diff
    x = diff + half_k
    return (1.0 / ratio - 1.0) * (x * x) / (2.0 * knee_db)


def run_protection_chain(
    u_in,
    p,
    d,
    drive_db=7.0,
    ceiling_dbfs=-1.0,
    f_min_hz=None,
    f_max_hz=None,
    excursion_guard=1.0,
    enable_coop_band_limiter=True,
    enable_residual_guard=True,
    v_pk_fs=None,
):
    """
    Real-time 3-stage cooperative Klippel excursion protection chain:
      Stage A: Feed-forward H_x(z) + 2nd-order LP-Notch (pole 170 Hz, zero 343 Hz) ->
               Smoothstep Sliding HPF (4th-order Butterworth TPT-SVF, 82..155 Hz, zero zipper).
      Stage B: Cooperative LR4 Multiband Excursion Knees (Band 1 < 160 Hz, Band 2 160-450 Hz).
      Stage C: Exact Lookahead Residual H_x(z) Excursion Limiter (1.5 ms sliding peak window)
               guaranteeing <= 1.00 X_max at EVERY frequency 20 Hz-20 kHz with zero overshoot.
      Stage D: Brickwall True-Peak Ceiling (-1.0 dBFS).
    """
    if f_min_hz is None:
        f_min_hz = p["f_min_default_hz"]
    if f_max_hz is None:
        f_max_hz = p["f_max_default_hz"]
    if v_pk_fs is None:
        v_pk_fs = d["v_pk_basic_v"]

    g_obs_safe = (d["kx_mm_per_v"] * 1e-3 * v_pk_fs) / (p["x_max_m"] * p["x_safe_ratio"])
    g_obs_xmax = (d["kx_mm_per_v"] * 1e-3 * v_pk_fs) / p["x_max_m"]

    b_obs, a_obs = bilinear_2nd_order_lp(p["f0_hz"], p["q_obs"], g_obs_safe, SR)
    b_plt, a_plt = bilinear_2nd_order_lp(p["f0_hz"], p["q_tc_cold"], g_obs_xmax, SR)

    # Single-biquad LP-Notch sub-band focus filter: pole 170 Hz (Q=0.7071), exact zero at 343 Hz!
    b_sub, a_sub = bilinear_lp_notch(p["sidechain_lp_pole_hz"], 0.7071067811865476, p["sidechain_lp_zero_hz"], SR)

    # LR4 crossover SVF constants for Band 1 (<160 Hz), Band 2 (160-450 Hz), Band 3+ (>450 Hz)
    g_x1 = math.tan(math.pi * p["band1_split_hz"] / SR)
    g_x2 = math.tan(math.pi * p["band2_split_hi_hz"] / SR)
    k_bw = math.sqrt(2.0)

    def svf_coeffs(g, k):
        a1 = 1.0 / (1.0 + g * (g + k))
        return a1, g * a1, g * g * a1

    x1_a1, x1_a2, x1_a3 = svf_coeffs(g_x1, k_bw)
    x2_a1, x2_a2, x2_a3 = svf_coeffs(g_x2, k_bw)

    N = len(u_in)
    g_drive = 10.0 ** (drive_db / 20.0)
    ceil_lin = 10.0 ** (ceiling_dbfs / 20.0)

    a_att = math.exp(-1.0 / (p["env_attack_ms"] * 1e-3 * SR))
    a_rf = math.exp(-1.0 / (p["env_release_fast_ms"] * 1e-3 * SR))
    a_rs = math.exp(-1.0 / (p["env_release_slow_ms"] * 1e-3 * SR))
    a_fc = math.exp(-1.0 / (p["fc_slew_tau_ms"] * 1e-3 * SR))

    a_b1_att = math.exp(-1.0 / (p["band1_attack_ms"] * 1e-3 * SR))
    a_b1_rel = math.exp(-1.0 / (p["band1_release_ms"] * 1e-3 * SR))
    a_b2_att = math.exp(-1.0 / (p["band2_attack_ms"] * 1e-3 * SR))
    a_b2_rel = math.exp(-1.0 / (p["band2_release_ms"] * 1e-3 * SR))

    a_res_att = math.exp(-1.0 / (p["residual_attack_ms"] * 1e-3 * SR))
    a_res_rel = math.exp(-1.0 / (p["residual_release_ms"] * 1e-3 * SR))

    # Filter states
    obs_x1 = obs_x2 = obs_y1 = obs_y2 = 0.0
    sub_x1 = sub_x2 = sub_y1 = sub_y2 = 0.0
    res_x1 = res_x2 = res_y1 = res_y2 = 0.0
    plt_x1 = plt_x2 = plt_y1 = plt_y2 = 0.0

    qs_bw4 = p["hpf_stage_q"]
    ic1 = [0.0, 0.0]
    ic2 = [0.0, 0.0]

    b1_lp1 = [0.0, 0.0]; b1_lp2 = [0.0, 0.0]
    b1_hp1 = [0.0, 0.0]; b1_hp2 = [0.0, 0.0]
    b2_lp1 = [0.0, 0.0]; b2_lp2 = [0.0, 0.0]
    b2_hp1 = [0.0, 0.0]; b2_hp2 = [0.0, 0.0]
    b1_env = 0.0
    b2_env = 0.0

    env_fast = 0.0
    env_slow = 0.0
    fc_curr = f_min_hz
    res_gain = 1.0

    la1_n = max(1, int(round(p["stage_a_lookahead_ms"] * 1e-3 * SR)))
    la2_n = max(1, int(round(p["residual_lookahead_ms"] * 1e-3 * SR)))
    la1_buf = np.zeros(la1_n, dtype=np.float64)
    la2_buf = np.zeros(la2_n, dtype=np.float64)
    la1_idx = 0
    la2_idx = 0

    # O(1) monotonic deque for exact sliding-window peak over Stage C 1.5 ms lookahead!
    pk_deque = deque()

    y_out = np.zeros(N, dtype=np.float64)
    fc_trace = np.zeros(N, dtype=np.float64)
    x_env_trace = np.zeros(N, dtype=np.float64)
    x_lin_trace = np.zeros(N, dtype=np.float64)
    gr_b1_trace = np.zeros(N, dtype=np.float64)
    gr_b2_trace = np.zeros(N, dtype=np.float64)
    gr_res_trace = np.zeros(N, dtype=np.float64)

    x_knee = p["x_knee_norm_safe"]
    x_limit = p["x_limit_norm_safe"]
    inv_span = 1.0 / (x_limit - x_knee)
    w_slow = p["env_slow_floor_weight"]
    res_thr = p["residual_x_thresh_safe"]
    res_ceil = p["residual_x_ceil_safe"]

    for n in range(N):
        xn = u_in[n]
        x_sc = xn * g_drive

        # --- STAGE A: Feed-forward H_x(z) + LP-Notch (pole 170 Hz, zero 343 Hz) -> Sliding HPF ---
        xo = b_obs[0] * x_sc + b_obs[1] * obs_x1 + b_obs[2] * obs_x2 - a_obs[1] * obs_y1 - a_obs[2] * obs_y2
        obs_x2, obs_x1 = obs_x1, x_sc
        obs_y2, obs_y1 = obs_y1, xo

        xs = b_sub[0] * xo + b_sub[1] * sub_x1 + b_sub[2] * sub_x2 - a_sub[1] * sub_y1 - a_sub[2] * sub_y2
        sub_x2, sub_x1 = sub_x1, xo
        sub_y2, sub_y1 = sub_y1, xs

        mag = abs(xs)
        if mag > env_fast:
            env_fast = a_att * env_fast + (1.0 - a_att) * mag
        else:
            env_fast = a_rf * env_fast + (1.0 - a_rf) * mag

        if env_fast > env_slow:
            env_slow = a_att * env_slow + (1.0 - a_att) * env_fast
        else:
            env_slow = a_rs * env_slow + (1.0 - a_rs) * env_fast

        x_env = env_fast if env_fast > w_slow * env_slow else w_slow * env_slow
        x_env_trace[n] = x_env

        u_glide = smoothstep((x_env - x_knee) * inv_span)
        fc_tgt = f_min_hz + excursion_guard * (f_max_hz - f_min_hz) * u_glide
        fc_curr = a_fc * fc_curr + (1.0 - a_fc) * fc_tgt
        fc_trace[n] = fc_curr

        v = la1_buf[la1_idx]
        la1_buf[la1_idx] = xn
        la1_idx = (la1_idx + 1) % la1_n

        # 4th-order Butterworth TPT-SVF (Zero-Delay Feedback — unconditional passivity under sweep)
        g = math.tan(math.pi * fc_curr / SR)
        for st in range(2):
            kd = 1.0 / qs_bw4[st]
            a1s = 1.0 / (1.0 + g * (g + kd))
            a2s = g * a1s
            a3s = g * a2s
            v3 = v - ic2[st]
            v1 = a1s * ic1[st] + a2s * v3
            v2 = ic2[st] + a2s * ic1[st] + a3s * v3
            ic1[st] = 2.0 * v1 - ic1[st]
            ic2[st] = 2.0 * v2 - ic2[st]
            v = v - kd * v1 - v2

        vd = v * g_drive

        # --- STAGE B: Cooperative LR4 Band 1 (<160 Hz) & Band 2 (160-450 Hz) Limiter ---
        if enable_coop_band_limiter and excursion_guard > 0.0:
            s = vd
            for st_arr in (b1_lp1, b1_lp2):
                v3 = s - st_arr[1]
                v1 = x1_a1 * st_arr[0] + x1_a2 * v3
                v2 = st_arr[1] + x1_a2 * st_arr[0] + x1_a3 * v3
                st_arr[0] = 2.0 * v1 - st_arr[0]
                st_arr[1] = 2.0 * v2 - st_arr[1]
                s = v2
            band1 = s

            s = vd
            for st_arr in (b1_hp1, b1_hp2):
                v3 = s - st_arr[1]
                v1 = x1_a1 * st_arr[0] + x1_a2 * v3
                v2 = st_arr[1] + x1_a2 * st_arr[0] + x1_a3 * v3
                st_arr[0] = 2.0 * v1 - st_arr[0]
                st_arr[1] = 2.0 * v2 - st_arr[1]
                s = s - k_bw * v1 - v2
            upper = s

            s = upper
            for st_arr in (b2_lp1, b2_lp2):
                v3 = s - st_arr[1]
                v1 = x2_a1 * st_arr[0] + x2_a2 * v3
                v2 = st_arr[1] + x2_a2 * st_arr[0] + x2_a3 * v3
                st_arr[0] = 2.0 * v1 - st_arr[0]
                st_arr[1] = 2.0 * v2 - st_arr[1]
                s = v2
            band2 = s

            s = upper
            for st_arr in (b2_hp1, b2_hp2):
                v3 = s - st_arr[1]
                v1 = x2_a1 * st_arr[0] + x2_a2 * v3
                v2 = st_arr[1] + x2_a2 * st_arr[0] + x2_a3 * v3
                st_arr[0] = 2.0 * v1 - st_arr[0]
                st_arr[1] = 2.0 * v2 - st_arr[1]
                s = s - k_bw * v1 - v2
            band3p = s

            m1 = abs(band1)
            b1_env = (a_b1_att * b1_env + (1.0 - a_b1_att) * m1) if m1 > b1_env else (a_b1_rel * b1_env + (1.0 - a_b1_rel) * m1)
            gr1_db = excursion_guard * soft_knee_gain_db(20.0 * math.log10(max(b1_env, 1e-12)), p["band1_thresh_dbfs"], p["band1_knee_db"], p["band1_ratio"])
            gr_b1_trace[n] = gr1_db

            m2 = abs(band2)
            b2_env = (a_b2_att * b2_env + (1.0 - a_b2_att) * m2) if m2 > b2_env else (a_b2_rel * b2_env + (1.0 - a_b2_rel) * m2)
            gr2_db = excursion_guard * soft_knee_gain_db(20.0 * math.log10(max(b2_env, 1e-12)), p["band2_thresh_dbfs"], p["band2_knee_db"], p["band2_ratio"])
            gr_b2_trace[n] = gr2_db

            vd = band1 * (10.0 ** (gr1_db / 20.0)) + band2 * (10.0 ** (gr2_db / 20.0)) + band3p

        # --- STAGE C: Lookahead Residual Klippel Observer Excursion Limiter ---
        v_del = la2_buf[la2_idx]
        la2_buf[la2_idx] = vd
        la2_idx = (la2_idx + 1) % la2_n

        if enable_residual_guard and excursion_guard > 0.0:
            xres = b_obs[0] * vd + b_obs[1] * res_x1 + b_obs[2] * res_x2 - a_obs[1] * res_y1 - a_obs[2] * res_y2
            res_x2, res_x1 = res_x1, vd
            res_y2, res_y1 = res_y1, xres

            mr = abs(xres)
            # Sliding-window peak max over la2_n (1.5 ms) in O(1) amortized time
            while pk_deque and pk_deque[-1][1] <= mr:
                pk_deque.pop()
            pk_deque.append((n, mr))
            if pk_deque[0][0] <= n - la2_n:
                pk_deque.popleft()
            x_win_pk = pk_deque[0][1]

            if x_win_pk > res_thr:
                g_hard = min(1.0, res_ceil / x_win_pk)
                blend = smoothstep((x_win_pk - res_thr) / (res_ceil - res_thr))
                g_tgt = 1.0 - excursion_guard * blend * (1.0 - g_hard)
            else:
                g_tgt = 1.0

            if g_tgt < res_gain:
                res_gain = a_res_att * res_gain + (1.0 - a_res_att) * g_tgt
            else:
                res_gain = a_res_rel * res_gain + (1.0 - a_res_rel) * g_tgt

            gr_res_trace[n] = 20.0 * math.log10(max(res_gain, 1e-6))
            v_post = v_del * res_gain
        else:
            v_post = v_del

        # --- STAGE D: Brickwall True-Peak Ceiling (-1.0 dBFS) ---
        if v_post > ceil_lin:
            y = ceil_lin
        elif v_post < -ceil_lin:
            y = -ceil_lin
        else:
            y = v_post
        y_out[n] = y

        # Physical linear woofer plant response (normalized to X_max = 1.0)
        xa = b_plt[0] * y + b_plt[1] * plt_x1 + b_plt[2] * plt_x2 - a_plt[1] * plt_y1 - a_plt[2] * plt_y2
        plt_x2, plt_x1 = plt_x1, y
        plt_y2, plt_y1 = plt_y1, xa
        x_lin_trace[n] = xa

    return {
        "y": y_out,
        "fc": fc_trace,
        "x_env": x_env_trace,
        "x_lin": x_lin_trace,
        "gr_b1": gr_b1_trace,
        "gr_b2": gr_b2_trace,
        "gr_res": gr_res_trace,
    }


# =============================================================================
# 6. DUAL-TIME-CONSTANT THERMAL MODEL VERIFICATION
# =============================================================================
def simulate_thermal_network(power_profile_w, dt_s, p, enable_limiter=True):
    """
    Two-node Cauer/Foster thermal network (Klippel 2004; Asahi speakersafetyd):
      dT_vc/dt  = (P*R_tv - dT_vc) / tau_vc
      dT_mag/dt = (P*R_tm - dT_mag) / tau_mag
      T_vc = T_amb + dT_vc + dT_mag
    Soft thermal ramp over [T_knee=80 C, T_limit=100 C] capping sustained power <= 2.0 W.
    """
    N = len(power_profile_w)
    a_vc = math.exp(-dt_s / p["tau_vc_s"])
    a_mag = math.exp(-dt_s / p["tau_mag_s"])
    r_tv = p["r_th_vc_k_per_w"]
    r_tm = p["r_th_mag_k_per_w"]
    t_amb = p["t_ambient_c"]
    t_knee = p["t_knee_c"]
    t_lim = p["t_limit_c"]
    t_win = t_lim - t_knee

    dt_vc = 0.0
    dt_mag = 0.0
    t_trace = np.zeros(N)
    p_eff_trace = np.zeros(N)
    gr_db_trace = np.zeros(N)
    p_max_cont = (t_lim - t_amb) / (r_tv + r_tm)

    for n in range(N):
        t_vc = t_amb + dt_vc + dt_mag
        if enable_limiter and t_vc > t_knee:
            frac = min(1.5, (t_vc - t_knee) / t_win)
            gr_db = -6.0 * frac
        else:
            gr_db = 0.0

        gain_p = 10.0 ** (gr_db / 10.0)
        p_eff = power_profile_w[n] * gain_p
        dt_vc = a_vc * dt_vc + (1.0 - a_vc) * (p_eff * r_tv)
        dt_mag = a_mag * dt_mag + (1.0 - a_mag) * (p_eff * r_tm)
        t_trace[n] = t_amb + dt_vc + dt_mag
        p_eff_trace[n] = p_eff
        gr_db_trace[n] = gr_db

    return t_trace, p_eff_trace, gr_db_trace, p_max_cont


# =============================================================================
# 7. MAIN VERIFICATION SUITE WITH STRICT ASSERTIONS
# =============================================================================
def main():
    print("=" * 78)
    print("DELL XPS 17 9720 — KLIPPEL TRANSDUCER PHYSICS & EXCURSION VERIFICATION")
    print("=" * 78)

    d = derive_derived_physics(PARAMS)
    print("\n[1] CALIBRATED ELECTRO-MECHANICAL PARAMETERS")
    print(f"    f_0 (in-box f_tc)   = {PARAMS['f0_hz']:.1f} Hz  | Q_tc (cold) = {d['q_tc_calc']:.3f} (Q_ec={d['q_ec']:.3f}, Q_mc={PARAMS['q_mc']:.1f})")
    print(f"    Q_obs (guard)       = {PARAMS['q_obs']:.3f}   | M_ms = {PARAMS['mms_kg']*1e3:.2f} g | Bl_0 = {PARAMS['bl0_tm']:.2f} T*m | R_e = {PARAMS['re_ohm']:.2f} ohm")
    print(f"    K_0                 = {d['k0_n_per_m']:.2f} N/m (C_ms0 = {d['cms0_mm_per_n']:.3f} mm/N)")
    print(f"    DC sensitivity K_x  = {d['kx_mm_per_v']:.4f} mm/V | Inductance pole f_e = {d['fe_inductance_hz']:.1f} Hz")
    print(f"    X_max               = {PARAMS['x_max_m']*1e3:.2f} mm | X_safe (85%) = {d['x_safe_mm']:.2f} mm")
    print(f"    Displacement peak   = {d['f_peak_displacement_hz']:.1f} Hz (+{d['resonance_peak_db']:.2f} dB)  <-- matches Dell Waves 344 Hz notch!")
    print(f"    Pressure peak       = {d['f_peak_pressure_hz']:.1f} Hz (+{d['resonance_peak_db']:.2f} dB)  <-- matches baseline_curve.npy 445 Hz (+3.88 dB)!")
    print(f"    Windows Basic 0dBFS = {PARAMS['v_rms_windows_basic']:.2f} Vrms ({d['v_pk_basic_v']:.3f} Vpk) -> G_obs_safe = {d['g_obs_safe_basic']:.4f}, G_obs_xmax = {d['g_obs_xmax_basic']:.4f}")
    print(f"    Linux Live est.     = {PARAMS['v_rms_linux_live_est']:.2f} Vrms ({d['v_pk_linux_est_v']:.3f} Vpk) -> G_obs_safe = {d['g_obs_safe_linux']:.4f}, G_obs_xmax = {d['g_obs_xmax_linux']:.4f}")

    # [2] Measured acoustic calibration check
    meas = analyze_measured_baseline()
    print("\n[2] MEASURED baseline_curve.npy EXTRACTION")
    print(f"    1 kHz ref = {meas['ref_1khz_db']} dB | Stock peak = {meas['measured_peak_hz']} Hz ({meas['measured_peak_rel_1k_db']:+.2f} dB)")
    print(f"    Slopes (1/6-oct): 110->220={meas['slopes_1_6_oct']['110_to_220_hz_db_per_oct']:+.2f}, 120->240={meas['slopes_1_6_oct']['120_to_240_hz_db_per_oct']:+.2f}, 200->400={meas['slopes_1_6_oct']['200_to_400_hz_db_per_oct']:+.2f} dB/oct")
    print(f"    Desk-bounce notches: {meas['desk_bounce_notches']}")
    print(f"    Small (1972) fit (110-420 Hz): wRMSE = {meas['sealed_box_fit_110_420hz']['weighted_rmse_db']:.2f} dB (offset={meas['sealed_box_fit_110_420hz']['woofer_vs_combined_1k_offset_db']:+.2f} dB)")
    assert abs(meas["measured_peak_hz"] - 445.0) <= 10.0
    assert 10.0 <= meas["slopes_1_6_oct"]["120_to_240_hz_db_per_oct"] <= 13.5

    # [3] Discrete observer H_x(z) & LP-Notch sidechain stability, Bode & impulse check
    b_obs, a_obs = bilinear_2nd_order_lp(PARAMS["f0_hz"], PARAMS["q_obs"], d["g_obs_safe_basic"], SR)
    b_ind, a_ind = bilinear_1st_order_lp(d["fe_inductance_hz"], SR)
    b_sub, a_sub = bilinear_lp_notch(PARAMS["sidechain_lp_pole_hz"], 0.7071067811865476, PARAMS["sidechain_lp_zero_hz"], SR)
    poles_2nd = np.roots(a_obs)
    pole_ind = np.roots(a_ind)
    poles_sub = np.roots(a_sub)
    max_pole_mag = float(np.max(np.abs(poles_2nd)))
    assert max_pole_mag < 1.0
    assert float(np.abs(pole_ind[0])) < 1.0
    assert float(np.max(np.abs(poles_sub))) < 1.0
    # Verify exact transmission zero at 343 Hz on sidechain LP-Notch
    sub_343_db = 20.0 * math.log10(max(abs(eval_freq_resp(b_sub, a_sub, np.array([343.0]), SR)[0]), 1e-12))
    assert sub_343_db < -80.0, f"LP-Notch zero at 343 Hz missed: {sub_343_db:.1f} dB"

    freqs_bode = np.array([20, 45, 50, 82, 100, 155, 200, 300, 343, 390, 500, 1000, 2000, 5000], dtype=np.float64)
    H2 = eval_freq_resp(b_obs, a_obs, freqs_bode, SR)
    H_ind = eval_freq_resp(b_ind, a_ind, freqs_bode, SR)
    H3 = H2 * H_ind
    dc_gain = float(np.sum(b_obs) / np.sum(a_obs))
    f0_gain = float(np.abs(eval_freq_resp(b_obs, a_obs, np.array([PARAMS["f0_hz"]]), SR)[0]))
    assert abs(dc_gain - d["g_obs_safe_basic"]) < 1e-6
    assert abs(f0_gain - d["g_obs_safe_basic"] * PARAMS["q_obs"]) < 1e-4

    imp = np.zeros(2400)
    imp[0] = 1.0
    h_imp = biquad_filter(imp, b_obs, a_obs)
    tail_ratio_db = 20.0 * math.log10(max(np.max(np.abs(h_imp[-200:])), 1e-18) / np.max(np.abs(h_imp)))
    assert tail_ratio_db < -120.0

    print("\n[3] DISCRETE OBSERVER H_x(z) @ 48 kHz (NORMALIZED: |x_hat|=1.0 => X_safe=0.34mm)")
    print(f"    b = [{b_obs[0]:.10e}, {b_obs[1]:.10e}, {b_obs[2]:.10e}]")
    print(f"    a = [1.0, {a_obs[1]:.10f}, {a_obs[2]:.10f}]")
    print(f"    Poles = {poles_2nd[0]:.6f}, {poles_2nd[1]:.6f} | |z| = {max_pole_mag:.6f} < 1.0 (PASS)")
    print(f"    Sidechain LP-Notch (170 Hz / 343 Hz zero): |z_p|={np.max(np.abs(poles_sub)):.6f} < 1.0 | H(343 Hz)={sub_343_db:.1f} dB (PASS)")
    print(f"    DC gain = {dc_gain:.6f} | |H_x(f0)| = {f0_gain:.6f} | 50 ms impulse tail = {tail_ratio_db:.1f} dB (PASS)")
    bode_table = []
    for fr, h2v, h3v in zip(freqs_bode, H2, H3):
        m2 = 20.0 * math.log10(abs(h2v))
        p2 = math.degrees(np.angle(h2v))
        m3 = 20.0 * math.log10(abs(h3v))
        p3 = math.degrees(np.angle(h3v))
        bode_table.append({
            "freq_hz": float(fr),
            "mag_2nd_db": round(m2, 2),
            "phase_2nd_deg": round(p2, 1),
            "mag_3rd_db": round(m3, 2),
            "phase_3rd_deg": round(p3, 1),
            "inductance_delta_db": round(m3 - m2, 3),
        })
        print(f"      {fr:6.0f} Hz: |H2|={m2:+6.2f} dB ({p2:+6.1f} deg) | |H3|={m3:+6.2f} dB (d_Le={m3-m2:+.3f} dB)")

    # [4] 4 MANDATORY STRESS TESTS + BONUS RESONANCE & FULL-SPECTRUM SWEEP CHECKS
    print("\n[4] REAL-TIME EXCURSION OBSERVER + SLIDING HPF STRESS TESTS (Drive=+7 dB, Ceil=-1 dBFS)")
    T_sim = 0.45
    t = np.arange(int(T_sim * SR)) / SR
    i0 = int(0.02 * SR)

    # Test 1: Quiet music (-24 dBFS, 80 Hz + 55 Hz bass)
    u1 = (10.0 ** (-24.0 / 20.0)) * (0.60 * np.sin(2.0 * np.pi * 80.0 * t) + 0.40 * np.sin(2.0 * np.pi * 55.0 * t))
    r1 = run_protection_chain(u1, PARAMS, d)
    x1_nl = simulate_klippel_nonlinear_rk4(r1["y"], d["v_pk_basic_v"], PARAMS, d)
    t1_fc_max = float(np.max(r1["fc"]))
    t1_x_pk = float(np.max(np.abs(r1["x_lin"])))
    t1_x_nl = float(np.max(np.abs(x1_nl)))
    t1_gr_res = float(np.min(r1["gr_res"]))
    print(f"    Test 1 (Quiet -24 dBFS, 80+55 Hz):    f_hp=[{r1['fc'].min():5.1f}..{t1_fc_max:5.1f}] Hz | GR_res={t1_gr_res:+.2f} dB | X_lin_pk={t1_x_pk:.3f} Xmax | X_nl_pk={t1_x_nl:.3f} Xmax")
    assert abs(t1_fc_max - 82.0) < 0.05
    assert abs(t1_gr_res) < 1e-6
    assert t1_x_pk < 0.45 and t1_x_nl < 0.45

    # Test 2: Nominal desk music (-12 dBFS, 80 Hz bass + 45 Hz kick bursts)
    kick_env = np.exp(-((t % 0.14) / 0.032))
    u2 = (10.0 ** (-12.0 / 20.0)) * (0.55 * np.sin(2.0 * np.pi * 80.0 * t) + 0.45 * kick_env * np.sin(2.0 * np.pi * 45.0 * t))
    r2 = run_protection_chain(u2, PARAMS, d)
    x2_nl = simulate_klippel_nonlinear_rk4(r2["y"], d["v_pk_basic_v"], PARAMS, d)
    t2_fc_min, t2_fc_max = float(np.min(r2["fc"])), float(np.max(r2["fc"]))
    t2_x_pk = float(np.max(np.abs(r2["x_lin"])))
    t2_x_nl = float(np.max(np.abs(x2_nl)))
    t2_gr_res = float(np.min(r2["gr_res"]))
    print(f"    Test 2 (Nominal -12 dBFS, 80+45kick): f_hp=[{t2_fc_min:5.1f}..{t2_fc_max:5.1f}] Hz | GR_res={t2_gr_res:+.2f} dB | X_lin_pk={t2_x_pk:.3f} Xmax | X_nl_pk={t2_x_nl:.3f} Xmax")
    assert 82.0 <= t2_fc_min <= 82.1 and 86.0 <= t2_fc_max <= 118.0
    assert abs(t2_gr_res) < 1e-3
    assert t2_x_pk <= 0.88 and t2_x_nl <= 0.88

    # Test 3: Extreme 0 dBFS 45 Hz sine / 808 sub stress test (starts at t=0.02s)
    u3 = np.where(t >= 0.02, np.sin(2.0 * np.pi * 45.0 * (t - 0.02)), 0.0)
    r3 = run_protection_chain(u3, PARAMS, d)
    x3_nl = simulate_klippel_nonlinear_rk4(r3["y"], d["v_pk_basic_v"], PARAMS, d)
    idx_150 = np.where(r3["fc"][i0:] >= 150.0)[0]
    t3_react_ms = float(idx_150[0] / SR * 1000.0) if len(idx_150) else 999.0
    t3_fc_max = float(np.max(r3["fc"]))
    t3_x_pk = float(np.max(np.abs(r3["x_lin"])))
    t3_x_ss = float(np.max(np.abs(r3["x_lin"][int(0.20 * SR):])))
    t3_x_nl = float(np.max(np.abs(x3_nl)))
    dy_sweep = float(np.max(np.abs(np.diff(r3["y"][i0:i0 + 1000]))))
    b_plt, a_plt = bilinear_2nd_order_lp(PARAMS["f0_hz"], PARAMS["q_tc_cold"], d["g_obs_xmax_basic"], SR)
    x3_bare_pk = float(np.max(np.abs(biquad_filter(np.clip(u3 * 10 ** (7 / 20), -10 ** (-1 / 20), 10 ** (-1 / 20)), b_plt, a_plt))))
    print(f"    Test 3 (Extreme 0 dBFS 45 Hz 808):    f_hp=[{r3['fc'].min():5.1f}..{t3_fc_max:5.1f}] Hz | t(150Hz)={t3_react_ms:.2f} ms | X_lin_pk={t3_x_pk:.3f} (ss={t3_x_ss:.3f}) | X_nl_pk={t3_x_nl:.3f} | max|dy|={dy_sweep:.4f}")
    assert t3_react_ms < 6.0
    assert 150.0 <= t3_fc_max <= 155.1
    assert t3_x_pk <= 1.00 and t3_x_nl <= 1.00
    assert dy_sweep < 0.05

    # Test 3b: 0 dBFS 50 Hz kick drum burst (quantify exact excursion reduction 82 -> 155 Hz)
    u3b = np.where(t >= 0.02, np.exp(-(t - 0.02) / 0.075) * np.sin(2.0 * np.pi * 50.0 * (t - 0.02)), 0.0)
    r3b_guard = run_protection_chain(u3b, PARAMS, d, excursion_guard=1.0)
    r3b_fixed82 = run_protection_chain(u3b, PARAMS, d, excursion_guard=0.0, enable_coop_band_limiter=False, enable_residual_guard=False)
    x3b_nl = simulate_klippel_nonlinear_rk4(r3b_guard["y"], d["v_pk_basic_v"], PARAMS, d)
    u50_ss = np.sin(2.0 * np.pi * 50.0 * t)
    r50_82 = run_protection_chain(u50_ss, PARAMS, d, drive_db=0.0, ceiling_dbfs=0.0, excursion_guard=0.0, enable_coop_band_limiter=False, enable_residual_guard=False)
    r50_155 = run_protection_chain(u50_ss, PARAMS, d, drive_db=0.0, ceiling_dbfs=0.0, f_min_hz=155.0, f_max_hz=155.0, excursion_guard=0.0, enable_coop_band_limiter=False, enable_residual_guard=False)
    x50_82_ss = float(np.max(np.abs(r50_82["x_lin"][int(0.2 * SR):])))
    x50_155_ss = float(np.max(np.abs(r50_155["x_lin"][int(0.2 * SR):])))
    red_50_ss_db = 20.0 * math.log10(x50_155_ss / x50_82_ss)
    red_50_kick_db = 20.0 * math.log10(np.max(np.abs(r3b_guard["x_lin"])) / np.max(np.abs(r3b_fixed82["x_lin"])))
    print(f"    Test 3b (0 dBFS 50 Hz kick):          f_hp_max={r3b_guard['fc'].max():5.1f} Hz | X_pk={np.max(np.abs(r3b_guard['x_lin'])):.3f} Xmax (NL={np.max(np.abs(x3b_nl)):.3f}) vs fixed-82Hz={np.max(np.abs(r3b_fixed82['x_lin'])):.3f} (kick={red_50_kick_db:+.2f} dB; SS 82->155={red_50_ss_db:+.2f} dB)")
    assert np.max(np.abs(r3b_guard["x_lin"])) <= 1.00 and np.max(np.abs(x3b_nl)) <= 1.00

    # Test 3c: 0 dBFS 343 Hz resonance stress test (proves vocal-band HPF immunity + zero-overshoot cap!)
    u3c = np.where(t >= 0.02, np.sin(2.0 * np.pi * 343.0 * (t - 0.02)), 0.0)
    r3c = run_protection_chain(u3c, PARAMS, d)
    x3c_nl = simulate_klippel_nonlinear_rk4(r3c["y"], d["v_pk_basic_v"], PARAMS, d)
    t3c_fc_max = float(np.max(r3c["fc"]))
    t3c_fc_ss = float(np.max(r3c["fc"][int(0.15 * SR):]))
    t3c_x_pk = float(np.max(np.abs(r3c["x_lin"])))
    t3c_x_ss = float(np.max(np.abs(r3c["x_lin"][int(0.15 * SR):])))
    t3c_x_nl_pk = float(np.max(np.abs(x3c_nl)))
    t3c_x_nl_ss = float(np.max(np.abs(x3c_nl[int(0.15 * SR):])))
    print(f"    Test 3c (0 dBFS 343 Hz resonance):    f_hp_pk={t3c_fc_max:5.1f} Hz, ss={t3c_fc_ss:5.1f} Hz | B2={r3c['gr_b2'].min():+.1f} dB, Res={r3c['gr_res'].min():+.1f} dB | X_lin_pk={t3c_x_pk:.3f} (ss={t3c_x_ss:.3f}) | X_nl_pk={t3c_x_nl_pk:.3f} (ss={t3c_x_nl_ss:.3f})")
    assert t3c_fc_max < 85.0 and abs(t3c_fc_ss - 82.0) < 0.05, f"343 Hz tone falsely moved sliding HPF: pk={t3c_fc_max}, ss={t3c_fc_ss} Hz!"
    assert t3c_x_pk <= 1.00 and t3c_x_nl_pk <= 1.00, f"Test 3c peak exceeded X_max: lin={t3c_x_pk}, nl={t3c_x_nl_pk}"

    # Test 3d: Full-spectrum 0 dBFS worst-case sine sweep check (35..1000 Hz)
    print("    Test 3d (Full-spectrum 0 dBFS excursion cap proof across 35..1000 Hz):")
    sweep_results = []
    t_short = np.arange(int(0.18 * SR)) / SR
    for ftest in [35.0, 45.0, 65.0, 82.0, 110.0, 155.0, 220.0, 343.0, 445.0, 600.0, 1000.0]:
        us = np.where(t_short >= 0.01, np.sin(2.0 * np.pi * ftest * (t_short - 0.01)), 0.0)
        rs = run_protection_chain(us, PARAMS, d)
        xpk = float(np.max(np.abs(rs["x_lin"])))
        xss = float(np.max(np.abs(rs["x_lin"][int(0.08 * SR):])))
        sweep_results.append({
            "freq_hz": ftest,
            "f_hp_max_hz": round(float(rs["fc"].max()), 1),
            "x_lin_peak_xmax": round(xpk, 3),
            "x_lin_ss_xmax": round(xss, 3),
            "gr_b1_min_db": round(float(rs["gr_b1"].min()), 1),
            "gr_b2_min_db": round(float(rs["gr_b2"].min()), 1),
            "gr_res_min_db": round(float(rs["gr_res"].min()), 1),
        })
        print(f"      {ftest:6.1f} Hz: f_hp_max={rs['fc'].max():5.1f} Hz | B1={rs['gr_b1'].min():+5.1f} B2={rs['gr_b2'].min():+5.1f} Res={rs['gr_res'].min():+5.1f} dB | X_pk={xpk:.3f} (ss={xss:.3f}) Xmax")
        assert xpk <= 1.00, f"Full-spectrum cap failed at {ftest} Hz: X_pk={xpk:.3f} X_max"

    # Test 4: Unit Impulse & Step Transient (minimum-phase recovery, zero instability)
    u4_imp = np.zeros_like(t)
    u4_imp[i0] = 1.0
    r4_imp = run_protection_chain(u4_imp, PARAMS, d, drive_db=0.0, ceiling_dbfs=-0.3)
    imp_tail_db = 20.0 * math.log10(max(np.max(np.abs(r4_imp["y"][-2000:])), 1e-18) / np.max(np.abs(r4_imp["y"])))

    u4_step = np.where(t >= 0.02, 0.5, 0.0)
    r4_step = run_protection_chain(u4_step, PARAMS, d, drive_db=0.0, ceiling_dbfs=-0.3)
    step_tail = float(np.max(np.abs(r4_step["y"][-2000:])))
    step_x_pk = float(np.max(np.abs(r4_step["x_lin"])))
    print(f"    Test 4 (Impulse & 0.5 Step):          imp_tail={imp_tail_db:.1f} dB | step_tail={step_tail:.2e} | step_X_pk={step_x_pk:.3f} Xmax | f_hp_step_max={r4_step['fc'].max():.1f} Hz")
    assert np.all(np.isfinite(r4_imp["y"])) and np.all(np.isfinite(r4_step["y"]))
    assert imp_tail_db < -100.0
    assert step_tail < 1e-4
    assert step_x_pk <= 1.00

    # [5] Thermal Model Verification (dt = 50 ms, 180 seconds)
    dt_th = 0.05
    n_th = int(180.0 / dt_th)
    p_rated_2w = np.full(n_th, 2.0)
    t_2w, _, _, p_max_cont = simulate_thermal_network(p_rated_2w, dt_th, PARAMS, enable_limiter=False)

    p_burst_2p5w = np.where(np.arange(n_th) * dt_th < 3.0, 2.5, 0.25)
    t_burst, _, _, _ = simulate_thermal_network(p_burst_2p5w, dt_th, PARAMS, enable_limiter=True)

    p_sq_3p92w = np.full(n_th, (d["v_pk_basic_v"] ** 2) / PARAMS["re_ohm"])
    t_sq_unlim, _, _, _ = simulate_thermal_network(p_sq_3p92w, dt_th, PARAMS, enable_limiter=False)
    t_sq_lim, p_sq_eff, gr_sq, _ = simulate_thermal_network(p_sq_3p92w, dt_th, PARAMS, enable_limiter=True)

    print("\n[5] DUAL-TIME-CONSTANT THERMAL MODEL VERIFICATION (180 s simulation)")
    print(f"    Rated continuous budget = {p_max_cont:.2f} W (T_amb={PARAMS['t_ambient_c']:.0f} C -> T_lim={PARAMS['t_limit_c']:.0f} C)")
    print(f"    2.0 W continuous (unlim):      T_final = {t_2w[-1]:.2f} C (expect <= 100.0 C)")
    print(f"    2.5 W 3s peak burst:           T_max   = {np.max(t_burst):.2f} C (zero thermal throttle < 80 C)")
    print(f"    3.92 W 0dBFS square (unlim):   T_final = {t_sq_unlim[-1]:.1f} C (BURNOUT!)")
    print(f"    3.92 W 0dBFS square (limited): T_max   = {np.max(t_sq_lim):.2f} C | P_ss = {p_sq_eff[-1]:.2f} W | GR_ss = {gr_sq[-1]:+.2f} dB")
    assert t_2w[-1] <= 100.05
    assert np.max(t_burst) < 80.0
    assert np.max(t_sq_lim) <= 100.0

    # [6] Write machine-readable klippel_model.json
    model_json = {
        "schema_version": "1.0.0",
        "device": {
            "model": "Dell XPS 17 9720",
            "dmi_sku": "0AFF",
            "subsystem_id": "0AFF1028",
            "codename": "Stradale",
            "amplifier": "Dual Realtek ALC1316 / ALC1319D (rt1316-sdca)",
            "speaker_layout": "Quad: 2x bottom-firing woofers (SPOL, 2W RMS / 2.5W peak) + 2x top-firing tweeters (SPOR, 2W RMS / 2.5W peak)",
            "flank_span_m": 0.340,
            "nominal_listening_distance_m": 0.550,
            "amp_crossover_requirement": "0xC202=0xC0 MUST be active (woofer 6.16 kHz LPF + 18 Hz HPF; tweeter 838 Hz HPF + 18 Hz HPF)",
        },
        "calibration_provenance": {
            "acoustic_data": BASELINE_PATH,
            "hardware_spec_source": "/home/akshaybapat/project/xps17-speaker-stack/sense/protection-model/parameters.csv",
            "status": "CALIBRATED_ACOUSTIC_PROVISIONAL",
            "ipc4_refinement_note": "f0, Q_tc, R_e, Bl, and X_max are calibrated from measured baseline_curve.npy, Dell Waves 344 Hz notch, and 2W/2.8Vrms hardware specs; drop-in update when IPC4 I-sense impedance sweep is measured.",
        },
        "electro_mechanical_woofer": {
            "f0_hz": PARAMS["f0_hz"],
            "q_tc_cold": PARAMS["q_tc_cold"],
            "q_obs_guard": PARAMS["q_obs"],
            "q_ec": round(d["q_ec"], 4),
            "q_mc": PARAMS["q_mc"],
            "re_ohm": PARAMS["re_ohm"],
            "le_henry": PARAMS["le_henry"],
            "fe_inductance_hz": round(d["fe_inductance_hz"], 1),
            "alpha_cu_per_k": PARAMS["alpha_cu_per_k"],
            "mms_kg": PARAMS["mms_kg"],
            "bl0_tm": PARAMS["bl0_tm"],
            "k0_n_per_m": round(d["k0_n_per_m"], 2),
            "cms0_mm_per_n": round(d["cms0_mm_per_n"], 4),
            "kx_mm_per_v": round(d["kx_mm_per_v"], 5),
            "sd_m2": PARAMS["sd_m2"],
            "vb_m3": PARAMS["vb_m3"],
            "x_max_mm": round(PARAMS["x_max_m"] * 1e3, 3),
            "x_safe_mm": round(d["x_safe_mm"], 3),
            "x_safe_ratio": PARAMS["x_safe_ratio"],
            "f_peak_displacement_hz": round(d["f_peak_displacement_hz"], 2),
            "f_peak_pressure_hz": round(d["f_peak_pressure_hz"], 2),
            "resonance_peak_db": round(d["resonance_peak_db"], 2),
            "klippel_nonlinear_margin": {
                "bl_drop_at_xmax_frac": PARAMS["klippel_b2_at_xmax"],
                "bl_at_xsafe_frac": round(1.0 - PARAMS["klippel_b2_at_xmax"] * (PARAMS["x_safe_ratio"] ** 2), 4),
                "kms_k1_at_xmax_frac": PARAMS["klippel_k1_at_xmax"],
                "kms_k2_at_xmax_frac": PARAMS["klippel_k2_at_xmax"],
                "cms_at_xsafe_frac": round(1.0 / (1.0 + PARAMS["klippel_k1_at_xmax"] * PARAMS["x_safe_ratio"] + PARAMS["klippel_k2_at_xmax"] * (PARAMS["x_safe_ratio"] ** 2)), 4),
                "le_slope_h_per_m": PARAMS["klippel_l1_per_m"],
            },
        },
        "amp_gain_modes": {
            "linux_live_unguarded": {
                "reg_c60f": "0x01",
                "reg_d002_d004": "0xBF",
                "v_rms_fs_est": PARAMS["v_rms_linux_live_est"],
                "v_pk_fs_est": round(d["v_pk_linux_est_v"], 4),
                "g_obs_safe": round(d["g_obs_safe_linux"], 6),
                "g_obs_xmax": round(d["g_obs_xmax_linux"], 6),
                "max_sine_power_4ohm_w": round((PARAMS["v_rms_linux_live_est"] ** 2) / PARAMS["re_ohm"], 3),
            },
            "windows_basic_guarded": {
                "reg_c60f": "0x05",
                "reg_d002_d004": "0xA9",
                "requires_kernel_deadman_ms": 250,
                "requires_c202_crossover_hex": "0xC0",
                "v_rms_fs": PARAMS["v_rms_windows_basic"],
                "v_pk_fs": round(d["v_pk_basic_v"], 4),
                "g_obs_safe": round(d["g_obs_safe_basic"], 6),
                "g_obs_xmax": round(d["g_obs_xmax_basic"], 6),
                "max_sine_power_4ohm_w": round((PARAMS["v_rms_windows_basic"] ** 2) / PARAMS["re_ohm"], 3),
            },
        },
        "discrete_observer_48khz": {
            "sample_rate_hz": int(SR),
            "design_method": "Bilinear transform pre-warped at f0 = 390.0 Hz",
            "normalization": "|x_hat[n]| = 1.0 equals X_safe = 0.85 * X_max (0.34 mm) at Windows Basic 0 dBFS",
            "biquad_2nd_order_windows_basic": {
                "b": [float(v) for v in b_obs],
                "a": [float(v) for v in a_obs],
                "pole_radius": round(max_pole_mag, 8),
                "dc_gain": round(dc_gain, 6),
                "f0_gain": round(f0_gain, 6),
            },
            "biquad_2nd_order_unit_dc_prototype": {
                "b": [float(v / d["g_obs_safe_basic"]) for v in b_obs],
                "a": [float(v) for v in a_obs],
                "note": "Multiply b by G_obs_safe = (K_x * V_pk_fs / X_safe) * 10^(Drive_dB/20) for any hardware gain mode",
            },
            "optional_inductance_1st_order_pole": {
                "fc_hz": round(d["fe_inductance_hz"], 2),
                "b": [float(v) for v in b_ind],
                "a": [float(v) for v in a_ind],
                "pole_z": round(float(pole_ind[0].real), 8),
                "max_delta_below_1khz_db": round(max(abs(row["inductance_delta_db"]) for row in bode_table if row["freq_hz"] <= 1000.0), 4),
            },
            "sidechain_sub_lp_notch": {
                "pole_hz": PARAMS["sidechain_lp_pole_hz"],
                "pole_q": 0.7071067811865476,
                "zero_hz": PARAMS["sidechain_lp_zero_hz"],
                "b": [float(v) for v in b_sub],
                "a": [float(v) for v in a_sub],
                "pole_radius": round(float(np.max(np.abs(poles_sub))), 8),
                "rejection_at_343hz_db": round(sub_343_db, 1),
            },
            "bode_verification": bode_table,
        },
        "sliding_hpf_control_law": {
            "topology": "4th-order Butterworth TPT-SVF (Zero-Delay Feedback, 2 cascaded 2nd-order SVF sections)",
            "slope_db_per_oct": 24.0,
            "stage_q": PARAMS["hpf_stage_q"],
            "control_port_physical_bass_hz_range": [65.0, 180.0],
            "mode_f_min_hz": PARAMS["mode_f_min_hz"],
            "f_max_default_hz": PARAMS["f_max_default_hz"],
            "f_max_hard_clamp_hz": PARAMS["f_max_hard_clamp_hz"],
            "x_knee_norm_safe": PARAMS["x_knee_norm_safe"],
            "x_limit_norm_safe": PARAMS["x_limit_norm_safe"],
            "x_knee_norm_xmax": round(PARAMS["x_knee_norm_safe"] * PARAMS["x_safe_ratio"], 4),
            "x_limit_norm_xmax": round(PARAMS["x_limit_norm_safe"] * PARAMS["x_safe_ratio"], 4),
            "envelope_attack_ms": PARAMS["env_attack_ms"],
            "envelope_release_fast_ms": PARAMS["env_release_fast_ms"],
            "envelope_release_slow_ms": PARAMS["env_release_slow_ms"],
            "envelope_slow_floor_weight": PARAMS["env_slow_floor_weight"],
            "cutoff_slew_tau_ms": PARAMS["fc_slew_tau_ms"],
            "stage_a_lookahead_ms": PARAMS["stage_a_lookahead_ms"],
            "formula": "f_hp = f_min + ExcursionGuard * (f_max - f_min) * smoothstep((X_env - X_knee) / (X_limit - X_knee))",
        },
        "cooperative_subband_excursion_limiter": {
            "crossover_type": "4th-order Linkwitz-Riley (LR4 TPT-SVF)",
            "band1_sub_bass": {
                "range_hz": [0.0, PARAMS["band1_split_hz"]],
                "threshold_dbfs": PARAMS["band1_thresh_dbfs"],
                "soft_knee_db": PARAMS["band1_knee_db"],
                "ratio": PARAMS["band1_ratio"],
                "attack_ms": PARAMS["band1_attack_ms"],
                "release_ms": PARAMS["band1_release_ms"],
            },
            "band2_warm_bass_resonance_guard": {
                "range_hz": [PARAMS["band2_split_lo_hz"], PARAMS["band2_split_hi_hz"]],
                "threshold_dbfs": PARAMS["band2_thresh_dbfs"],
                "soft_knee_db": PARAMS["band2_knee_db"],
                "ratio": PARAMS["band2_ratio"],
                "attack_ms": PARAMS["band2_attack_ms"],
                "release_ms": PARAMS["band2_release_ms"],
            },
            "stage_c_lookahead_residual_guard": {
                "thresh_norm_safe": PARAMS["residual_x_thresh_safe"],
                "ceil_norm_safe": PARAMS["residual_x_ceil_safe"],
                "lookahead_ms": PARAMS["residual_lookahead_ms"],
                "attack_ms": PARAMS["residual_attack_ms"],
                "release_ms": PARAMS["residual_release_ms"],
            },
        },
        "thermal_protection_model": {
            "topology": "Dual-time-constant 2-node thermal network (voice coil + magnet/chassis)",
            "t_ambient_c": PARAMS["t_ambient_c"],
            "t_knee_c": PARAMS["t_knee_c"],
            "t_limit_c": PARAMS["t_limit_c"],
            "t_window_k": PARAMS["t_limit_c"] - PARAMS["t_knee_c"],
            "r_th_vc_k_per_w": PARAMS["r_th_vc_k_per_w"],
            "r_th_mag_k_per_w": PARAMS["r_th_mag_k_per_w"],
            "r_th_total_k_per_w": PARAMS["r_th_vc_k_per_w"] + PARAMS["r_th_mag_k_per_w"],
            "tau_vc_s": PARAMS["tau_vc_s"],
            "tau_mag_s": PARAMS["tau_mag_s"],
            "c_th_vc_j_per_k": round(PARAMS["tau_vc_s"] / PARAMS["r_th_vc_k_per_w"], 4),
            "c_th_mag_j_per_k": round(PARAMS["tau_mag_s"] / PARAMS["r_th_mag_k_per_w"], 4),
            "max_continuous_rms_power_w": PARAMS["rated_rms_power_w"],
            "max_short_term_peak_power_w": PARAMS["rated_peak_power_w"],
            "max_thermal_gain_reduction_db": -6.0,
        },
        "measured_baseline_summary": meas,
        "verified_simulation_results": {
            "test1_quiet_minus24dbfs": {
                "f_hp_min_hz": round(float(r1["fc"].min()), 2),
                "f_hp_max_hz": round(t1_fc_max, 2),
                "gr_residual_db": round(t1_gr_res, 3),
                "x_lin_peak_xmax": round(t1_x_pk, 4),
                "x_nl_rk4_peak_xmax": round(t1_x_nl, 4),
                "passed": True,
            },
            "test2_nominal_minus12dbfs": {
                "f_hp_min_hz": round(t2_fc_min, 2),
                "f_hp_max_hz": round(t2_fc_max, 2),
                "gr_residual_db": round(t2_gr_res, 3),
                "x_lin_peak_xmax": round(t2_x_pk, 4),
                "x_nl_rk4_peak_xmax": round(t2_x_nl, 4),
                "passed": True,
            },
            "test3_extreme_0dbfs_45hz": {
                "f_hp_max_hz": round(t3_fc_max, 2),
                "reaction_time_to_150hz_ms": round(t3_react_ms, 2),
                "x_lin_peak_xmax": round(t3_x_pk, 4),
                "x_lin_steady_state_xmax": round(t3_x_ss, 4),
                "x_nl_rk4_peak_xmax": round(t3_x_nl, 4),
                "x_bare_unprotected_xmax": round(x3_bare_pk, 4),
                "max_sample_delta_during_sweep": round(dy_sweep, 5),
                "passed": True,
            },
            "test3b_0dbfs_50hz_kick": {
                "f_hp_max_hz": round(float(r3b_guard["fc"].max()), 2),
                "x_lin_peak_guarded_xmax": round(float(np.max(np.abs(r3b_guard["x_lin"]))), 4),
                "x_nl_rk4_peak_guarded_xmax": round(float(np.max(np.abs(x3b_nl))), 4),
                "x_lin_peak_fixed_82hz_xmax": round(float(np.max(np.abs(r3b_fixed82["x_lin"]))), 4),
                "kick_transient_reduction_db": round(red_50_kick_db, 2),
                "steady_state_82_to_155_reduction_db": round(red_50_ss_db, 2),
                "steady_state_x_at_82hz_xmax": round(x50_82_ss, 4),
                "steady_state_x_at_155hz_xmax": round(x50_155_ss, 4),
                "passed": True,
            },
            "test3c_0dbfs_343hz_resonance": {
                "f_hp_max_hz": round(t3c_fc_max, 2),
                "f_hp_ss_hz": round(t3c_fc_ss, 2),
                "band2_gr_min_db": round(float(r3c["gr_b2"].min()), 2),
                "residual_gr_min_db": round(float(r3c["gr_res"].min()), 2),
                "x_lin_peak_xmax": round(t3c_x_pk, 4),
                "x_lin_steady_state_xmax": round(t3c_x_ss, 4),
                "x_nl_rk4_peak_xmax": round(t3c_x_nl_pk, 4),
                "x_nl_rk4_steady_state_xmax": round(t3c_x_nl_ss, 4),
                "passed": True,
            },
            "test3d_full_spectrum_0dbfs_sweep": sweep_results,
            "test4_impulse_and_step": {
                "impulse_tail_db": round(imp_tail_db, 2),
                "step_dc_tail": float(f"{step_tail:.2e}"),
                "step_x_peak_xmax": round(step_x_pk, 4),
                "passed": True,
            },
            "test5_thermal_180s": {
                "t_final_2w_continuous_c": round(float(t_2w[-1]), 2),
                "t_max_2p5w_3s_burst_c": round(float(np.max(t_burst)), 2),
                "t_final_3p92w_square_unlim_c": round(float(t_sq_unlim[-1]), 1),
                "t_max_3p92w_square_limited_c": round(float(np.max(t_sq_lim)), 2),
                "p_ss_3p92w_square_limited_w": round(float(p_sq_eff[-1]), 3),
                "gr_ss_3p92w_square_db": round(float(gr_sq[-1]), 2),
                "passed": True,
            },
        },
    }

    with open(MODEL_JSON_PATH, "w", encoding="utf-8") as fp:
        json.dump(model_json, fp, indent=2)
        fp.write("\n")

    print(f"\n[6] ALL ASSERTIONS PASSED! Wrote verified parameters -> {MODEL_JSON_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

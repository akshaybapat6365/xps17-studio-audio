#!/usr/bin/env python3
"""Publication-grade dark-studio SVG -> high-DPI PNG chart renderer (pure stdlib + rsvg-convert).

Produces 4 crisp 2200x1240 px engineering charts inside verify-integrate/bench/charts/:
  1. 01_frequency_response.png
  2. 02_bass_and_body_comparison.png
  3. 03_stereo_correlation_and_depth.png
  4. 04_dynamics_loudness_thd.png
"""

from __future__ import annotations

import html
import math
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Smoked Aether Optical Studio Glass Theme Tokens (4K 3840x2160 UHD)
# ---------------------------------------------------------------------------
BG = "#070A15"
PANEL = "#0E1326"
PANEL_BORDER = "#2B365E"
GRID_MAJOR = "#212B4A"
GRID_MINOR = "#161E36"
TEXT_PRI = "#F2F6FF"
TEXT_SEC = "#B0BDD6"
TEXT_MUTED = "#7E8CA8"
FONT_SANS = "Inter Display, Inter, DejaVu Sans, sans-serif"
FONT_MONO = "JetBrainsMono Nerd Font, DejaVu Sans Mono, monospace"

CHAIN_STYLES: Dict[str, Dict[str, Any]] = {
    "stock_raw": {"color": "#94A3B8", "dash": "7,4", "width": 2.4, "short": "Stock XPS 17"},
    "old_spatial_fdtr": {"color": "#FB7185", "dash": "", "width": 2.8, "short": "OLD Spatial FDTR"},
    "new_studio_deep": {"color": "#5CE1E6", "dash": "", "width": 3.6, "short": "NEW Studio Deep"},
    "new_studio_spatial": {"color": "#B39DFF", "dash": "", "width": 2.7, "short": "NEW Spatial"},
    "new_studio_reference": {"color": "#4ADE80", "dash": "8,3", "width": 2.4, "short": "NEW Reference"},
    "new_studio_voice": {"color": "#FBBF24", "dash": "3,3", "width": 2.2, "short": "NEW Voice"},
}


def esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def map_lin(v: float, d0: float, d1: float, r0: float, r1: float) -> float:
    if d1 == d0:
        return r0
    return r0 + (v - d0) * (r1 - r0) / (d1 - d0)


def map_log(v: float, f0: float, f1: float, r0: float, r1: float) -> float:
    lv = math.log10(max(v, 1e-9))
    return map_lin(lv, math.log10(f0), math.log10(f1), r0, r1)


def polyline_pts(xs: Sequence[float], ys: Sequence[float]) -> str:
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))


def svg_header(w: int, h: int, title: str, subtitle: str, all_verified: bool) -> List[str]:
    badge_txt = "BINARY VERIFIED · libxps17_studio_dsp.so" if all_verified else "OLD: BINARY VERIFIED  |  NEW: DESIGN REF (PENDING BINARY)"
    badge_fill = "#0D2E28" if all_verified else "#2B2110"
    badge_stroke = "#34D399" if all_verified else "#F59E0B"
    badge_text_col = "#5CE1A6" if all_verified else "#FBBF24"
    badge_w = 296 if all_verified else 380
    bx = w - 22 - badge_w
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="3840" height="2160" viewBox="0 0 {w} {h}">',
        '<defs>',
        '  <radialGradient id="aetherBg" cx="50%" cy="18%" r="78%">',
        '    <stop offset="0%" stop-color="#171E3F"/>',
        '    <stop offset="52%" stop-color="#0B0F20"/>',
        f'    <stop offset="100%" stop-color="{BG}"/>',
        '  </radialGradient>',
        '  <radialGradient id="aetherFloor" cx="50%" cy="102%" r="60%">',
        '    <stop offset="0%" stop-color="#3B2B6E" stop-opacity="0.34"/>',
        '    <stop offset="100%" stop-color="#070A15" stop-opacity="0"/>',
        '  </radialGradient>',
        '</defs>',
        f'<rect width="{w}" height="{h}" fill="url(#aetherBg)"/>',
        f'<rect width="{w}" height="{h}" fill="url(#aetherFloor)"/>',
        f'<text x="24" y="31" fill="{TEXT_PRI}" font-family="{FONT_SANS}" font-size="20" font-weight="800">{esc(title)}</text>',
        f'<text x="24" y="51" fill="{TEXT_SEC}" font-family="{FONT_SANS}" font-size="12.2" font-weight="500">{esc(subtitle)}</text>',
        f'<rect x="{bx}" y="13" width="{badge_w}" height="26" rx="13" fill="{badge_fill}" stroke="{badge_stroke}" stroke-width="1.3"/>',
        f'<text x="{bx + badge_w / 2:.1f}" y="30" text-anchor="middle" fill="{badge_text_col}" font-family="{FONT_MONO}" font-size="10.2" font-weight="700">{esc(badge_txt)}</text>',
    ]


def svg_panel(x: float, y: float, w: float, h: float, title: str = "", subtitle: str = "") -> List[str]:
    out = [
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{PANEL}" fill-opacity="0.88" stroke="{PANEL_BORDER}" stroke-width="1.2"/>',
        f'<line x1="{x + 12}" y1="{y + 0.8}" x2="{x + w - 12}" y2="{y + 0.8}" stroke="#8E9CF5" stroke-opacity="0.32" stroke-width="1"/>',
    ]
    if title:
        out.append(
            f'<text x="{x + 15}" y="{y + 22}" fill="{TEXT_PRI}" font-family="{FONT_SANS}" font-size="13.8" font-weight="700">{esc(title)}</text>'
        )
    if subtitle:
        out.append(
            f'<text x="{x + 15}" y="{y + 38}" fill="{TEXT_SEC}" font-family="{FONT_SANS}" font-size="10.8">{esc(subtitle)}</text>'
        )
    return out


def render_svg_to_png(svg_str: str, svg_path: Path, png_path: Path, width_px: int = 3840) -> Path:
    svg_path.write_text(svg_str, encoding="utf-8")
    subprocess.run(
        ["rsvg-convert", "-w", str(width_px), "-h", "2160", "-b", BG, "-o", str(png_path), str(svg_path)],
        check=True,
        capture_output=True,
    )
    return png_path


def _chain_short_label(chains: Dict[str, Any], cid: str) -> str:
    st = CHAIN_STYLES[cid]["short"]
    c = chains[cid]
    if not c.get("binary_verified", True):
        return f"{st}*"
    return st


# ---------------------------------------------------------------------------
# Chart 01: Projected Acoustic Frequency Response (20 Hz - 20 kHz)
# ---------------------------------------------------------------------------
def build_chart_01(results: Dict[str, Any], out_dir: Path) -> Path:
    W, H = 1100, 620
    chains = results["chains"]
    all_v = results.get("all_binaries_verified", False)
    el = svg_header(
        W,
        H,
        "Projected In-Room Acoustic Frequency Response (Dell XPS 17 9720)",
        "Measured Blue Yeti X speaker curve (baseline_curve.npy, 0.076 dB repeatability) + DSP transfer function (normalized at 1 kHz)",
        all_v,
    )

    order = [k for k in ("stock_raw", "old_spatial_fdtr", "new_studio_deep", "new_studio_spatial", "new_studio_reference") if k in chains]

    # Top Legend Row (y=62..86)
    lx = 28.0
    for cid in order:
        st = CHAIN_STYLES[cid]
        lbl = _chain_short_label(chains, cid)
        dash = f' stroke-dasharray="{st["dash"]}"' if st["dash"] else ""
        el.append(f'<line x1="{lx:.1f}" y1="74" x2="{lx + 24:.1f}" y2="74" stroke="{st["color"]}" stroke-width="{st["width"]}"{dash}/>')
        el.append(f'<text x="{lx + 30:.1f}" y="78" fill="{TEXT_PRI}" font-family="{FONT_SANS}" font-size="11.2" font-weight="600">{esc(lbl)}</text>')
        lx += 30 + len(lbl) * 7.2 + 24

    if not all_v:
        el.append(f'<text x="{W - 26}" y="78" text-anchor="end" fill="#FBBF24" font-family="{FONT_SANS}" font-size="10">* = Design Reference Model (Pending Binary)</text>')

    # Main Plot Card
    el += svg_panel(20, 90, 1060, 512)
    px0, px1 = 82.0, 1052.0
    py0, py1 = 118.0, 548.0
    fmin, fmax = 20.0, 20000.0
    ymin, ymax = -45.0, 15.0

    # Shaded acoustic zones
    zones = [
        (70.0, 180.0, "#38BDF8", 0.08, "Phys Bass\n70–180 Hz"),
        (180.0, 350.0, "#818CF8", 0.07, "Mid-Bass\n180–350 Hz"),
        (1000.0, 4000.0, "#34D399", 0.06, "Vocal Clarity\n1–4 kHz"),
        (6000.0, 14000.0, "#FBBF24", 0.06, "Treble Air\n6–14 kHz"),
    ]
    for flo, fhi, col, op, zlbl in zones:
        xa = map_log(flo, fmin, fmax, px0, px1)
        xb = map_log(fhi, fmin, fmax, px0, px1)
        el.append(f'<rect x="{xa:.1f}" y="{py0:.1f}" width="{xb - xa:.1f}" height="{py1 - py0:.1f}" fill="{col}" fill-opacity="{op}"/>')
        cx = 0.5 * (xa + xb)
        parts = zlbl.split("\n")
        el.append(f'<text x="{cx:.1f}" y="{py0 + 14:.1f}" text-anchor="middle" fill="{col}" font-family="{FONT_SANS}" font-size="9.5" font-weight="700">{esc(parts[0])}</text>')
        el.append(f'<text x="{cx:.1f}" y="{py0 + 26:.1f}" text-anchor="middle" fill="{TEXT_MUTED}" font-family="{FONT_MONO}" font-size="8.6">{esc(parts[1])}</text>')

    # Horizontal dB grid
    for db in range(-45, 20, 5):
        y = map_lin(db, ymin, ymax, py1, py0)
        is_zero = db == 0
        stroke = "#475569" if is_zero else GRID_MAJOR
        sw = 1.2 if is_zero else 0.7
        el.append(f'<line x1="{px0:.1f}" y1="{y:.1f}" x2="{px1:.1f}" y2="{y:.1f}" stroke="{stroke}" stroke-width="{sw}"/>')
        el.append(f'<text x="{px0 - 10:.1f}" y="{y + 4:.1f}" text-anchor="end" fill="{TEXT_SEC if not is_zero else TEXT_PRI}" font-family="{FONT_MONO}" font-size="10">{db:+d}</text>')

    # Vertical log-frequency grid
    xticks = [
        (20, "20"), (30, "30"), (50, "50"), (70, "70"), (100, "100"), (150, "150"), (200, "200"),
        (300, "300"), (500, "500"), (1000, "1k"), (2000, "2k"), (3000, "3k"), (5000, "5k"),
        (8000, "8k"), (10000, "10k"), (15000, "15k"), (20000, "20k"),
    ]
    for hz, lbl in xticks:
        x = map_log(hz, fmin, fmax, px0, px1)
        el.append(f'<line x1="{x:.1f}" y1="{py0:.1f}" x2="{x:.1f}" y2="{py1:.1f}" stroke="{GRID_MINOR}" stroke-width="0.8"/>')
        el.append(f'<text x="{x:.1f}" y="{py1 + 17:.1f}" text-anchor="middle" fill="{TEXT_SEC}" font-family="{FONT_MONO}" font-size="10">{lbl}</text>')

    # 15 kHz Yeti X sweep boundary
    x15 = map_log(15000.0, fmin, fmax, px0, px1)
    el.append(f'<line x1="{x15:.1f}" y1="{py0:.1f}" x2="{x15:.1f}" y2="{py1:.1f}" stroke="#64748B" stroke-width="1" stroke-dasharray="4,4"/>')
    el.append(f'<text x="{x15 - 6:.1f}" y="{py0 + 44:.1f}" text-anchor="end" fill="{TEXT_MUTED}" font-family="{FONT_SANS}" font-size="9">Yeti X sweep bound (15 kHz)</text>')

    # Clip region for curves
    el.append(f'<defs><clipPath id="clipFR"><rect x="{px0}" y="{py0}" width="{px1 - px0}" height="{py1 - py0}"/></clipPath></defs>')
    el.append('<g clip-path="url(#clipFR)">')
    for cid in order:
        st = CHAIN_STYLES[cid]
        fr = chains[cid]["frequency_response"]
        xs = [map_log(f, fmin, fmax, px0, px1) for f in fr["freqs_hz"]]
        ys = [map_lin(clamp(v, ymin - 2, ymax + 2), ymin, ymax, py1, py0) for v in fr["acoustic_rel_1khz_db"]]
        dash = f' stroke-dasharray="{st["dash"]}"' if st["dash"] else ""
        el.append(f'<polyline fill="none" stroke="{st["color"]}" stroke-width="{st["width"]}" stroke-linejoin="round" stroke-linecap="round"{dash} points="{polyline_pts(xs, ys)}"/>')
    el.append("</g>")

    # Axis titles
    el.append(f'<text x="{0.5 * (px0 + px1):.1f}" y="{py1 + 38:.1f}" text-anchor="middle" fill="{TEXT_SEC}" font-family="{FONT_SANS}" font-size="11.5" font-weight="600">Frequency (Hz, logarithmic scale)</text>')
    el.append(f'<text x="29" y="{0.5 * (py0 + py1):.1f}" text-anchor="middle" fill="{TEXT_SEC}" font-family="{FONT_SANS}" font-size="11.5" font-weight="600" transform="rotate(-90 29 {0.5 * (py0 + py1):.1f})">Projected Acoustic SPL (dB re 1 kHz)</text>')

    # Delta Callout Box in clean bottom-left/center-bottom open area
    if "old_spatial_fdtr" in chains and "new_studio_deep" in chains:
        old_sp = chains["old_spatial_fdtr"]["frequency_response"]["spots"]
        new_sp = chains["new_studio_deep"]["frequency_response"]["spots"]
        stk_sp = chains["stock_raw"]["frequency_response"]["spots"]
        d125 = new_sp["125Hz"]["acoustic_rel_1khz_db"] - old_sp["125Hz"]["acoustic_rel_1khz_db"]
        d150 = new_sp["150Hz"]["acoustic_rel_1khz_db"] - old_sp["150Hz"]["acoustic_rel_1khz_db"]
        d1500 = new_sp["1500Hz"]["acoustic_rel_1khz_db"] - stk_sp["1500Hz"]["acoustic_rel_1khz_db"]
        d10k = new_sp["10000Hz"]["acoustic_rel_1khz_db"] - stk_sp["10000Hz"]["acoustic_rel_1khz_db"]

        bx, by, bw, bh = 555, 442, 476, 92
        el.append(f'<rect x="{bx}" y="{by}" width="{bw}" height="{bh}" rx="6" fill="#0B111A" fill-opacity="0.92" stroke="#334155" stroke-width="1"/>')
        el.append(f'<text x="{bx + 12}" y="{by + 19}" fill="#38BDF8" font-family="{FONT_SANS}" font-size="10.8" font-weight="700">KEY ACOUSTIC DELTAS (NEW Studio Deep vs Baselines)</text>')
        el.append(f'<text x="{bx + 12}" y="{by + 39}" fill="{TEXT_PRI}" font-family="{FONT_MONO}" font-size="10">125 Hz Phys Bass vs OLD 180Hz HPF: {d125:+6.1f} dB   |   150 Hz: {d150:+6.1f} dB</text>')
        el.append(f'<text x="{bx + 12}" y="{by + 58}" fill="{TEXT_PRI}" font-family="{FONT_MONO}" font-size="10">1.5 kHz Harsh Peak Tamed vs Stock: {d1500:+6.1f} dB   |  10 kHz Air: {d10k:+6.1f} dB</text>')
        el.append(f'<text x="{bx + 12}" y="{by + 77}" fill="{TEXT_MUTED}" font-family="{FONT_SANS}" font-size="9.5">OLD 180 Hz 8th-order (48 dB/oct) HPF causes the steep low-end cliff visible in red.</text>')

    # Plot frame
    el.append(f'<rect x="{px0}" y="{py0}" width="{px1 - px0}" height="{py1 - py0}" fill="none" stroke="#334155" stroke-width="1"/>')
    el.append("</svg>")
    return render_svg_to_png("\n".join(el), out_dir / "01_frequency_response.svg", out_dir / "01_frequency_response.png")


# ---------------------------------------------------------------------------
# Chart 02: Bass Preservation (Speech-Gate Audit), Harmonic Spectrum & Klippel Excursion
# ---------------------------------------------------------------------------
def build_chart_02(results: Dict[str, Any], out_dir: Path) -> Path:
    W, H = 1100, 620
    chains = results["chains"]
    all_v = results.get("all_binaries_verified", False)
    el = svg_header(
        W,
        H,
        "Bass Body, Speech-Gate Preservation & Klippel Woofer Excursion",
        "Proving OLD speech_gate bass choking, 40–400 Hz missing-fundamental harmonics & XPS 17 mechanical X_max safety",
        all_v,
    )

    show = [k for k in ("stock_raw", "old_spatial_fdtr", "new_studio_deep", "new_studio_spatial") if k in chains]

    # Top-Left Panel A: Vocal + Bass Mix (70-240 Hz Envelope over 0..4s)
    el += svg_panel(20, 64, 520, 268, "A. Vocal + Bass Mix: Radiated Bass Band (70–240 Hz)", "Continuous 55+85 Hz bassline; shaded = 680–2650 Hz vocals active")
    ax0, ax1 = 72.0, 522.0
    ay0, ay1 = 112.0, 296.0

    # Shaded vocal intervals [1.0, 2.0] and [2.6, 3.6]
    for t0, t1 in ((1.0, 2.0), (2.6, 3.6)):
        xa = map_lin(t0, 0.0, 4.0, ax0, ax1)
        xb = map_lin(t1, 0.0, 4.0, ax0, ax1)
        el.append(f'<rect x="{xa:.1f}" y="{ay0:.1f}" width="{xb - xa:.1f}" height="{ay1 - ay0:.1f}" fill="#F59E0B" fill-opacity="0.11"/>')
        el.append(f'<text x="{0.5 * (xa + xb):.1f}" y="{ay0 + 12:.1f}" text-anchor="middle" fill="#FBBF24" font-family="{FONT_SANS}" font-size="8.8" font-weight="600">Vocals ON</text>')

    # Determine Y range dynamically from active curves (clamped sensibly)
    vb_vals = []
    for cid in show[:3]:
        vb_vals.extend(chains[cid]["vocal_bass_preservation"]["envelope_db"][8:-8])
    ymin_a = math.floor((min(vb_vals) - 4.0) / 5.0) * 5.0
    ymax_a = math.ceil((max(vb_vals) + 4.0) / 5.0) * 5.0
    ymin_a = max(ymin_a, -75.0)
    ymax_a = max(ymax_a, ymin_a + 15.0)

    for db in range(int(ymin_a), int(ymax_a) + 1, 5):
        y = map_lin(db, ymin_a, ymax_a, ay1, ay0)
        el.append(f'<line x1="{ax0:.1f}" y1="{y:.1f}" x2="{ax1:.1f}" y2="{y:.1f}" stroke="{GRID_MAJOR}" stroke-width="0.6"/>')
        if db % 10 == 0:
            el.append(f'<text x="{ax0 - 6:.1f}" y="{y + 3.5:.1f}" text-anchor="end" fill="{TEXT_SEC}" font-family="{FONT_MONO}" font-size="9">{db}</text>')
    for ts in (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0):
        x = map_lin(ts, 0.0, 4.0, ax0, ax1)
        el.append(f'<line x1="{x:.1f}" y1="{ay0:.1f}" x2="{x:.1f}" y2="{ay1:.1f}" stroke="{GRID_MINOR}" stroke-width="0.6"/>')
        el.append(f'<text x="{x:.1f}" y="{ay1 + 13:.1f}" text-anchor="middle" fill="{TEXT_SEC}" font-family="{FONT_MONO}" font-size="9">{ts:.1f}s</text>')

    el.append(f'<defs><clipPath id="clipA"><rect x="{ax0}" y="{ay0}" width="{ax1 - ax0}" height="{ay1 - ay0}"/></clipPath></defs>')
    el.append('<g clip-path="url(#clipA)">')
    for cid in show[:3]:
        st = CHAIN_STYLES[cid]
        vb = chains[cid]["vocal_bass_preservation"]
        xs = [map_lin(t, 0.0, 4.0, ax0, ax1) for t in vb["envelope_t_s"]]
        ys = [map_lin(clamp(v, ymin_a - 2, ymax_a + 2), ymin_a, ymax_a, ay1, ay0) for v in vb["envelope_db"]]
        dash = f' stroke-dasharray="{st["dash"]}"' if st["dash"] else ""
        el.append(f'<polyline fill="none" stroke="{st["color"]}" stroke-width="{st["width"]}"{dash} points="{polyline_pts(xs, ys)}"/>')
    el.append("</g>")
    el.append(f'<rect x="{ax0}" y="{ay0}" width="{ax1 - ax0}" height="{ay1 - ay0}" fill="none" stroke="#334155" stroke-width="0.9"/>')
    el.append(f'<text x="33" y="{0.5 * (ay0 + ay1):.1f}" text-anchor="middle" fill="{TEXT_SEC}" font-family="{FONT_SANS}" font-size="9.5" transform="rotate(-90 33 {0.5 * (ay0 + ay1):.1f})">Radiated Bass (dB)</text>')

    # Delta summary strip at bottom of Panel A
    old_d = chains["old_spatial_fdtr"]["vocal_bass_preservation"]["bass_preservation_delta_db"]
    summary_a = f"OLD Vocal Δ: {old_d:+.2f} dB"
    if "new_studio_deep" in chains:
        new_d = chains["new_studio_deep"]["vocal_bass_preservation"]["bass_preservation_delta_db"]
        summary_a += f"   |   NEW Deep Δ: {new_d:+.2f} dB"
    el.append(f'<text x="{ax0}" y="{ay1 + 29:.1f}" fill="#38BDF8" font-family="{FONT_MONO}" font-size="9.5" font-weight="700">{esc(summary_a)}</text>')

    # Top-Right Panel B: Bass Kick Harmonic Spectrum (40 - 400 Hz)
    el += svg_panel(556, 64, 524, 268, "B. Bass Kick Radiated Harmonic Spectrum (40–400 Hz)", "Fundamentals (45/75/110 Hz) + Missing-Fundamental Harmonics (120–360 Hz)")
    bx0, bx1 = 610.0, 1062.0
    by0, by1 = 112.0, 296.0
    psd_max = max(max(chains[cid]["bass_kick_psychoacoustics"]["spectrum_40_400_db"]) for cid in show[:3])
    ymax_b = max(-5.0, math.ceil((psd_max + 11.0) / 10.0) * 10.0)
    ymin_b = ymax_b - 70.0

    # Shade harmonic virtual-pitch band 120-360 Hz
    hx0 = map_lin(120.0, 40.0, 400.0, bx0, bx1)
    hx1 = map_lin(360.0, 40.0, 400.0, bx0, bx1)
    el.append(f'<rect x="{hx0:.1f}" y="{by0:.1f}" width="{hx1 - hx0:.1f}" height="{by1 - by0:.1f}" fill="#38BDF8" fill-opacity="0.07"/>')
    el.append(f'<text x="{0.5 * (hx0 + hx1):.1f}" y="{by0 + 13:.1f}" text-anchor="middle" fill="#38BDF8" font-family="{FONT_SANS}" font-size="8.8" font-weight="600">Virtual-Pitch Harmonic Passband (120–360 Hz)</text>')

    for db in range(int(ymin_b), int(ymax_b) + 1, 10):
        y = map_lin(db, ymin_b, ymax_b, by1, by0)
        el.append(f'<line x1="{bx0:.1f}" y1="{y:.1f}" x2="{bx1:.1f}" y2="{y:.1f}" stroke="{GRID_MAJOR}" stroke-width="0.6"/>')
        el.append(f'<text x="{bx0 - 6:.1f}" y="{y + 3.5:.1f}" text-anchor="end" fill="{TEXT_SEC}" font-family="{FONT_MONO}" font-size="9">{db}</text>')
    for hz in (50, 75, 110, 150, 180, 220, 300, 360, 400):
        x = map_lin(hz, 40.0, 400.0, bx0, bx1)
        el.append(f'<line x1="{x:.1f}" y1="{by0:.1f}" x2="{x:.1f}" y2="{by1:.1f}" stroke="{GRID_MINOR}" stroke-width="0.6"/>')
        el.append(f'<text x="{x:.1f}" y="{by1 + 13:.1f}" text-anchor="middle" fill="{TEXT_SEC}" font-family="{FONT_MONO}" font-size="8.8">{hz}</text>')

    el.append(f'<defs><clipPath id="clipB"><rect x="{bx0}" y="{by0}" width="{bx1 - bx0}" height="{by1 - by0}"/></clipPath></defs>')
    el.append('<g clip-path="url(#clipB)">')
    for cid in show[:3]:
        st = CHAIN_STYLES[cid]
        bp = chains[cid]["bass_kick_psychoacoustics"]
        xs = [map_lin(f, 40.0, 400.0, bx0, bx1) for f in bp["spectrum_40_400_hz"]]
        ys = [map_lin(clamp(v, ymin_b - 2, ymax_b + 2), ymin_b, ymax_b, by1, by0) for v in bp["spectrum_40_400_db"]]
        dash = f' stroke-dasharray="{st["dash"]}"' if st["dash"] else ""
        el.append(f'<polyline fill="none" stroke="{st["color"]}" stroke-width="{st["width"] - 0.3}"{dash} points="{polyline_pts(xs, ys)}"/>')
    el.append("</g>")
    el.append(f'<rect x="{bx0}" y="{by0}" width="{bx1 - bx0}" height="{by1 - by0}" fill="none" stroke="#334155" stroke-width="0.9"/>')
    el.append(f'<text x="570" y="{0.5 * (by0 + by1):.1f}" text-anchor="middle" fill="{TEXT_SEC}" font-family="{FONT_SANS}" font-size="9.5" transform="rotate(-90 570 {0.5 * (by0 + by1):.1f})">Radiated PSD (dB)</text>')

    old_pbi = chains["old_spatial_fdtr"]["bass_kick_psychoacoustics"]["perceived_bass_index_db"]
    pbi_txt = f"Perceived Bass Index — OLD: {old_pbi:+.1f} dB"
    if "new_studio_deep" in chains:
        new_pbi = chains["new_studio_deep"]["bass_kick_psychoacoustics"]["perceived_bass_index_db"]
        pbi_txt += f"  |  NEW Deep: {new_pbi:+.1f} dB ({new_pbi - old_pbi:+.1f} dB)"
    el.append(f'<text x="{bx0}" y="{by1 + 29:.1f}" fill="#38BDF8" font-family="{FONT_MONO}" font-size="9.3" font-weight="700">{esc(pbi_txt)}</text>')

    # Bottom Panel C: Klippel Woofer Excursion Envelope vs X_max (0.45 mm)
    el += svg_panel(20, 344, 1060, 260, "C. Klippel 2nd-Order Micro-Woofer Mechanical Excursion (f0 = 145 Hz, Qm = 0.85)", "Time-domain diaphragm displacement on Deep Bass Kick vs X_max = 0.45 mm hardware safety ceiling")
    cx0, cx1 = 76.0, 1054.0
    cy0, cy1 = 392.0, 560.0
    xmax_mm = 0.45
    max_obs = max(chains[k]["klippel_excursion"]["peak_mm"] for k in show)
    ymax_c = max(0.60, math.ceil((max_obs + 0.08) * 10.0) / 10.0)

    # Red danger zone above X_max (background shading first)
    y_xmax = map_lin(xmax_mm, 0.0, ymax_c, cy1, cy0)
    el.append(f'<rect x="{cx0:.1f}" y="{cy0:.1f}" width="{cx1 - cx0:.1f}" height="{max(0.0, y_xmax - cy0):.1f}" fill="#F85149" fill-opacity="0.11"/>')

    step_c = 0.1 if ymax_c <= 0.85 else 0.2
    v = 0.0
    while v <= ymax_c + 1e-6:
        y = map_lin(v, 0.0, ymax_c, cy1, cy0)
        el.append(f'<line x1="{cx0:.1f}" y1="{y:.1f}" x2="{cx1:.1f}" y2="{y:.1f}" stroke="{GRID_MAJOR}" stroke-width="0.6"/>')
        el.append(f'<text x="{cx0 - 8:.1f}" y="{y + 3.5:.1f}" text-anchor="end" fill="{TEXT_SEC}" font-family="{FONT_MONO}" font-size="9">{v:.2f}</text>')
        v += step_c

    for ts in (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0):
        x = map_lin(ts, 0.0, 4.0, cx0, cx1)
        el.append(f'<line x1="{x:.1f}" y1="{cy0:.1f}" x2="{x:.1f}" y2="{cy1:.1f}" stroke="{GRID_MINOR}" stroke-width="0.6"/>')
        el.append(f'<text x="{x:.1f}" y="{cy1 + 14:.1f}" text-anchor="middle" fill="{TEXT_SEC}" font-family="{FONT_MONO}" font-size="9">{ts:.1f}s</text>')

    el.append(f'<defs><clipPath id="clipC"><rect x="{cx0}" y="{cy0}" width="{cx1 - cx0}" height="{cy1 - cy0}"/></clipPath></defs>')
    el.append('<g clip-path="url(#clipC)">')
    for cid in show:
        st = CHAIN_STYLES[cid]
        kl = chains[cid]["klippel_excursion"]
        xs = [map_lin(t, 0.0, 4.0, cx0, cx1) for t in kl["envelope_t_s"]]
        ys = [map_lin(clamp(val, 0.0, ymax_c), 0.0, ymax_c, cy1, cy0) for val in kl["envelope_mm"]]
        dash = f' stroke-dasharray="{st["dash"]}"' if st["dash"] else ""
        el.append(f'<polyline fill="none" stroke="{st["color"]}" stroke-width="{st["width"]}"{dash} points="{polyline_pts(xs, ys)}"/>')
    el.append("</g>")
    # Draw X_max safety ceiling line ON TOP of traces for unmistakable visibility
    el.append(f'<line x1="{cx0:.1f}" y1="{y_xmax:.1f}" x2="{cx1:.1f}" y2="{y_xmax:.1f}" stroke="#F85149" stroke-width="1.8" stroke-dasharray="6,4"/>')
    el.append(f'<rect x="{cx1 - 322:.1f}" y="{max(cy0 + 3, y_xmax - 18):.1f}" width="316" height="15" rx="3" fill="#180D10" fill-opacity="0.88"/>')
    el.append(f'<text x="{cx1 - 10:.1f}" y="{max(cy0 + 13, y_xmax - 7):.1f}" text-anchor="end" fill="#F85149" font-family="{FONT_MONO}" font-size="9.2" font-weight="700">X_max SAFETY LIMIT = 0.450 mm (DANGER ABOVE)</text>')
    el.append(f'<rect x="{cx0}" y="{cy0}" width="{cx1 - cx0}" height="{cy1 - cy0}" fill="none" stroke="#334155" stroke-width="0.9"/>')
    el.append(f'<text x="34" y="{0.5 * (cy0 + cy1):.1f}" text-anchor="middle" fill="{TEXT_SEC}" font-family="{FONT_SANS}" font-size="10" transform="rotate(-90 34 {0.5 * (cy0 + cy1):.1f})">Peak Excursion (mm)</text>')

    # Legend + Excursion Margin Strip along bottom of Panel C
    lx = cx0
    for cid in show:
        st = CHAIN_STYLES[cid]
        kl = chains[cid]["klippel_excursion"]
        lbl = f"{_chain_short_label(chains, cid)}: {kl['peak_mm']:.3f}mm ({kl['excursion_margin_db']:+.1f}dB)"
        dash = f' stroke-dasharray="{st["dash"]}"' if st["dash"] else ""
        el.append(f'<line x1="{lx:.1f}" y1="{cy1 + 31:.1f}" x2="{lx + 18:.1f}" y2="{cy1 + 31:.1f}" stroke="{st["color"]}" stroke-width="2.4"{dash}/>')
        el.append(f'<text x="{lx + 23:.1f}" y="{cy1 + 34:.1f}" fill="{TEXT_PRI}" font-family="{FONT_MONO}" font-size="9.2">{esc(lbl)}</text>')
        lx += 23 + len(lbl) * 5.7 + 20

    el.append("</svg>")
    return render_svg_to_png("\n".join(el), out_dir / "02_bass_and_body_comparison.svg", out_dir / "02_bass_and_body_comparison.png")


# ---------------------------------------------------------------------------
# Chart 03: Stereo Correlation r(f) & Side/Mid Spatial Width Profile
# ---------------------------------------------------------------------------
def build_chart_03(results: Dict[str, Any], out_dir: Path) -> Path:
    W, H = 1100, 620
    chains = results["chains"]
    all_v = results.get("all_binaries_verified", False)
    el = svg_header(
        W,
        H,
        "Stereo Phase Correlation r(f) & Nearfield Spatial Width Profile",
        "Verifying <180 Hz mono-bass anchor (>= +0.98 on uncorrelated noise) and clean Mid/Side stage depth across bands",
        all_v,
    )

    show = [k for k in ("stock_raw", "old_spatial_fdtr", "new_studio_deep", "new_studio_spatial", "new_studio_reference") if k in chains]

    # Shared Legend Bar
    lx = 26.0
    for cid in show:
        st = CHAIN_STYLES[cid]
        lbl = _chain_short_label(chains, cid)
        dash = f' stroke-dasharray="{st["dash"]}"' if st["dash"] else ""
        el.append(f'<line x1="{lx:.1f}" y1="73" x2="{lx + 22:.1f}" y2="73" stroke="{st["color"]}" stroke-width="{st["width"]}"{dash}/>')
        el.append(f'<text x="{lx + 28:.1f}" y="77" fill="{TEXT_PRI}" font-family="{FONT_SANS}" font-size="11" font-weight="600">{esc(lbl)}</text>')
        lx += 28 + len(lbl) * 7.1 + 22

    # Left Panel A: r(f) across 31.5 Hz - 16 kHz (Uncorrelated Pink Noise)
    el += svg_panel(20, 90, 560, 510, "A. Stereo Correlation r(f) — Uncorrelated Pink Noise", "Stress test: input r = 0.00 everywhere; DSP must lock <180 Hz >= +0.98")
    ax0, ax1 = 76.0, 558.0
    ay0, ay1 = 142.0, 534.0
    fmin, fmax = 31.5, 16000.0
    ymin, ymax = -0.35, 1.08

    # Shade <180 Hz Mono Anchor Zone
    x180 = map_log(180.0, fmin, fmax, ax0, ax1)
    el.append(f'<rect x="{ax0:.1f}" y="{ay0:.1f}" width="{x180 - ax0:.1f}" height="{ay1 - ay0:.1f}" fill="#10B981" fill-opacity="0.09"/>')
    el.append(f'<line x1="{x180:.1f}" y1="{ay0:.1f}" x2="{x180:.1f}" y2="{ay1:.1f}" stroke="#10B981" stroke-width="1.2" stroke-dasharray="4,3"/>')
    el.append(f'<text x="{0.5 * (ax0 + x180):.1f}" y="{ay0 + 15:.1f}" text-anchor="middle" fill="#34D399" font-family="{FONT_SANS}" font-size="9.5" font-weight="700">MONO ANCHOR &lt;180 Hz</text>')

    # Horizontal grid + +0.98 PASS threshold
    for r_val in (-0.2, 0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
        y = map_lin(r_val, ymin, ymax, ay1, ay0)
        col = "#475569" if r_val == 0.0 else GRID_MAJOR
        el.append(f'<line x1="{ax0:.1f}" y1="{y:.1f}" x2="{ax1:.1f}" y2="{y:.1f}" stroke="{col}" stroke-width="0.8"/>')
        el.append(f'<text x="{ax0 - 8:.1f}" y="{y + 3.5:.1f}" text-anchor="end" fill="{TEXT_SEC}" font-family="{FONT_MONO}" font-size="9.5">{r_val:+.1f}</text>')

    y_pass = map_lin(0.98, ymin, ymax, ay1, ay0)
    el.append(f'<line x1="{ax0:.1f}" y1="{y_pass:.1f}" x2="{x180:.1f}" y2="{y_pass:.1f}" stroke="#34D399" stroke-width="1.6" stroke-dasharray="5,3"/>')
    el.append(f'<text x="{x180 + 18:.1f}" y="{y_pass + 3.5:.1f}" fill="#34D399" font-family="{FONT_MONO}" font-size="9" font-weight="700">PASS &gt;= +0.98</text>')

    for hz, lbl in ((31.5, "31.5"), (63, "63"), (125, "125"), (180, "180"), (250, "250"), (500, "500"), (1000, "1k"), (2000, "2k"), (4000, "4k"), (8000, "8k"), (16000, "16k")):
        x = map_log(hz, fmin, fmax, ax0, ax1)
        el.append(f'<line x1="{x:.1f}" y1="{ay0:.1f}" x2="{x:.1f}" y2="{ay1:.1f}" stroke="{GRID_MINOR}" stroke-width="0.7"/>')
        el.append(f'<text x="{x:.1f}" y="{ay1 + 15:.1f}" text-anchor="middle" fill="{TEXT_SEC}" font-family="{FONT_MONO}" font-size="9">{lbl}</text>')

    el.append(f'<defs><clipPath id="clipR"><rect x="{ax0}" y="{ay0}" width="{ax1 - ax0}" height="{ay1 - ay0}"/></clipPath></defs>')
    el.append('<g clip-path="url(#clipR)">')
    for cid in show:
        st = CHAIN_STYLES[cid]
        sc = chains[cid]["stereo_uncorrelated"]
        xs = [map_log(f, fmin, fmax, ax0, ax1) for f in sc["third_octave_hz"]]
        ys = [map_lin(clamp(v, ymin, ymax), ymin, ymax, ay1, ay0) for v in sc["third_octave_r"]]
        dash = f' stroke-dasharray="{st["dash"]}"' if st["dash"] else ""
        el.append(f'<polyline fill="none" stroke="{st["color"]}" stroke-width="{st["width"]}"{dash} points="{polyline_pts(xs, ys)}"/>')
    el.append("</g>")
    el.append(f'<rect x="{ax0}" y="{ay0}" width="{ax1 - ax0}" height="{ay1 - ay0}" fill="none" stroke="#334155" stroke-width="0.9"/>')
    el.append(f'<text x="{0.5 * (ax0 + ax1):.1f}" y="{ay1 + 36:.1f}" text-anchor="middle" fill="{TEXT_SEC}" font-family="{FONT_SANS}" font-size="11" font-weight="600">1/3-Octave Band Center Frequency (Hz)</text>')
    el.append(f'<text x="34" y="{0.5 * (ay0 + ay1):.1f}" text-anchor="middle" fill="{TEXT_SEC}" font-family="{FONT_SANS}" font-size="11" font-weight="600" transform="rotate(-90 34 {0.5 * (ay0 + ay1):.1f})">Inter-Channel Correlation r(f)</text>')

    # Gate Table Inset in Panel A (top-right area where curves sit near 0)
    gx, gy, gw = 328, 166, 220
    gh = 24 + len(show) * 18
    el.append(f'<rect x="{gx}" y="{gy}" width="{gw}" height="{gh}" rx="6" fill="#0B111A" fill-opacity="0.92" stroke="#334155" stroke-width="1"/>')
    el.append(f'<text x="{gx + 10}" y="{gy + 16}" fill="{TEXT_PRI}" font-family="{FONT_MONO}" font-size="9.2" font-weight="700">&lt;180 Hz MONO ANCHOR GATE</text>')
    for idx, cid in enumerate(show):
        sc = chains[cid]["stereo_uncorrelated"]
        rv = sc["bands"]["sub180_hz"]["correlation_r"]
        ok = sc["sub180_mono_anchor_pass"]
        pcol = "#34D399" if ok else "#F85149"
        row_y = gy + 34 + idx * 18
        el.append(f'<text x="{gx + 10}" y="{row_y}" fill="{CHAIN_STYLES[cid]["color"]}" font-family="{FONT_SANS}" font-size="9.5" font-weight="600">{esc(CHAIN_STYLES[cid]["short"])}</text>')
        el.append(f'<text x="{gx + gw - 10}" y="{row_y}" text-anchor="end" fill="{pcol}" font-family="{FONT_MONO}" font-size="9.5" font-weight="700">{rv:+.3f} {"PASS" if ok else "FAIL"}</text>')

    # Right Panel B: Band-by-Band Spatial Width (Side/Mid Ratio dB on Studio Stereo Mix)
    el += svg_panel(596, 90, 484, 510, "B. Band-by-Band Spatial Width (Side / Mid Ratio dB)", "Studio stereo mix (mono <120 Hz, r=+0.45 above); higher = wider stage")
    bx0, bx1 = 654.0, 1058.0
    by0, by1 = 142.0, 534.0
    ymin_b, ymax_b = -36.0, 6.0

    for db in range(-36, 8, 6):
        y = map_lin(db, ymin_b, ymax_b, by1, by0)
        col = "#475569" if db == 0 else GRID_MAJOR
        el.append(f'<line x1="{bx0:.1f}" y1="{y:.1f}" x2="{bx1:.1f}" y2="{y:.1f}" stroke="{col}" stroke-width="0.8"/>')
        el.append(f'<text x="{bx0 - 8:.1f}" y="{y + 3.5:.1f}" text-anchor="end" fill="{TEXT_SEC}" font-family="{FONT_MONO}" font-size="9.5">{db:+d}</text>')

    band_keys = [
        ("sub180_hz", "<180 Hz\n(Bass)"),
        ("180_500_hz", "180–500 Hz\n(Body)"),
        ("500_4000_hz", "0.5–4 kHz\n(Stage)"),
        ("4000_16000_hz", "4–16 kHz\n(Air)"),
    ]
    bar_chains = [k for k in ("stock_raw", "old_spatial_fdtr", "new_studio_deep", "new_studio_spatial") if k in chains]
    n_groups = len(band_keys)
    n_bars = len(bar_chains)
    group_w = (bx1 - bx0) / n_groups
    bar_w = min(20.0, (group_w - 22.0) / max(n_bars, 1))
    y_base = map_lin(ymin_b, ymin_b, ymax_b, by1, by0)

    for gi, (bk, blbl) in enumerate(band_keys):
        gx_c = bx0 + (gi + 0.5) * group_w
        tot_w = n_bars * bar_w + (n_bars - 1) * 3.0
        start_x = gx_c - 0.5 * tot_w
        for bi, cid in enumerate(bar_chains):
            val = clamp(chains[cid]["stereo_studio"]["bands"][bk]["side_mid_ratio_db"], ymin_b, ymax_b)
            y_top = map_lin(val, ymin_b, ymax_b, by1, by0)
            x_bar = start_x + bi * (bar_w + 3.0)
            col = CHAIN_STYLES[cid]["color"]
            h_bar = max(2.0, y_base - y_top)
            el.append(f'<rect x="{x_bar:.1f}" y="{y_top:.1f}" width="{bar_w:.1f}" height="{h_bar:.1f}" rx="2" fill="{col}" fill-opacity="0.88"/>')
            # Value text above bar
            el.append(f'<text x="{x_bar + bar_w / 2:.1f}" y="{max(by0 + 10, y_top - 4):.1f}" text-anchor="middle" fill="{TEXT_PRI}" font-family="{FONT_MONO}" font-size="8">{val:+.0f}</text>')
        lbl_parts = blbl.split("\n")
        el.append(f'<text x="{gx_c:.1f}" y="{by1 + 16:.1f}" text-anchor="middle" fill="{TEXT_PRI}" font-family="{FONT_SANS}" font-size="10" font-weight="600">{esc(lbl_parts[0])}</text>')
        el.append(f'<text x="{gx_c:.1f}" y="{by1 + 29:.1f}" text-anchor="middle" fill="{TEXT_MUTED}" font-family="{FONT_SANS}" font-size="9">{esc(lbl_parts[1])}</text>')

    el.append(f'<rect x="{bx0}" y="{by0}" width="{bx1 - bx0}" height="{by1 - by0}" fill="none" stroke="#334155" stroke-width="0.9"/>')
    el.append(f'<text x="612" y="{0.5 * (by0 + by1):.1f}" text-anchor="middle" fill="{TEXT_SEC}" font-family="{FONT_SANS}" font-size="11" font-weight="600" transform="rotate(-90 612 {0.5 * (by0 + by1):.1f})">Side / Mid Energy Ratio (dB)</text>')
    el.append(f'<text x="{bx0}" y="{by1 + 48:.1f}" fill="{TEXT_MUTED}" font-family="{FONT_SANS}" font-size="9.2">Lower Side/Mid &lt;180 Hz = tighter mono bass; higher 0.5–4 kHz = wider stage.</text>')

    el.append("</svg>")
    return render_svg_to_png("\n".join(el), out_dir / "03_stereo_correlation_and_depth.svg", out_dir / "03_stereo_correlation_and_depth.png")


# ---------------------------------------------------------------------------
# Chart 04: Mastering Dynamics, Loudness, Distortion & E-Core CPU %
# ---------------------------------------------------------------------------
def build_chart_04(results: Dict[str, Any], out_dir: Path) -> Path:
    W, H = 1100, 620
    chains = results["chains"]
    all_v = results.get("all_binaries_verified", False)
    el = svg_header(
        W,
        H,
        "Mastering Loudness, Dynamic Range, Distortion & E-Core CPU Telemetry",
        "ITU-R BS.1770-4 LUFS · 4× True Peak (dBTP) · PLR Dynamic Range · THD+N / SMPTE IMD · Alder Lake E-Core Real-Time Budget",
        all_v,
    )

    show = [k for k in ("stock_raw", "old_spatial_fdtr", "new_studio_deep", "new_studio_spatial", "new_studio_reference") if k in chains]
    short_names = [CHAIN_STYLES[k]["short"].replace("NEW ", "") for k in show]

    # --- Q1 Top-Left: Integrated LUFS & 4x True Peak (dBTP) ---
    el += svg_panel(20, 64, 520, 264, "A. Loudness (BS.1770-4 LUFS) & 4× True Peak (dBTP)", "Deep Bass Kick mastering drive vs -1.0 dBTP brickwall ceiling")
    x0, x1, y0, y1 = 68.0, 522.0, 114.0, 286.0
    ymin, ymax = -20.0, 4.0

    for db in (-20, -15, -10, -5, -1, 0):
        y = map_lin(db, ymin, ymax, y1, y0)
        is_ceil = db == -1
        col = "#F85149" if is_ceil else GRID_MAJOR
        dash = ' stroke-dasharray="5,3"' if is_ceil else ""
        el.append(f'<line x1="{x0:.1f}" y1="{y:.1f}" x2="{x1:.1f}" y2="{y:.1f}" stroke="{col}" stroke-width="{1.4 if is_ceil else 0.7}"{dash}/>')
        if db != -1:
            el.append(f'<text x="{x0 - 6:.1f}" y="{y + 3.5:.1f}" text-anchor="end" fill="{TEXT_SEC}" font-family="{FONT_MONO}" font-size="9">{db}</text>')
    y_ceil = map_lin(-1.0, ymin, ymax, y1, y0)
    el.append(f'<text x="{x0 + 6:.1f}" y="{y0 + 12:.1f}" fill="#F85149" font-family="{FONT_MONO}" font-size="8.6" font-weight="700">---  -1.0 dBTP BRICKWALL CEILING</text>')

    gw = (x1 - x0) / len(show)
    bw = min(24.0, (gw - 18.0) / 2.0)
    for i, cid in enumerate(show):
        dy = chains[cid]["bass_kick_dynamics"]
        lufs = clamp(dy["integrated_lufs"], ymin, ymax)
        tp = clamp(dy["true_peak_dbtp"], ymin, ymax)
        gx = x0 + (i + 0.5) * gw
        # Bar 1: LUFS (cyan-slate)
        y_l = map_lin(lufs, ymin, ymax, y1, y0)
        el.append(f'<rect x="{gx - bw - 2:.1f}" y="{y_l:.1f}" width="{bw:.1f}" height="{max(2.0, y1 - y_l):.1f}" rx="2" fill="#38BDF8" fill-opacity="0.88"/>')
        el.append(f'<text x="{gx - bw / 2 - 2:.1f}" y="{y_l - 4:.1f}" text-anchor="middle" fill="#38BDF8" font-family="{FONT_MONO}" font-size="8.2">{lufs:.1f}</text>')
        # Bar 2: True Peak (amber/green depending on pass)
        tp_col = "#34D399" if dy["true_peak_pass"] else "#F85149"
        y_t = map_lin(tp, ymin, ymax, y1, y0)
        el.append(f'<rect x="{gx + 2:.1f}" y="{y_t:.1f}" width="{bw:.1f}" height="{max(2.0, y1 - y_t):.1f}" rx="2" fill="{tp_col}" fill-opacity="0.88"/>')
        el.append(f'<text x="{gx + bw / 2 + 2:.1f}" y="{max(y0 + 9, y_t - 4):.1f}" text-anchor="middle" fill="{tp_col}" font-family="{FONT_MONO}" font-size="8.2">{tp:.1f}</text>')
        el.append(f'<text x="{gx:.1f}" y="{y1 + 14:.1f}" text-anchor="middle" fill="{TEXT_PRI}" font-family="{FONT_SANS}" font-size="9" font-weight="600">{esc(short_names[i])}</text>')

    el.append(f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" fill="none" stroke="#334155" stroke-width="0.9"/>')
    el.append(f'<rect x="{x0}" y="{y1 + 22}" width="10" height="8" rx="2" fill="#38BDF8"/><text x="{x0 + 14}" y="{y1 + 29}" fill="{TEXT_SEC}" font-family="{FONT_SANS}" font-size="9">Integrated LUFS</text>')
    el.append(f'<rect x="{x0 + 115}" y="{y1 + 22}" width="10" height="8" rx="2" fill="#34D399"/><text x="{x0 + 129}" y="{y1 + 29}" fill="{TEXT_SEC}" font-family="{FONT_SANS}" font-size="9">4× True Peak dBTP (&lt;= -1.0 PASS)</text>')

    # --- Q2 Top-Right: PLR Dynamic Range & Crest Factor ---
    el += svg_panel(556, 64, 524, 264, "B. Dynamic Range (PLR) & Crest Factor (dB)", "Higher PLR preserves musical punch instead of limiter pumping")
    x0, x1, y0, y1 = 604.0, 1062.0, 114.0, 286.0
    max_dyn = max(max(chains[k]["bass_kick_dynamics"]["plr_dynamic_range_db"], chains[k]["bass_kick_dynamics"]["crest_factor_db"]) for k in show)
    ymax_d = max(16.0, math.ceil((max_dyn + 2.0) / 2.0) * 2.0)

    for v in range(0, int(ymax_d) + 1, 4):
        y = map_lin(v, 0.0, ymax_d, y1, y0)
        el.append(f'<line x1="{x0:.1f}" y1="{y:.1f}" x2="{x1:.1f}" y2="{y:.1f}" stroke="{GRID_MAJOR}" stroke-width="0.7"/>')
        el.append(f'<text x="{x0 - 6:.1f}" y="{y + 3.5:.1f}" text-anchor="end" fill="{TEXT_SEC}" font-family="{FONT_MONO}" font-size="9">{v}</text>')

    gw = (x1 - x0) / len(show)
    bw = min(24.0, (gw - 18.0) / 2.0)
    for i, cid in enumerate(show):
        dy = chains[cid]["bass_kick_dynamics"]
        plr = dy["plr_dynamic_range_db"]
        cf = dy["crest_factor_db"]
        gx = x0 + (i + 0.5) * gw
        y_p = map_lin(plr, 0.0, ymax_d, y1, y0)
        el.append(f'<rect x="{gx - bw - 2:.1f}" y="{y_p:.1f}" width="{bw:.1f}" height="{max(2.0, y1 - y_p):.1f}" rx="2" fill="#A78BFA" fill-opacity="0.88"/>')
        el.append(f'<text x="{gx - bw / 2 - 2:.1f}" y="{y_p - 4:.1f}" text-anchor="middle" fill="#A78BFA" font-family="{FONT_MONO}" font-size="8.2">{plr:.1f}</text>')
        y_c = map_lin(cf, 0.0, ymax_d, y1, y0)
        el.append(f'<rect x="{gx + 2:.1f}" y="{y_c:.1f}" width="{bw:.1f}" height="{max(2.0, y1 - y_c):.1f}" rx="2" fill="#FBBF24" fill-opacity="0.88"/>')
        el.append(f'<text x="{gx + bw / 2 + 2:.1f}" y="{y_c - 4:.1f}" text-anchor="middle" fill="#FBBF24" font-family="{FONT_MONO}" font-size="8.2">{cf:.1f}</text>')
        el.append(f'<text x="{gx:.1f}" y="{y1 + 14:.1f}" text-anchor="middle" fill="{TEXT_PRI}" font-family="{FONT_SANS}" font-size="9" font-weight="600">{esc(short_names[i])}</text>')

    el.append(f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" fill="none" stroke="#334155" stroke-width="0.9"/>')
    el.append(f'<rect x="{x0}" y="{y1 + 22}" width="10" height="8" rx="2" fill="#A78BFA"/><text x="{x0 + 14}" y="{y1 + 29}" fill="{TEXT_SEC}" font-family="{FONT_SANS}" font-size="9">PLR (TruePeak - LUFS, dB)</text>')
    el.append(f'<rect x="{x0 + 165}" y="{y1 + 22}" width="10" height="8" rx="2" fill="#FBBF24"/><text x="{x0 + 179}" y="{y1 + 29}" fill="{TEXT_SEC}" font-family="{FONT_SANS}" font-size="9">Crest Factor (Peak/RMS, dB)</text>')

    # --- Q3 Bottom-Left: Distortion Telemetry (THD+N @ 200Hz, 1kHz & SMPTE IMD %) ---
    el += svg_panel(20, 342, 520, 262, "C. Nonlinear Distortion: THD+N & SMPTE IMD (%)", "Lower is cleaner; log scale 0.001% – 20% (60 Hz + 7 kHz 4:1 IMD)")
    x0, x1, y0, y1 = 72.0, 522.0, 392.0, 562.0
    dmin, dmax = 0.001, 20.0

    for tick, tlbl in ((0.001, "0.001%"), (0.01, "0.01%"), (0.1, "0.1%"), (1.0, "1.0%"), (10.0, "10%")):
        y = map_log(tick, dmin, dmax, y1, y0)
        el.append(f'<line x1="{x0:.1f}" y1="{y:.1f}" x2="{x1:.1f}" y2="{y:.1f}" stroke="{GRID_MAJOR}" stroke-width="0.7"/>')
        el.append(f'<text x="{x0 - 6:.1f}" y="{y + 3.5:.1f}" text-anchor="end" fill="{TEXT_SEC}" font-family="{FONT_MONO}" font-size="8.5">{tlbl}</text>')

    gw = (x1 - x0) / len(show)
    bw = min(17.0, (gw - 16.0) / 3.0)
    dist_cols = ("#38BDF8", "#34D399", "#F85149")
    for i, cid in enumerate(show):
        d = chains[cid]["distortion"]
        vals = (
            d["thd_n_200hz"]["thdn_percent"],
            d["thd_n_1khz"]["thdn_percent"],
            d["smpte_imd_60hz_7khz"]["imd_percent"],
        )
        gx = x0 + (i + 0.5) * gw
        start_x = gx - 1.5 * bw - 2.0
        for bi, (val, col) in enumerate(zip(vals, dist_cols)):
            vc = clamp(val, dmin, dmax)
            y_v = map_log(vc, dmin, dmax, y1, y0)
            xb = start_x + bi * (bw + 2.0)
            el.append(f'<rect x="{xb:.1f}" y="{y_v:.1f}" width="{bw:.1f}" height="{max(2.0, y1 - y_v):.1f}" rx="2" fill="{col}" fill-opacity="0.88"/>')
            vtxt = f"{val:.2f}" if val >= 0.05 else f"{val:.3f}"
            if val < 0.002:
                vtxt = "~0"
            el.append(f'<text x="{xb + bw / 2:.1f}" y="{max(y0 + 8, y_v - 3):.1f}" text-anchor="middle" fill="{col}" font-family="{FONT_MONO}" font-size="7.4">{esc(vtxt)}</text>')
        el.append(f'<text x="{gx:.1f}" y="{y1 + 14:.1f}" text-anchor="middle" fill="{TEXT_PRI}" font-family="{FONT_SANS}" font-size="9" font-weight="600">{esc(short_names[i])}</text>')

    el.append(f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" fill="none" stroke="#334155" stroke-width="0.9"/>')
    el.append(f'<rect x="{x0}" y="{y1 + 22}" width="9" height="8" rx="2" fill="#38BDF8"/><text x="{x0 + 13}" y="{y1 + 29}" fill="{TEXT_SEC}" font-family="{FONT_SANS}" font-size="8.8">THD+N 200Hz</text>')
    el.append(f'<rect x="{x0 + 96}" y="{y1 + 22}" width="9" height="8" rx="2" fill="#34D399"/><text x="{x0 + 109}" y="{y1 + 29}" fill="{TEXT_SEC}" font-family="{FONT_SANS}" font-size="8.8">THD+N 1kHz</text>')
    el.append(f'<rect x="{x0 + 186}" y="{y1 + 22}" width="9" height="8" rx="2" fill="#F85149"/><text x="{x0 + 199}" y="{y1 + 29}" fill="{TEXT_SEC}" font-family="{FONT_SANS}" font-size="8.8">SMPTE IMD (60Hz+7kHz)</text>')

    # --- Q4 Bottom-Right: E-Core Real-Time CPU % vs 1.50% Budget ---
    el += svg_panel(556, 342, 524, 262, "D. Real-Time E-Core CPU % (CPUs 12–19, 48 kHz / 256q)", "10-second native LADSPA run() benchmark vs 1.50% budget ceiling")
    x0, x1, y0, y1 = 716.0, 1054.0, 392.0, 562.0
    cpu = results["cpu_benchmarks"]
    old_cpu = cpu["old_spatial_fdtr"]
    new_cpu = cpu["new_studio_dsp"]

    rows: List[Tuple[str, Optional[float], str, str]] = [
        ("OLD LADSPA (bass+warm)", old_cpu["stereo_ladspa_nodes_sum_cpu_percent"], "#FB7185", "BINARY"),
        ("OLD Fused 2×2 FDTR", old_cpu["fused_fdtr_single_plugin_cpu_percent"], "#F85149", "BINARY"),
    ]
    if new_cpu["binary_verified"]:
        for m, lbl, col in (
            ("deep", "NEW Studio Deep", "#38BDF8"),
            ("spatial", "NEW Studio Spatial", "#A78BFA"),
            ("reference", "NEW Studio Reference", "#34D399"),
        ):
            if m in new_cpu["modes"]:
                rows.append((lbl, new_cpu["modes"][m]["cpu_percent"], col, "BINARY"))
    else:
        rows.append(("NEW Studio Deep", None, "#38BDF8", "PENDING"))
        rows.append(("NEW Studio Spatial", None, "#A78BFA", "PENDING"))
        rows.append(("NEW Studio Reference", None, "#34D399", "PENDING"))

    max_cpu = max([1.8] + [r[1] for r in rows if r[1] is not None])
    xmax_cpu = math.ceil((max_cpu + 0.25) * 2.0) / 2.0

    # Grid + 1.50% budget line
    v = 0.0
    while v <= xmax_cpu + 1e-6:
        x = map_lin(v, 0.0, xmax_cpu, x0, x1)
        el.append(f'<line x1="{x:.1f}" y1="{y0:.1f}" x2="{x:.1f}" y2="{y1:.1f}" stroke="{GRID_MAJOR}" stroke-width="0.7"/>')
        el.append(f'<text x="{x:.1f}" y="{y1 + 14:.1f}" text-anchor="middle" fill="{TEXT_SEC}" font-family="{FONT_MONO}" font-size="8.8">{v:.1f}%</text>')
        v += 0.5

    x_bud = map_lin(1.5, 0.0, xmax_cpu, x0, x1)
    el.append(f'<line x1="{x_bud:.1f}" y1="{y0:.1f}" x2="{x_bud:.1f}" y2="{y1:.1f}" stroke="#10B981" stroke-width="1.6" stroke-dasharray="5,3"/>')
    el.append(f'<text x="{x_bud + 4:.1f}" y="{y0 + 11:.1f}" fill="#34D399" font-family="{FONT_MONO}" font-size="8.6" font-weight="700">1.50% BUDGET</text>')

    row_h = (y1 - y0 - 14.0) / len(rows)
    bar_h = min(20.0, row_h - 8.0)
    for idx, (rlbl, val, col, tag) in enumerate(rows):
        ry = y0 + 12.0 + idx * row_h
        el.append(f'<text x="{x0 - 8:.1f}" y="{ry + bar_h * 0.72:.1f}" text-anchor="end" fill="{TEXT_PRI}" font-family="{FONT_SANS}" font-size="9.6" font-weight="600">{esc(rlbl)}</text>')
        if val is not None:
            bw_c = max(3.0, map_lin(val, 0.0, xmax_cpu, x0, x1) - x0)
            el.append(f'<rect x="{x0:.1f}" y="{ry:.1f}" width="{bw_c:.1f}" height="{bar_h:.1f}" rx="3" fill="{col}" fill-opacity="0.90"/>')
            el.append(f'<text x="{x0 + bw_c + 6:.1f}" y="{ry + bar_h * 0.72:.1f}" fill="{TEXT_PRI}" font-family="{FONT_MONO}" font-size="9" font-weight="700">{val:.3f}%</text>')
        else:
            bw_p = map_lin(1.5, 0.0, xmax_cpu, x0, x1) - x0
            el.append(f'<rect x="{x0:.1f}" y="{ry:.1f}" width="{bw_p:.1f}" height="{bar_h:.1f}" rx="3" fill="{col}" fill-opacity="0.12" stroke="{col}" stroke-width="1" stroke-dasharray="4,3"/>')
            el.append(f'<text x="{x0 + bw_p + 8:.1f}" y="{ry + bar_h * 0.72:.1f}" fill="#FBBF24" font-family="{FONT_MONO}" font-size="8.4">PENDING .so (&lt;=1.50% budget)</text>')

    el.append(f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" fill="none" stroke="#334155" stroke-width="0.9"/>')
    el.append(f'<text x="{x0}" y="{y1 + 29:.1f}" fill="{TEXT_MUTED}" font-family="{FONT_SANS}" font-size="8.8">Measured via ctypes LadspaHost pinned to E-cores (taskset -c 12-19 nice -n 19).</text>')

    el.append("</svg>")
    return render_svg_to_png("\n".join(el), out_dir / "04_dynamics_loudness_thd.svg", out_dir / "04_dynamics_loudness_thd.png")


def generate_all_charts(results: Dict[str, Any], out_dir: Path) -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    return [
        build_chart_01(results, out_dir),
        build_chart_02(results, out_dir),
        build_chart_03(results, out_dir),
        build_chart_04(results, out_dir),
    ]

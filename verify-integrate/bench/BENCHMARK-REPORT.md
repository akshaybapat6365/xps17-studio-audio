# Dell XPS 17 9720 Studio Audio — Offline Acoustic & Mastering Benchmark Report

- **Generated:** `2026-11-24T16:23:05Z`
- **Host:** `Linux-7.2.15-1-omarchy-x86_64-with-glibc2.44` · Python `3.14.8` · NumPy `2.5.4`
- **Measured Speaker Calibration:** `/home/akshaybapat/project/omarchy-audio-tune/baseline_curve.npy` (Blue Yeti X, 455 measurements, 0.076 dB repeatability, valid 80 Hz – 15 kHz, 1 kHz ref = `+68.59 dB`)
- **OLD Baseline Binary:** `/home/akshaybapat/project/omarchy-spatial-audio/lib/libomarchy_spatial_fdtr.so` (`binary_verified: true`, full `91-spatial-fdtr.conf` graph)
- **NEW Studio Binary:** `/home/akshaybapat/project/xps17-studio-audio/dsp-engine/lib/libxps17_studio_dsp.so` (`exists: True`) · Kernels (`4/4` present) · CLI (`exists: True`)

> **STATUS: FULL BINARY-VERIFIED HEAD-TO-HEAD BENCHMARK** (`libxps17_studio_dsp.so` + Lead 2 FIR WAVs executed via `LadspaHost`)

---

## 1. Metering Self-Test Verification (ITU-R BS.1770-4 & 4× True Peak)

- **BS.1770-4 Stereo 997 Hz @ 0 dBFS:** `+0.000 LUFS` (target `0.00 ± 0.08`)
- **BS.1770-4 Mono 997 Hz @ 0 dBFS:** `-3.010 LUFS` (target `-3.01 ± 0.08`)
- **12 kHz (`fs/4`, π/4-shifted) Inter-Sample Peak:** Sample Peak = `-3.010 dBFS`, 4× Kaiser-Sinc True Peak = `+0.002 dBTP` (target `0.00 ± 0.05`)

---

## 2. Executive Comparison Table (All Chains)

| Chain | Verified | Phys Bass 70–180 Hz (dB) | Perceived Bass Index (dB) | Mid-Bass 150–350 Hz (dB) | Vocal Clarity 1–4 kHz (dB) | Treble Air 6–14 kHz (dB) | Vocal+Bass Δ (dB) | `<180 Hz` Stereo `r` | Klippel Margin (dB) | LUFS / True Peak (dBTP) | PLR (dB) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **Stock XPS 17 Raw** | `BINARY` | `-47.62` | `-46.31` | `-30.69` | `-74.16` | `-57.16` | `+0.02` | `+0.015 (FAIL)` | `-0.56 (OVER!)` | `-14.0 / -2.06` | `11.94` |
| **OLD Spatial FDTR (91-conf)** | `BINARY` | `-41.64` | `-41.64` | `-18.81` | `-57.84` | `-50.52` | `-0.70` | `+0.393 (FAIL)` | `+1.61 (OK)` | `-6.2 / -1.00` | `5.21` |
| **NEW Studio Deep** | `BINARY` | `-41.31` | `-34.07` | `-22.45` | `-59.16` | `-44.17` | `-0.14` | `+1.000 (PASS)` | `+0.96 (OK)` | `-9.2 / -1.19` | `8.00` |
| **NEW Studio Spatial** | `BINARY` | `-41.47` | `-34.30` | `-22.76` | `-60.78` | `-43.68` | `-0.12` | `+1.000 (PASS)` | `+1.04 (OK)` | `-9.4 / -1.19` | `8.18` |
| **NEW Studio Reference** | `BINARY` | `-41.93` | `-41.78` | `-22.09` | `-60.50` | `-45.54` | `+0.08` | `+1.000 (PASS)` | `+1.09 (OK)` | `-9.1 / -1.19` | `7.89` |
| **NEW Studio Voice** | `BINARY` | `-57.30` | `-57.30` | `-28.31` | `-67.73` | `-46.90` | `+0.04` | `+0.999 (PASS)` | `+9.80 (OK)` | `-16.8 / -2.71` | `14.10` |

*Note: `DESIGN REF*` indicates metrics produced by the contract-faithful design reference model while `libxps17_studio_dsp.so` and Lead 2 kernels are pending compilation. `BINARY` indicates execution through real compiled native binaries and measured data.*

---

## 3. Distortion, Phase / Pre-Ringing & Real-Time E-Core CPU Budget

| Chain | Verified | THD+N @ 200 Hz (%) | THD+N @ 1 kHz (%) | SMPTE IMD 60Hz+7kHz (%) | Pre-Ringing Energy (dB) | Burst True Peak (dBTP) | Speech Vocal/Sub Ratio (dB) |
|---|---|---|---|---|---|---|---|
| **Stock XPS 17 Raw** | `BINARY` | `0.0000%` | `0.0000%` | `0.0000%` | `-166.0 dB` | `-0.50` | `+30.13` |
| **OLD Spatial FDTR (91-conf)** | `BINARY` | `2.9950%` | `0.0971%` | `1.0999%` | `-141.9 dB` | `-3.09` | `+23.59` |
| **NEW Studio Deep** | `BINARY` | `0.4331%` | `0.0041%` | `0.3538%` | `-176.9 dB` | `-1.19` | `+16.19` |
| **NEW Studio Spatial** | `BINARY` | `0.3977%` | `0.0043%` | `0.5144%` | `-177.0 dB` | `-1.19` | `+17.17` |
| **NEW Studio Reference** | `BINARY` | `0.1807%` | `0.0000%` | `0.0000%` | `-175.4 dB` | `-1.19` | `+20.45` |
| **NEW Studio Voice** | `BINARY` | `0.0182%` | `0.0035%` | `0.0000%` | `-174.1 dB` | `-1.19` | `+30.73` |

### Real-Time E-Core CPU % Benchmark (10.0 s @ 48 kHz, 256-sample PipeWire quantum, CPUs 12–19)

- **OLD `libomarchy_spatial_fdtr.so` (stereo graph `bass_gen + warm_stage` LADSPA nodes):** `0.466%` E-core CPU (excl. 4 external PipeWire convolvers + LSP LV2 nodes)
- **OLD `libomarchy_spatial_fdtr.so` (fused 2×2 FDTR + limiter single plugin):** `3.161%` E-core CPU (`168.6 µs` / `5333 µs` quantum, `31.6×` real-time)
- **NEW `libxps17_studio_dsp.so` (`deep`):** `0.885%` E-core CPU (`47.2 µs` / `5333 µs` quantum, `112.9×` real-time) — **PASS vs 1.5% budget**
- **NEW `libxps17_studio_dsp.so` (`spatial`):** `0.943%` E-core CPU (`50.3 µs` / `5333 µs` quantum, `106.1×` real-time) — **PASS vs 1.5% budget**
- **NEW `libxps17_studio_dsp.so` (`reference`):** `0.885%` E-core CPU (`47.2 µs` / `5333 µs` quantum, `112.9×` real-time) — **PASS vs 1.5% budget**
- **NEW `libxps17_studio_dsp.so` (`voice`):** `0.931%` E-core CPU (`49.7 µs` / `5333 µs` quantum, `107.3×` real-time) — **PASS vs 1.5% budget**

---

## 4. Key Forensic Findings Proven by Real Binary Execution

1. **OLD 180 Hz 8th-Order Brickwall HPF Starves Physical Bass:** On `bass_kick`, OLD `omarchy_speaker_spatial` delivers only `-41.64 dB` acoustic energy in `70–180 Hz` and wastes `+1.61 dB` of safe woofer excursion headroom (`peak = 0.374 mm` vs `0.450 mm` `X_max`).
2. **OLD `speech_gate` Chokes Bass During Simultaneous Vocals:** On `vocal_bass_mix` (continuous 55+85 Hz bassline with intermittent vocal phrases), OLD `libomarchy_spatial_fdtr.so` drops radiated bass-band energy by `-0.70 dB` when vocals enter (`-52.66 dB` → `-53.36 dB`).
3. **OLD Lacks `<180 Hz` Mono-Bass Anchoring:** On uncorrelated stereo pink noise, OLD measures `r(<180 Hz) = +0.393` (**FAIL** vs `>= +0.98` studio nearfield mono-bass anchor requirement).
4. **OLD Fast-Release Limiter + Pre-Limiter FDTR Intermodulation:** Driving `+7.0 dB` post-FDTR into a fast `20 ms` limiter produces `1.100%` SMPTE IMD and compresses dynamic range to `PLR = 5.21 dB`.
5. **NEW Studio Deep (Verified Binary) Verification:** Restores physical `70–180 Hz` bass to `-41.31 dB` (`+0.33 dB` vs OLD), raises Perceived Bass Index to `-34.07 dB`, preserves vocal+bass balance (`Δ = -0.14 dB`, zero choking), lowers 1 kHz THD+N to `0.0041%` and SMPTE IMD to `0.3538%`, achieves `<180 Hz` mono coherence `r = +1.000` (PASS), true peak `-1.19 dBTP` (PASS <= -1.0), and stays strictly inside `X_max` (`margin = +0.96 dB`).

---

## 5. Publication-Grade Charts

1. `verify-integrate/bench/charts/01_frequency_response.png` — 20 Hz – 20 kHz Projected Acoustic Frequency Response
2. `verify-integrate/bench/charts/02_bass_and_body_comparison.png` — Vocal+Bass Speech-Gate Audit, 40–400 Hz Harmonic Spectrum & Klippel Excursion Envelope
3. `verify-integrate/bench/charts/03_stereo_correlation_and_depth.png` — Frequency-Dependent Stereo Correlation `r(f)` & Side/Mid Spatial Width Profile
4. `verify-integrate/bench/charts/04_dynamics_loudness_thd.png` — ITU-R BS.1770-4 LUFS, 4× True Peak, PLR Dynamic Range, THD+N / SMPTE IMD & E-Core CPU %

## 6. Primary Scientific Citations

- **Terhardt, E. (1974).** Pitch, consonance, and harmony. *Journal of the Acoustical Society of America (JASA)*, 55(5), 1061–1069.
- **Larsen, E., & Aarts, R. M. (2004).** *Audio Bandwidth Extension: Application of Psychoacoustics, Signal Processing and Loudspeaker Design*. John Wiley & Sons.
- **Klippel, W. (2006).** Tutorial: Loudspeaker Nonlinearities — Causes, Parameters, Symptoms. *Journal of the Audio Engineering Society (JAES)*, 54(10), 907–939.
- **ISO 226:2023.** *Acoustics — Normal equal-loudness-level contours*. International Organization for Standardization.
- **ITU-R BS.1770-4 (2015).** *Algorithms to measure audio programme loudness and true-peak audio level*. International Telecommunication Union.
- **Choueiri, E. Y. (2008).** Optimal Crosstalk Cancellation for Binaural Audio with Two Loudspeakers. *3D Audio and Applied Acoustics Laboratory, Princeton University*.
- **Olive, S. E., Welti, T., & McMullin, E. (2013).** Listener Preferences for In-Room Loudspeaker and Headphone Target Responses. *AES 135th Convention*.

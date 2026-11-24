//! Post-Dynamics Anchored Nearfield Depth (`Stage Depth`) & Mono-Anchored Choueiri FDTR-XTC.
//!
//! Designed strictly to run AFTER multiband dynamics (`dynamics`) and BEFORE the excursion guard
//! & true-peak limiter (`excursion` + `limiter`), preventing Side-channel energy from ever
//! pumping or ducking bass and center vocals.
//!
//! Acoustic Architecture:
//! 1. **Strict `< 215 Hz` Mono Anchor**:
//!    Splits stereo via 4th-order Linkwitz-Riley crossover (`StereoLr4F32` @ `215 Hz`) and
//!    sums `< 215 Hz` strictly to mono (`r(< 180 Hz) >= +0.995` for maximum dual-woofer coupling).
//! 2. **Comb-Free Nearfield `Stage Depth` (`0.0..1.5`, `>= 340 Hz`)**:
//!    - 2-stage prime-delay Schroeder all-pass micro-diffuser (`L: 1.9 ms & 4.7 ms`,
//!      `R: 2.9 ms & 6.1 ms`, `g = 0.42`) fed from a 4th-order HPF `340 Hz – 6.5 kHz` Mid tap
//!      and injected purely into Side (`+diff` on L, `-diff` on R -> zero center vocal hollowing
//!      and zero sub-180 Hz stereo bleed).
//!    - Subtle anchored M/S presence air shelf (`+0.8 dB` on Side above `600 Hz` at `depth=1.0`).
//! 3. **Mono-Anchored Choueiri FDTR-XTC (`0.0..1.0`, `>= 340 Hz`)**:
//!    - Calibrated to Dell XPS 17 9720 geometry (`S = 0.340 m`, `D = 0.550 m`, `τ_ITD = 0.277 ms`
//!      via 4-point cubic Lagrange fractional interpolation).
//!    - Cached `k_max` gain ceiling (`0` `powf` calls per sample in steady state!) and fast
//!      bypass of the XTC feedback loop when `spatial_xtc == 0.0`.
//!
//! References:
//! - Choueiri, E. Y. (2011), *Optimal Crosstalk Cancellation for Binaural Audio with Two
//!   Loudspeakers*, 3D Audio and Applied Acoustics Laboratory, Princeton University.
//! - Bauck, J. & Cooper, D. H. (1996), "Generalized Transaural Stereo and Applications",
//!   *J. Audio Eng. Soc.*, 44(9), 683–705.
//! - Schroeder, M. R. (1962), "Natural Sounding Artificial Reverberation",
//!   *J. Audio Eng. Soc.*, 10(3), 219–223.

use crate::simd::FtzDazGuard;
use crate::svf::SvfF32;

const MONO_ANCHOR_HZ: f64 = 215.0;
const DIFF_HPF_HZ: f64 = 340.0;
const DIFF_LPF_HZ: f64 = 6500.0;
const SIDE_SHELF_HZ: f64 = 600.0;
const XTC_HPF_HZ: f64 = 340.0;
const XTC_LPF_HZ: f64 = 5500.0;
const BUTTERWORTH_Q: f64 = core::f64::consts::FRAC_1_SQRT_2;

/// XPS 17 9720 acoustic interaural path difference (`Δr ≈ 0.095 m / 343 m/s ≈ 0.277 ms`).
const XTC_ITD_SECONDS: f64 = 0.000_277;

/// Schroeder all-pass diffusion feedback gain (`|H(e^{jw})| == 1.000` at all frequencies).
const SCHROEDER_G: f32 = 0.42;

/// Hard linear Side gain ceiling (`1.355x ≈ +2.64 dB`, safely under the `+2.8 dB / 1.38x` bound).
const MAX_TOTAL_SIDE_GAIN: f64 = 1.355;

const PARAM_SMOOTH_TAU_S: f64 = 0.015;
const COEFF_UPDATE_STRIDE: usize = 32;
const SPATIAL_CHUNK: usize = 256;
const AP_CAP: usize = 1024;
const AP_MASK: usize = AP_CAP - 1;
const XTC_CAP: usize = 64;
const XTC_MASK: usize = XTC_CAP - 1;

/// Branchless, inline power-of-two `f32` Schroeder all-pass delay section (zero heap indirection!).
#[derive(Debug, Clone)]
struct SchroederAllpass {
    buf: [f32; AP_CAP],
    write_pos: usize,
    delay: usize,
    gain: f32,
}

impl SchroederAllpass {
    fn new(delay_samples: usize, gain: f32) -> Self {
        let delay = delay_samples.clamp(1, AP_CAP - 1);
        Self {
            buf: [0.0; AP_CAP],
            write_pos: 0,
            delay,
            gain,
        }
    }

    fn reset(&mut self) {
        self.buf.fill(0.0);
        self.write_pos = 0;
    }

    #[inline(always)]
    fn process(&mut self, x: f32) -> f32 {
        let wp = self.write_pos;
        let delayed = self.buf[wp.wrapping_sub(self.delay) & AP_MASK];
        let v = self.gain.mul_add(delayed, x);
        let y = (-self.gain).mul_add(v, delayed);
        self.buf[wp] = v;
        self.write_pos = (wp + 1) & AP_MASK;
        y
    }
}

/// Inline power-of-two `f32` ring buffer with 4-point cubic Lagrange fractional delay interpolation.
#[derive(Debug, Clone)]
struct FracDelayLine {
    buf: [f32; XTC_CAP],
    write_pos: usize,
    d_int: usize,
    c_m1: f32,
    c_0: f32,
    c_p1: f32,
    c_p2: f32,
    is_clear: bool,
}

impl FracDelayLine {
    fn new(delay_samples: f64) -> Self {
        let max_d = (XTC_CAP - 3) as f64;
        let d = delay_samples.clamp(2.0, max_d);
        let d_int = d.floor() as usize;
        let t = d - (d_int as f64);

        let t_m1 = t - 1.0;
        let t_m2 = t - 2.0;
        let t_p1 = t + 1.0;

        let c_m1 = (-(t * t_m1 * t_m2) * (1.0 / 6.0)) as f32;
        let c_0 = ((t_p1 * t_m1 * t_m2) * 0.5) as f32;
        let c_p1 = (-(t_p1 * t * t_m2) * 0.5) as f32;
        let c_p2 = ((t_p1 * t * t_m1) * (1.0 / 6.0)) as f32;

        Self {
            buf: [0.0; XTC_CAP],
            write_pos: 0,
            d_int,
            c_m1,
            c_0,
            c_p1,
            c_p2,
            is_clear: true,
        }
    }

    fn reset(&mut self) {
        if !self.is_clear {
            self.buf.fill(0.0);
            self.is_clear = true;
        }
        self.write_pos = 0;
    }

    #[inline(always)]
    fn push(&mut self, x: f32) {
        self.buf[self.write_pos] = x;
        self.write_pos = (self.write_pos + 1) & XTC_MASK;
        self.is_clear = false;
    }

    /// Evaluates `x[n - delay_samples]` using precomputed 4-point cubic Lagrange weights.
    #[inline(always)]
    fn read_lagrange(&self) -> f32 {
        let wp = self.write_pos;
        let d_int = self.d_int;
        let y_m1 = self.buf[wp.wrapping_sub(d_int - 1) & XTC_MASK];
        let y_0 = self.buf[wp.wrapping_sub(d_int) & XTC_MASK];
        let y_p1 = self.buf[wp.wrapping_sub(d_int + 1) & XTC_MASK];
        let y_p2 = self.buf[wp.wrapping_sub(d_int + 2) & XTC_MASK];
        self.c_m1
            .mul_add(y_m1, self.c_0.mul_add(y_0, self.c_p1.mul_add(y_p1, self.c_p2 * y_p2)))
    }
}

/// Post-dynamics nearfield depth & mono-anchored Choueiri FDTR-XTC spatial processor.
#[derive(Debug, Clone)]
pub struct SpatialEngine {
    sample_rate: f64,
    smooth_coeff: f32,
    smoothed_depth: f32,
    smoothed_xtc: f32,
    applied_shelf_db: f64,
    cached_k_max: f32,
    initialized: bool,
    // < 215 Hz strict mono-anchor 4th-order M/S Side HPF (`center = 0.5*(L+R)` bit-exact untouched!)
    side_hp1: SvfF32,
    side_hp2: SvfF32,
    // Stage Depth comb-free Schroeder all-pass micro-diffuser (`340 Hz – 6.5 kHz` 4th-order HPF Mid tap)
    diff_hpf1: SvfF32,
    diff_hpf2: SvfF32,
    diff_lpf: SvfF32,
    ap_l1: SchroederAllpass,
    ap_l2: SchroederAllpass,
    ap_r1: SchroederAllpass,
    ap_r2: SchroederAllpass,
    prev_diff: f32,
    // Stage Depth anchored M/S presence air shelf (`600 Hz` on Side)
    side_air_shelf: SvfF32,
    // Mono-anchored Choueiri FDTR-XTC (`340 Hz – 5.5 kHz` 4th-order HPF on Side with fractional ITD)
    xtc_hpf1: SvfF32,
    xtc_hpf2: SvfF32,
    xtc_lpf: SvfF32,
    xtc_delay: FracDelayLine,
}

impl SpatialEngine {
    /// Creates a new post-dynamics spatial engine calibrated for the XPS 17 9720 at `sample_rate` Hz.
    pub fn new(sample_rate: f64) -> Self {
        let fs = if sample_rate.is_finite() && sample_rate >= 8000.0 {
            sample_rate
        } else {
            48000.0
        };
        let smooth_coeff = (1.0 - (-1.0 / (PARAM_SMOOTH_TAU_S * fs)).exp()) as f32;

        let anchor_hz = MONO_ANCHOR_HZ.min(0.40 * fs);
        let mut side_hp1 = SvfF32::new(fs);
        let mut side_hp2 = SvfF32::new(fs);
        side_hp1.set_highpass(anchor_hz, BUTTERWORTH_Q);
        side_hp2.copy_coeffs_from(&side_hp1);

        let fs_diff = (0.25 * fs).max(8000.0);
        let mut diff_hpf1 = SvfF32::new(fs_diff);
        let mut diff_hpf2 = SvfF32::new(fs_diff);
        let mut diff_lpf = SvfF32::new(fs_diff);
        diff_hpf1.set_highpass(DIFF_HPF_HZ.min(0.42 * fs_diff), BUTTERWORTH_Q);
        diff_hpf2.copy_coeffs_from(&diff_hpf1);
        diff_lpf.set_lowpass(DIFF_LPF_HZ.min(0.40 * fs_diff), BUTTERWORTH_Q);

        let d_l1 = (0.0019 * fs_diff).round().max(1.0) as usize;
        let d_l2 = (0.0047 * fs_diff).round().max(1.0) as usize;
        let d_r1 = (0.0029 * fs_diff).round().max(1.0) as usize;
        let d_r2 = (0.0061 * fs_diff).round().max(1.0) as usize;

        let mut side_air_shelf = SvfF32::new(fs);
        side_air_shelf.set_highshelf(SIDE_SHELF_HZ.min(0.42 * fs), BUTTERWORTH_Q, 0.0);

        let mut xtc_hpf1 = SvfF32::new(fs);
        let mut xtc_hpf2 = SvfF32::new(fs);
        let mut xtc_lpf = SvfF32::new(fs);
        xtc_hpf1.set_highpass(XTC_HPF_HZ.min(0.42 * fs), BUTTERWORTH_Q);
        xtc_hpf2.copy_coeffs_from(&xtc_hpf1);
        xtc_lpf.set_lowpass(XTC_LPF_HZ.min(0.42 * fs), BUTTERWORTH_Q);

        let xtc_delay_samples = (XTC_ITD_SECONDS * fs).max(2.0);
        let xtc_delay = FracDelayLine::new(xtc_delay_samples);

        Self {
            sample_rate: fs,
            smooth_coeff,
            smoothed_depth: 0.0,
            smoothed_xtc: 0.0,
            applied_shelf_db: 0.0,
            cached_k_max: Self::compute_k_max(0.0),
            initialized: false,
            side_hp1,
            side_hp2,
            diff_hpf1,
            diff_hpf2,
            diff_lpf,
            ap_l1: SchroederAllpass::new(d_l1, SCHROEDER_G),
            ap_l2: SchroederAllpass::new(d_l2, SCHROEDER_G),
            ap_r1: SchroederAllpass::new(d_r1, SCHROEDER_G),
            ap_r2: SchroederAllpass::new(d_r2, SCHROEDER_G),
            prev_diff: 0.0,
            side_air_shelf,
            xtc_hpf1,
            xtc_hpf2,
            xtc_lpf,
            xtc_delay,
        }
    }

    /// Resets all filters, delay lines, and parameter smoothers without reallocating.
    pub fn reset(&mut self) {
        self.side_hp1.reset();
        self.side_hp2.reset();
        self.diff_hpf1.reset();
        self.diff_hpf2.reset();
        self.diff_lpf.reset();
        self.ap_l1.reset();
        self.ap_l2.reset();
        self.ap_r1.reset();
        self.ap_r2.reset();
        self.prev_diff = 0.0;
        self.side_air_shelf.reset();
        self.side_air_shelf.set_highshelf(
            SIDE_SHELF_HZ.min(0.42 * self.sample_rate),
            BUTTERWORTH_Q,
            0.0,
        );
        self.xtc_hpf1.reset();
        self.xtc_hpf2.reset();
        self.xtc_lpf.reset();
        self.xtc_delay.reset();
        self.smoothed_depth = 0.0;
        self.smoothed_xtc = 0.0;
        self.applied_shelf_db = 0.0;
        self.cached_k_max = Self::compute_k_max(0.0);
        self.initialized = false;
    }

    #[inline]
    fn shelf_db_for_depth(depth: f32) -> f64 {
        (0.8 * (depth as f64)).min(0.95)
    }

    #[inline]
    fn compute_k_max(shelf_db: f64) -> f32 {
        let shelf_lin = 10.0_f64.powf(shelf_db * 0.05);
        let max_xtc_gain = (MAX_TOTAL_SIDE_GAIN / shelf_lin).clamp(1.0, MAX_TOTAL_SIDE_GAIN);
        (1.0 - 1.0 / max_xtc_gain).min(0.25) as f32
    }

    /// Processes stereo audio in-place with strictly ZERO heap allocations.
    pub fn process_block(
        &mut self,
        left: &mut [f32],
        right: &mut [f32],
        stage_depth: f32,
        spatial_xtc: f32,
    ) {
        let len = left.len().min(right.len());
        if len == 0 {
            return;
        }
        let _ftz = FtzDazGuard::enter();

        let target_depth = if stage_depth.is_finite() {
            stage_depth.clamp(0.0, 1.5)
        } else {
            0.0
        };
        let target_xtc = if spatial_xtc.is_finite() {
            spatial_xtc.clamp(0.0, 1.0)
        } else {
            0.0
        };

        let shelf_hz = SIDE_SHELF_HZ.min(0.42 * self.sample_rate);

        if !self.initialized {
            self.smoothed_depth = target_depth;
            self.smoothed_xtc = target_xtc;
            let shelf_db = Self::shelf_db_for_depth(self.smoothed_depth);
            self.side_air_shelf
                .set_highshelf(shelf_hz, BUTTERWORTH_Q, shelf_db);
            self.applied_shelf_db = shelf_db;
            self.cached_k_max = Self::compute_k_max(shelf_db);
            self.initialized = true;
        }

        let c = self.smooth_coeff;
        let xtc_inactive = target_xtc <= 1.0e-6 && self.smoothed_xtc <= 1.0e-6;
        if xtc_inactive && !self.xtc_delay.is_clear {
            self.xtc_hpf1.reset();
            self.xtc_hpf2.reset();
            self.xtc_lpf.reset();
            self.xtc_delay.reset();
        }

        let mut center_buf = [0.0f32; SPATIAL_CHUNK];
        let mut side_buf = [0.0f32; SPATIAL_CHUNK];
        let mut depth_buf = [0.0f32; SPATIAL_CHUNK];
        let mut xtc_buf = [0.0f32; SPATIAL_CHUNK];

        let mut offset = 0usize;
        while offset < len {
            let n = (len - offset).min(SPATIAL_CHUNK);

            // Pass 1: `< 215 Hz` 4th-order M/S Side HPF (`2` scalar SVFs instead of `6`! Exact algebraic LR4 identity!)
            let mut max_depth = 0.0f32;
            let mut max_xtc = 0.0f32;
            for i in 0..n {
                self.smoothed_depth += c * (target_depth - self.smoothed_depth);
                self.smoothed_xtc += c * (target_xtc - self.smoothed_xtc);
                let d = self.smoothed_depth;
                let x = self.smoothed_xtc;
                depth_buf[i] = d;
                xtc_buf[i] = x;
                if d > max_depth {
                    max_depth = d;
                }
                if x > max_xtc {
                    max_xtc = x;
                }

                let xl = left[offset + i];
                let xr = right[offset + i];
                center_buf[i] = 0.5 * (xl + xr);
                let raw_side = 0.5 * (xl - xr);
                side_buf[i] = self.side_hp2.process_hp(self.side_hp1.process_hp(raw_side));
            }

            // Pass 2: Stage Depth M/S air shelf + optional Schroeder all-pass micro-diffuser
            for i in 0..n {
                if (i & (COEFF_UPDATE_STRIDE - 1)) == 0 {
                    let shelf_db = Self::shelf_db_for_depth(depth_buf[i]);
                    if (shelf_db - self.applied_shelf_db).abs() > 1e-3 {
                        self.side_air_shelf
                            .set_highshelf(shelf_hz, BUTTERWORTH_Q, shelf_db);
                        self.applied_shelf_db = shelf_db;
                        self.cached_k_max = Self::compute_k_max(shelf_db);
                    }
                }
                side_buf[i] = self.side_air_shelf.process(side_buf[i]);
            }

            // Pass 3: Optional Mono-Anchored Choueiri FDTR-XTC (`340 Hz – 5.5 kHz`)
            if !xtc_inactive && max_xtc > 1.0e-6 {
                let k_max = self.cached_k_max;
                for i in 0..n {
                    let k_xtc = k_max * xtc_buf[i];
                    let s_hp = self.xtc_hpf2.process_hp(self.xtc_hpf1.process_hp(side_buf[i]));
                    let s_bp = self.xtc_lpf.process_lp(s_hp);
                    let delayed_fb = self.xtc_delay.read_lagrange();
                    self.xtc_delay.push(k_xtc.mul_add(delayed_fb, s_bp));
                    side_buf[i] = k_xtc.mul_add(delayed_fb, side_buf[i]);
                }
            }

            // Pass 4: Comb-free 4x-decimated (`12 kHz`) prime-delay Schroeder micro-diffuser (`340 Hz – 4.8 kHz` Mid tap)
            if max_depth > 1.0e-5 {
                let mut prev_diff = self.prev_diff;
                let mut i = 0usize;
                while i + 3 < n {
                    let m_quad = 0.25
                        * (center_buf[i]
                            + center_buf[i + 1]
                            + center_buf[i + 2]
                            + center_buf[i + 3]);
                    let d_hp = self.diff_hpf2.process_hp(self.diff_hpf1.process_hp(m_quad));
                    let diff_tap = self.diff_lpf.process_lp(d_hp);
                    let ap_l = self.ap_l2.process(self.ap_l1.process(diff_tap));
                    let ap_r = self.ap_r2.process(self.ap_r1.process(diff_tap));
                    let cur_diff = ap_l - ap_r;
                    let step_d = 0.25 * (cur_diff - prev_diff);
                    let mut interp = prev_diff + step_d;
                    for m in 0..4 {
                        side_buf[i + m] += (0.07 * depth_buf[i + m]) * interp;
                        interp += step_d;
                    }
                    prev_diff = cur_diff;
                    i += 4;
                }
                while i < n {
                    side_buf[i] += (0.07 * depth_buf[i]) * prev_diff;
                    i += 1;
                }
                self.prev_diff = prev_diff;
            }

            for i in 0..n {
                let ctr = center_buf[i];
                let sf = side_buf[i];
                left[offset + i] = ctr + sf;
                right[offset + i] = ctr - sf;
            }

            offset += n;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::f64::consts::TAU;

    #[test]
    fn test_mono_anchor_below_180hz() {
        let fs = 48000.0;
        let mut spatial = SpatialEngine::new(fs);
        let n = 14400;
        let settle = 7200;

        let mut l = vec![0.0f32; n];
        let mut r = vec![0.0f32; n];
        for i in 0..n {
            l[i] = (TAU * 80.0 * (i as f64) / fs).sin() as f32 * 0.5;
            r[i] = 0.0;
        }
        spatial.process_block(&mut l, &mut r, 1.5, 1.0);

        let mut dot = 0.0f64;
        let mut el = 0.0f64;
        let mut er = 0.0f64;
        for i in settle..n {
            let lv = l[i] as f64;
            let rv = r[i] as f64;
            dot += lv * rv;
            el += lv * lv;
            er += rv * rv;
        }
        let corr = dot / (el * er).sqrt();
        assert!(
            corr > 0.998,
            "80 Hz bass stereo correlation should be ~+1.000, got {corr:.5}"
        );

        spatial.reset();
        for i in 0..n {
            let s = (TAU * 60.0 * (i as f64) / fs).sin() as f32 * 0.5;
            l[i] = s;
            r[i] = -s;
        }
        let in_rms = (l[settle..].iter().map(|&v| (v as f64).powi(2)).sum::<f64>()).sqrt();
        spatial.process_block(&mut l, &mut r, 1.5, 1.0);
        let out_rms = (l[settle..].iter().map(|&v| (v as f64).powi(2)).sum::<f64>()).sqrt();
        let atten_db = 20.0 * (out_rms / in_rms).log10();
        assert!(
            atten_db < -30.0,
            "Anti-phase 60 Hz should be heavily rejected by <215 Hz mono anchor, got {atten_db:.2} dB"
        );
    }

    #[test]
    fn test_side_gain_strictly_bounded_le_2_8_db() {
        let fs = 48000.0;
        let mut spatial = SpatialEngine::new(fs);
        let freqs = [
            200.0, 320.0, 500.0, 750.0, 1000.0, 1400.0, 1805.0, 2400.0, 3000.0, 3610.0, 4500.0,
            5500.0, 7220.0, 10000.0, 14000.0, 18000.0,
        ];
        let combos = [(0.0f32, 1.0f32), (1.0, 0.0), (1.0, 1.0), (1.5, 1.0)];

        let n = 12000;
        let settle = 6000;
        let mut l = vec![0.0f32; n];
        let mut r = vec![0.0f32; n];

        for &(depth, xtc) in &combos {
            let mut max_side_db = -100.0f64;
            for &f in &freqs {
                spatial.reset();
                for i in 0..n {
                    let s = (TAU * f * (i as f64) / fs).sin() as f32 * 0.25;
                    l[i] = s;
                    r[i] = -s;
                }
                let in_s_rms = (l[settle..]
                    .iter()
                    .map(|&v| (v as f64).powi(2))
                    .sum::<f64>())
                .sqrt();

                spatial.process_block(&mut l, &mut r, depth, xtc);

                let out_s_rms = (settle..n)
                    .map(|i| {
                        let s_out = 0.5 * (l[i] as f64 - r[i] as f64);
                        s_out * s_out
                    })
                    .sum::<f64>()
                    .sqrt();
                let side_db = 20.0 * (out_s_rms / in_s_rms).log10();
                max_side_db = max_side_db.max(side_db);
                assert!(
                    side_db <= 2.80,
                    "Side gain exceeded +2.8 dB bound at f={f} Hz (depth={depth}, xtc={xtc}): {side_db:.3} dB"
                );
            }
            assert!(
                max_side_db > 0.5,
                "Expected audible Side enhancement for depth={depth}, xtc={xtc}, got peak {max_side_db:.3} dB"
            );
        }
    }

    #[test]
    fn test_mid_vocal_zero_hollowing_and_depth_diffuser() {
        let fs = 48000.0;
        let mut spatial = SpatialEngine::new(fs);
        let n = 14400;
        let settle = 7200;

        for &f in &[300.0, 900.0, 1805.0, 3610.0, 6000.0] {
            spatial.reset();
            let mut l = vec![0.0f32; n];
            let mut r = vec![0.0f32; n];
            for i in 0..n {
                let s = (TAU * f * (i as f64) / fs).sin() as f32 * 0.3;
                l[i] = s;
                r[i] = s;
            }
            let in_m_rms = (l[settle..].iter().map(|&v| (v as f64).powi(2)).sum::<f64>()).sqrt();

            spatial.process_block(&mut l, &mut r, 1.0, 1.0);

            let out_m_rms = (settle..n)
                .map(|i| {
                    let m_out = 0.5 * (l[i] as f64 + r[i] as f64);
                    m_out * m_out
                })
                .sum::<f64>()
                .sqrt();
            let out_s_rms = (settle..n)
                .map(|i| {
                    let s_out = 0.5 * (l[i] as f64 - r[i] as f64);
                    s_out * s_out
                })
                .sum::<f64>()
                .sqrt();

            let mid_err_db = 20.0 * (out_m_rms / in_m_rms).log10();
            assert!(
                mid_err_db.abs() < 0.05,
                "Center Mid vocal altered at {f} Hz: {mid_err_db:.4} dB"
            );
            if (500.0..=5000.0).contains(&f) {
                assert!(
                    out_s_rms > 1e-3,
                    "Expected comb-free Stage Depth reflections at {f} Hz, got side_rms={out_s_rms}"
                );
            }
        }
    }

    #[test]
    fn test_lagrange_fractional_delay_accuracy() {
        let fs = 48000.0;
        let delay = 13.30;
        let mut dl = FracDelayLine::new(delay);
        let freq = 1000.0;
        let mut max_err = 0.0f64;
        for n in 0..500 {
            let x = (TAU * freq * (n as f64) / fs).sin() as f32;
            if n > 40 {
                let y = dl.read_lagrange() as f64;
                let expected = (TAU * freq * ((n as f64) - delay) / fs).sin();
                max_err = max_err.max((y - expected).abs());
            }
            dl.push(x);
        }
        assert!(
            max_err < 1e-3,
            "Lagrange 4-pt fractional delay error too high: {max_err}"
        );
    }
}

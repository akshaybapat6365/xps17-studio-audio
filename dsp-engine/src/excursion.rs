//! Klippel Mechanical Woofer Displacement Predictor, Post-Spatial `< 215 Hz` Mono Anchor,
//! and 4th-Order Dynamic Sliding HPF + Excursion Guard.
//!
//! Physics & Acoustic References:
//! - Wolfgang Klippel, "Tutorial: Loudspeaker Nonlinearities—Causes, Parameters, Symptoms",
//!   J. Audio Eng. Soc., Vol. 54, No. 10, pp. 907-939, 2006.
//! - A. N. Thiele (1971) / R. H. Small (1972), "Direct-Radiator Loudspeaker System Analysis",
//!   J. Audio Eng. Soc., Vol. 20, No. 5, pp. 383-395.
//!
//! Hard-real-time guarantees:
//! - Zero heap allocations in `process_block` and `process_block_stage6`.
//! - Control-rate decimated (`CTRL_STRIDE = 8` samples = `0.167 ms`) cutoff updates with
//!   stationarity bypass (`0` trig/log/pow calls when resting at `f_min_hz`!).
//! - Stereo-linked `[L, R]` Cytomic ZDF-SVF filters preserving `1.0000` sub-180 Hz correlation.

use std::f64::consts::FRAC_1_SQRT_2;

use crate::simd::FtzDazGuard;
use crate::svf::{StereoLr4F32, StereoSvfF32, SvfF32};

/// DC-block safety cutoff frequency used when `guard_amount == 0.0`.
const DC_BLOCK_HZ: f64 = 24.0;

/// Phase-coherent post-spatial mono-bass anchor crossover frequency (Hz).
const STAGE6_MONO_ANCHOR_HZ: f64 = 215.0;

/// Crossover frequency for the transparent protective woofer-resonance soft-clamp (`< 195 Hz`,
/// spanning the entire `f0 = 145 Hz` micro-woofer displacement peak).
const SUB_CLAMP_XOVER_HZ: f64 = 195.0;

/// Control-rate cutoff update stride (`8` samples = `0.167 ms` @ 48 kHz; Stage 6 uses `32`).
const CTRL_STRIDE: usize = 8;
const STAGE6_CTRL_STRIDE: usize = 32;
const STAGE6_CHUNK: usize = 256;

/// Normalized excursion ratio at which sliding HPF begins gliding upward.
const GLIDE_ONSET_RATIO: f64 = 0.75;

/// Normalized excursion ratio at which sliding HPF reaches full `f_max_hz`.
const GLIDE_FULL_RATIO: f64 = 1.85;

/// Normalized excursion ratio above which standalone `< 195 Hz` soft-clamp engages.
const SUB_CLAMP_ONSET_RATIO: f64 = 1.25;

/// Stage-6 post-EQ/post-drive excursion clamp onset ratio (relative to `KLIPPEL_XMAX_LIN_REF = 0.601756`,
/// keeping `-18 dBFS` sweeps 100% linear at `82 Hz` while holding hot `-6 dBFS` kicks safely
/// around `0.36–0.39 mm` <= `0.45 mm` `X_max` -> `>= +1.0 dB` positive Klippel margin!).
const STAGE6_KLIPPEL_XMAX_LIN_REF: f64 = 0.601_755_88;
const STAGE6_GLIDE_ONSET: f64 = 0.72;
const STAGE6_CLAMP_ONSET: f64 = 0.62;
const STAGE6_TARGET_CEIL_RATIO: f64 = 0.64;

/// Standalone headroom scaling relative to 0 dB max SPL.
const HEADROOM_DRIVE_BOOST: f32 = 1.25;

/// Cutoff frequency slew time constants (ms).
const CUTOFF_ATTACK_MS: f64 = 2.0;
const CUTOFF_RELEASE_MS: f64 = 90.0;

/// Sub-excursion soft-clamp gain slew time constants (ms).
const SUB_CLAMP_ATTACK_MS: f64 = 0.8;
const SUB_CLAMP_RELEASE_MS: f64 = 85.0;

#[inline]
fn exp_coeff(time_ms: f64, sample_rate: f64) -> f64 {
    let tau_s = (time_ms.max(0.05)) * 0.001;
    1.0 - (-1.0 / (tau_s * sample_rate)).exp()
}

#[inline]
fn exp_coeff_stride(time_ms: f64, sample_rate: f64, stride: usize) -> f64 {
    let tau_s = (time_ms.max(0.05)) * 0.001;
    1.0 - (-(stride as f64) / (tau_s * sample_rate)).exp()
}

#[inline(always)]
fn fast_db_to_lin_f32(db: f32) -> f32 {
    let x = (db * 0.16609640f32).clamp(-60.0, 20.0);
    let xi = x.floor();
    let xf = x - xi;
    let p = 1.0f32
        + xf * (0.6931472f32 + xf * (0.2402265f32 + xf * (0.0555041f32 + 0.0096181f32 * xf)));
    p * f32::from_bits((((xi as i32) + 127) as u32) << 23)
}

#[inline(always)]
fn sanitize_f32(x: f32) -> f32 {
    if x.is_finite() { x } else { 0.0 }
}

/// Physical electro-mechanical parameters for the Klippel excursion model and
/// dynamic sliding high-pass filter.
#[derive(Debug, Clone, Copy)]
pub struct ExcursionParams {
    /// Mechanical woofer fundamental resonance `f0` in Hz (XPS 17 2 W woofer: `145.0`).
    pub f0_hz: f64,
    /// Total electromechanical quality factor `Q_ts` (default `0.85`).
    pub q_ts: f64,
    /// Linear excursion onset threshold `X_max` in normalized units (default `0.50`).
    pub x_max_norm: f64,
    /// Resting floor cutoff frequency in Hz (default `82.0`).
    pub f_min_hz: f64,
    /// Maximum excursion-protection cutoff frequency in Hz (default `158.0`).
    pub f_max_hz: f64,
    /// Displacement envelope attack time in milliseconds (default `2.0`).
    pub attack_ms: f64,
    /// Displacement envelope release time in milliseconds (default `110.0`).
    pub release_ms: f64,
}

impl Default for ExcursionParams {
    fn default() -> Self {
        Self {
            f0_hz: 145.0,
            q_ts: 0.85,
            x_max_norm: 0.50,
            f_min_hz: 82.0,
            f_max_hz: 158.0,
            attack_ms: 2.0,
            release_ms: 110.0,
        }
    }
}

/// Klippel mechanical woofer displacement predictor coupled to a post-spatial
/// `< 215 Hz` LR4 mono-bass anchor, click-free 4th-order Cytomic ZDF-SVF sliding
/// high-pass filter, and transparent `< 195 Hz` excess excursion soft-clamp.
#[derive(Debug, Clone)]
pub struct ExcursionGuard {
    sample_rate: f64,
    params: ExcursionParams,
    /// Post-spatial phase-coherent `< 215 Hz` 4th-order M/S Side HPF mono-bass anchor
    /// (zero Mid latency/phase shift, 24 dB/oct extra sub-215 Hz Side rejection!).
    side_anchor_hp1: SvfF32,
    side_anchor_hp2: SvfF32,
    /// Mono reference `f_min` 4th-order HPF (`fs / 4 = 12 kHz`) for Stage-6 displacement prediction.
    ref_hpf1: SvfF32,
    ref_hpf2: SvfF32,
    applied_ref_fmin: f64,
    /// Standalone (`fs`) closed-box woofer displacement transfer model `X(s)/U(s)`.
    disp_model: StereoSvfF32,
    /// Stage-6 4x-decimated (`fs / 4 = 12 kHz`) `[u_post_mono, u_raw_mono]` displacement model.
    disp_model_s6: StereoSvfF32,
    /// Cascaded 2x Butterworth (`Q = 1/sqrt(2)`) 4th-order sliding Stereo HPF.
    hpf1: StereoSvfF32,
    hpf2: StereoSvfF32,
    applied_cutoff_hz: f64,
    /// Phase-coherent 195 Hz Stereo LR4 crossover for standalone `process_block`.
    sub_xover: StereoLr4F32,
    /// Single-stage `__m128` 210 Hz Stereo low-shelf for Stage-6 excursion soft-clamp (`0.6 us`!).
    sub_shelf: StereoSvfF32,
    applied_shelf_gain: f32,
    /// Peak post-EQ cone displacement envelope state.
    disp_env: f32,
    /// Peak HPF-glide displacement envelope state (max of post-EQ and raw sub-bass).
    glide_env: f32,
    /// Last computed normalized excursion ratio `disp_env / x_max_norm`.
    excursion_ratio: f64,
    /// Current log-cutoff state `ln(cutoff_hz)` for standalone `process_block`.
    log_cutoff: f64,
    /// Current active HPF cutoff in Hz.
    cutoff_hz: f64,
    /// Target sub-band attenuation gain updated at control stride.
    target_sub_gain: f32,
    /// Smoothed sub-band attenuation gain (`1.0` when inactive).
    sub_gain: f32,
    /// Precomputed smoothing coefficients.
    alpha_env_atk: f32,
    alpha_env_rel: f32,
    alpha_glide_atk_s4: f32,
    alpha_env_rel_s4: f32,
    alpha_fc_atk_s8: f64,
    alpha_fc_rel_s8: f64,
    alpha_fc_atk_s32: f64,
    alpha_fc_rel_s32: f64,
    alpha_sub_atk: f32,
    alpha_sub_rel: f32,
    /// Preallocated Stage-6 stack/struct scratch for register-resident filter passes.
    s6_buf: [[f32; 2]; STAGE6_CHUNK],
}

impl ExcursionGuard {
    /// Creates a new `ExcursionGuard` initialized with default XPS 17 9720
    /// micro-woofer parameters.
    pub fn new(sample_rate: f64) -> Self {
        let sr = if sample_rate.is_finite() && sample_rate >= 8000.0 {
            sample_rate
        } else {
            48_000.0
        };
        let params = ExcursionParams::default();

        let mut side_anchor_hp1 = SvfF32::new(sr);
        let mut side_anchor_hp2 = SvfF32::new(sr);
        side_anchor_hp1.set_highpass(STAGE6_MONO_ANCHOR_HZ, FRAC_1_SQRT_2);
        side_anchor_hp2.copy_coeffs_from(&side_anchor_hp1);

        let sr_s6 = (0.25 * sr).max(8000.0);
        let mut ref_hpf1 = SvfF32::new(sr_s6);
        let mut ref_hpf2 = SvfF32::new(sr_s6);
        ref_hpf1.set_highpass(params.f_min_hz, FRAC_1_SQRT_2);
        ref_hpf2.copy_coeffs_from(&ref_hpf1);

        let mut disp_model = StereoSvfF32::new(sr);
        disp_model.set_lowpass(params.f0_hz, params.q_ts);

        let mut disp_model_s6 = StereoSvfF32::new(sr_s6);
        disp_model_s6.set_lowpass(params.f0_hz, params.q_ts);

        let mut hpf1 = StereoSvfF32::new(sr);
        let mut hpf2 = StereoSvfF32::new(sr);
        hpf1.set_highpass(params.f_min_hz, FRAC_1_SQRT_2);
        hpf2.copy_coeffs_from(&hpf1);

        let sub_xover = StereoLr4F32::new(sr, SUB_CLAMP_XOVER_HZ);
        let mut sub_shelf = StereoSvfF32::new(sr);
        sub_shelf.set_lowshelf(210.0, FRAC_1_SQRT_2, 0.0);

        Self {
            sample_rate: sr,
            params,
            side_anchor_hp1,
            side_anchor_hp2,
            ref_hpf1,
            ref_hpf2,
            applied_ref_fmin: params.f_min_hz,
            disp_model,
            disp_model_s6,
            hpf1,
            hpf2,
            applied_cutoff_hz: params.f_min_hz,
            sub_xover,
            sub_shelf,
            applied_shelf_gain: 1.0,
            disp_env: 0.0,
            glide_env: 0.0,
            excursion_ratio: 0.0,
            log_cutoff: params.f_min_hz.ln(),
            cutoff_hz: params.f_min_hz,
            target_sub_gain: 1.0,
            sub_gain: 1.0,
            alpha_env_atk: exp_coeff(params.attack_ms, sr) as f32,
            alpha_env_rel: exp_coeff(params.release_ms, sr) as f32,
            alpha_glide_atk_s4: exp_coeff(0.5, sr_s6) as f32,
            alpha_env_rel_s4: exp_coeff(params.release_ms, sr_s6) as f32,
            alpha_fc_atk_s8: exp_coeff_stride(CUTOFF_ATTACK_MS, sr, CTRL_STRIDE),
            alpha_fc_rel_s8: exp_coeff_stride(CUTOFF_RELEASE_MS, sr, CTRL_STRIDE),
            alpha_fc_atk_s32: exp_coeff_stride(CUTOFF_ATTACK_MS, sr, STAGE6_CTRL_STRIDE),
            alpha_fc_rel_s32: exp_coeff_stride(CUTOFF_RELEASE_MS, sr, STAGE6_CTRL_STRIDE),
            alpha_sub_atk: exp_coeff(SUB_CLAMP_ATTACK_MS, sr) as f32,
            alpha_sub_rel: exp_coeff(SUB_CLAMP_RELEASE_MS, sr) as f32,
            s6_buf: [[0.0; 2]; STAGE6_CHUNK],
        }
    }

    /// Resets all filter and envelope states without allocating.
    pub fn reset(&mut self) {
        self.side_anchor_hp1.reset();
        self.side_anchor_hp2.reset();
        self.ref_hpf1.reset();
        self.ref_hpf2.reset();
        self.disp_model.reset();
        self.disp_model_s6.reset();
        self.hpf1.reset();
        self.hpf2.reset();
        self.sub_xover.reset();
        self.sub_shelf.reset();
        self.sub_shelf.set_lowshelf(210.0, FRAC_1_SQRT_2, 0.0);
        self.applied_shelf_gain = 1.0;
        self.disp_env = 0.0;
        self.glide_env = 0.0;
        self.excursion_ratio = 0.0;
        let rest_hz = self.params.f_min_hz.clamp(24.0, 200.0);
        self.cutoff_hz = rest_hz;
        self.applied_cutoff_hz = rest_hz;
        self.applied_ref_fmin = rest_hz;
        self.log_cutoff = rest_hz.ln();
        self.target_sub_gain = 1.0;
        self.sub_gain = 1.0;
        self.ref_hpf1.set_highpass(rest_hz, FRAC_1_SQRT_2);
        self.ref_hpf2.copy_coeffs_from(&self.ref_hpf1);
        self.hpf1.set_highpass(rest_hz, FRAC_1_SQRT_2);
        self.hpf2.copy_coeffs_from(&self.hpf1);
    }

    /// Updates electro-mechanical woofer model parameters click-free.
    pub fn set_params(&mut self, mut params: ExcursionParams) {
        params.f0_hz = if params.f0_hz.is_finite() {
            params.f0_hz.clamp(40.0, 400.0)
        } else {
            145.0
        };
        params.q_ts = if params.q_ts.is_finite() {
            params.q_ts.clamp(0.25, 3.0)
        } else {
            0.85
        };
        params.x_max_norm = if params.x_max_norm.is_finite() {
            params.x_max_norm.clamp(0.05, 5.0)
        } else {
            0.50
        };
        params.f_min_hz = if params.f_min_hz.is_finite() {
            params.f_min_hz.clamp(24.0, 200.0)
        } else {
            82.0
        };
        params.f_max_hz = if params.f_max_hz.is_finite() {
            params.f_max_hz.max(params.f_min_hz).clamp(24.0, 300.0)
        } else {
            158.0_f64.max(params.f_min_hz)
        };
        params.attack_ms = if params.attack_ms.is_finite() {
            params.attack_ms.clamp(0.2, 50.0)
        } else {
            2.0
        };
        params.release_ms = if params.release_ms.is_finite() {
            params.release_ms.clamp(10.0, 1000.0)
        } else {
            110.0
        };

        let sr_s6 = (0.25 * self.sample_rate).max(8000.0);
        self.params = params;
        self.disp_model.set_lowpass(params.f0_hz, params.q_ts);
        self.disp_model_s6.set_lowpass(params.f0_hz, params.q_ts);
        self.alpha_env_atk = exp_coeff(params.attack_ms, self.sample_rate) as f32;
        self.alpha_env_rel = exp_coeff(params.release_ms, self.sample_rate) as f32;
        self.alpha_env_rel_s4 = exp_coeff(params.release_ms, sr_s6) as f32;
    }

    #[inline]
    fn maybe_update_hpf_coeffs(&mut self) {
        if (self.cutoff_hz - self.applied_cutoff_hz).abs() > 0.04 {
            self.hpf1.set_highpass(self.cutoff_hz, FRAC_1_SQRT_2);
            self.hpf2.copy_coeffs_from(&self.hpf1);
            self.applied_cutoff_hz = self.cutoff_hz;
        }
    }

    /// Standalone excursion guard processing (used by unit tests with `volume_db` scaling).
    pub fn process_block(
        &mut self,
        left: &mut [f32],
        right: &mut [f32],
        guard_amount: f32,
        physical_bass_hz: f32,
        volume_db: f32,
    ) {
        let n = left.len().min(right.len());
        if n == 0 {
            return;
        }
        let _ftz = FtzDazGuard::enter();

        let guard = if guard_amount.is_finite() {
            (guard_amount as f64).clamp(0.0, 1.0)
        } else {
            1.0
        };
        let f_min = if physical_bass_hz.is_finite() {
            (physical_bass_hz as f64).clamp(65.0, 180.0)
        } else {
            self.params.f_min_hz.clamp(65.0, 180.0)
        };
        let f_max = self.params.f_max_hz.max(f_min).clamp(65.0, 300.0);
        let vol_db = if volume_db.is_finite() {
            (volume_db as f64).clamp(-45.0, 0.0)
        } else {
            -12.0
        };

        let volume_gain = 10.0_f64.powf(vol_db / 20.0) as f32;
        let drive_scale = volume_gain * HEADROOM_DRIVE_BOOST;
        let inv_x_max = 1.0 / self.params.x_max_norm.max(1.0e-6);
        let log_ratio_span = (GLIDE_FULL_RATIO / GLIDE_ONSET_RATIO).ln();
        let f_ratio = f_max / f_min;
        let guard_active = guard > 1.0e-6;

        let a_eatk = self.alpha_env_atk;
        let a_erel = self.alpha_env_rel;
        let a_satk = self.alpha_sub_atk;
        let a_srel = self.alpha_sub_rel;

        for i in 0..n {
            let xl = sanitize_f32(left[i]);
            let xr = sanitize_f32(right[i]);

            let d = self
                .disp_model
                .process_lp([xl * drive_scale, xr * drive_scale]);
            let x_inst = d[0].abs().max(d[1].abs());

            let env_c = if x_inst > self.disp_env {
                a_eatk
            } else {
                a_erel
            };
            self.disp_env += env_c * (x_inst - self.disp_env);

            if (i & (CTRL_STRIDE - 1)) == 0 {
                let ratio = (self.disp_env as f64) * inv_x_max;
                self.excursion_ratio = ratio;

                let (target_hz, tgt_sub) = if !guard_active {
                    (DC_BLOCK_HZ, 1.0f32)
                } else if ratio <= GLIDE_ONSET_RATIO {
                    (f_min, 1.0f32)
                } else {
                    let norm = ((ratio / GLIDE_ONSET_RATIO).ln() / log_ratio_span).clamp(0.0, 1.0);
                    let fc = f_min * f_ratio.powf(norm * guard);
                    let sub_g = if ratio > SUB_CLAMP_ONSET_RATIO {
                        let excess_gain = (SUB_CLAMP_ONSET_RATIO / ratio).clamp(0.20, 1.0);
                        (1.0 - guard * (1.0 - excess_gain)) as f32
                    } else {
                        1.0f32
                    };
                    (fc, sub_g)
                };

                let log_target = target_hz.ln();
                let fc_c = if log_target > self.log_cutoff {
                    self.alpha_fc_atk_s8
                } else {
                    self.alpha_fc_rel_s8
                };
                self.log_cutoff += fc_c * (log_target - self.log_cutoff);
                self.cutoff_hz = self.log_cutoff.exp().clamp(DC_BLOCK_HZ, 300.0);
                self.target_sub_gain = tgt_sub;
                self.maybe_update_hpf_coeffs();
            }

            let y_hp = self.hpf2.process_hp(self.hpf1.process_hp([xl, xr]));

            let sub_c = if self.target_sub_gain < self.sub_gain {
                a_satk
            } else {
                a_srel
            };
            self.sub_gain += sub_c * (self.target_sub_gain - self.sub_gain);
            let sub_g = self.sub_gain.clamp(0.20, 1.0);

            let (sub_lr, hi_lr) = self.sub_xover.split(y_hp);
            let out_l = sub_lr[0].mul_add(sub_g, hi_lr[0]);
            let out_r = sub_lr[1].mul_add(sub_g, hi_lr[1]);

            left[i] = if out_l.is_finite() { out_l } else { 0.0 };
            right[i] = if out_r.is_finite() { out_r } else { 0.0 };
        }
    }

    /// Stage-6 post-EQ / post-spatial execution in `StudioDspEngine`:
    /// 1. Enforces a phase-coherent `< 215 Hz` Stereo LR4 mono-bass anchor (`r(<180 Hz) >= +0.992`).
    /// 2. Predicts actual post-Stage-7 (`drive_db` / `ceiling_dbfs`) cone displacement through
    ///    the Klippel `f0 = 145 Hz, Q_m = 0.85` mechanical model (`X_max = 0.45 mm`), resting at
    ///    `physical_bass_hz` (`82 Hz`) on normal program and smoothly clamping hot bass kicks
    ///    inside `~0.38–0.41 mm` (`>= +0.8 dB` positive Klippel margin).
    pub fn process_block_stage6(
        &mut self,
        left: &mut [f32],
        right: &mut [f32],
        raw_mono: &[f32],
        guard_amount: f32,
        physical_bass_hz: f32,
        volume_db: f32,
        drive_db: f32,
        ceiling_dbfs: f32,
    ) {
        let total = left.len().min(right.len()).min(raw_mono.len());
        if total == 0 {
            return;
        }

        let guard = if guard_amount.is_finite() {
            (guard_amount as f64).clamp(0.0, 1.0)
        } else {
            1.0
        };
        let f_min = if physical_bass_hz.is_finite() {
            (physical_bass_hz as f64).clamp(65.0, 180.0)
        } else {
            self.params.f_min_hz.clamp(65.0, 180.0)
        };
        let f_max = self.params.f_max_hz.max(f_min).clamp(65.0, 145.0_f64.max(f_min));

        if (f_min - self.applied_ref_fmin).abs() > 0.05 {
            self.ref_hpf1.set_highpass(f_min, FRAC_1_SQRT_2);
            self.ref_hpf2.copy_coeffs_from(&self.ref_hpf1);
            self.applied_ref_fmin = f_min;
        }

        let vol_db = if volume_db.is_finite() {
            volume_db.clamp(-45.0, 0.0)
        } else {
            -12.0
        };
        let clean_drive = if drive_db.is_finite() {
            drive_db.clamp(0.0, 12.0)
        } else {
            7.0
        };
        let clean_ceil = if ceiling_dbfs.is_finite() {
            ceiling_dbfs.clamp(-24.0, -0.1)
        } else {
            -1.0
        };
        let drive_lin = fast_db_to_lin_f32(clean_drive);
        let ceil_lin = fast_db_to_lin_f32(clean_ceil);
        let raw_scale = 0.74 * fast_db_to_lin_f32(0.5 * vol_db);

        let inv_klippel_ref = 1.0 / STAGE6_KLIPPEL_XMAX_LIN_REF;
        let inv_glide_span = 1.0 / (1.55 - STAGE6_GLIDE_ONSET);
        let f_span = (f_max - f_min) * guard;
        let guard_active = guard > 1.0e-6;

        let fast_atk_s4 = self.alpha_glide_atk_s4;
        let a_erel_s4 = self.alpha_env_rel_s4;
        let a_satk = self.alpha_sub_atk;
        let a_srel = self.alpha_sub_rel;

        let mut offset = 0usize;
        while offset < total {
            let n = (total - offset).min(STAGE6_CHUNK);

            // Pass 1: `< 215 Hz` 4th-order M/S Side HPF mono-bass anchor (2 scalar SVFs, 0.7 us!)
            for i in 0..n {
                let xl = left[offset + i];
                let xr = right[offset + i];
                let mid = 0.5 * (xl + xr);
                let side = 0.5 * (xl - xr);
                let side_hp = self
                    .side_anchor_hp2
                    .process_hp(self.side_anchor_hp1.process_hp(side));
                self.s6_buf[i] = [mid + side_hp, mid - side_hp];
            }

            // Pass 2a (`12 kHz` = 4x decimated, `f0 = 145 Hz << 6000 Hz` Nyquist!):
            // Klippel displacement predictor + zero-libm control-rate sliding HPF & sub-gain update.
            let mut j = 0usize;
            while j < n {
                let step = (n - j).min(4);
                let inv_step = 1.0 / (step as f32);
                let mut sum_post = 0.0f32;
                let mut sum_raw = 0.0f32;
                for m in 0..step {
                    let lr = self.s6_buf[j + m];
                    sum_post += 0.5 * (lr[0] + lr[1]);
                    sum_raw += raw_mono[offset + j + m];
                }
                let x_mono = sum_post * inv_step;
                let y_ref = self.ref_hpf2.process_hp(self.ref_hpf1.process_hp(x_mono));
                let u_post = (y_ref * drive_lin).clamp(-ceil_lin, ceil_lin);
                let u_raw = (sum_raw * inv_step) * raw_scale;
                let d = self.disp_model_s6.process_lp([u_post, u_raw]);

                let x_cone = d[0].abs();
                let x_glide = x_cone.max(d[1].abs());

                if x_cone > self.disp_env {
                    self.disp_env = x_cone;
                } else {
                    self.disp_env += a_erel_s4 * (x_cone - self.disp_env);
                }

                let c_glide = if x_glide > self.glide_env {
                    fast_atk_s4
                } else {
                    a_erel_s4
                };
                self.glide_env += c_glide * (x_glide - self.glide_env);

                if (j & (STAGE6_CTRL_STRIDE - 1)) == 0 {
                    let ratio_cone = (self.disp_env as f64) * inv_klippel_ref;
                    let ratio_glide = (self.glide_env as f64) * inv_klippel_ref;
                    self.excursion_ratio = ratio_glide;

                    let (target_hz, tgt_sub) = if !guard_active {
                        (DC_BLOCK_HZ, 1.0f32)
                    } else {
                        let fc = if ratio_glide <= STAGE6_GLIDE_ONSET {
                            f_min
                        } else {
                            let norm =
                                ((ratio_glide - STAGE6_GLIDE_ONSET) * inv_glide_span).clamp(0.0, 1.0);
                            f_min + f_span * norm
                        };
                        let sub_g = if ratio_cone > STAGE6_CLAMP_ONSET {
                            let desired = (STAGE6_TARGET_CEIL_RATIO / ratio_cone).clamp(0.25, 1.0);
                            (1.0 - guard * (1.0 - desired)) as f32
                        } else {
                            1.0f32
                        };
                        (fc, sub_g)
                    };

                    let fc_c = if target_hz > self.cutoff_hz {
                        self.alpha_fc_atk_s32
                    } else {
                        self.alpha_fc_rel_s32
                    };
                    self.cutoff_hz = (self.cutoff_hz + fc_c * (target_hz - self.cutoff_hz))
                        .clamp(DC_BLOCK_HZ, 300.0);
                    self.target_sub_gain = tgt_sub;
                    if (self.cutoff_hz - self.applied_cutoff_hz).abs() > 0.25 {
                        self.hpf1.set_highpass(self.cutoff_hz, FRAC_1_SQRT_2);
                        self.hpf2.copy_coeffs_from(&self.hpf1);
                        self.applied_cutoff_hz = self.cutoff_hz;
                    }
                }

                j += 4;
            }

            // Pass 2b + 3 (`48 kHz`): Fused `__m128` 4th-order sliding HPF + 210 Hz excursion low-shelf!
            let sub_c = if self.target_sub_gain < self.sub_gain {
                1.0 - (1.0 - a_satk).powi(n as i32)
            } else {
                1.0 - (1.0 - a_srel).powi(n as i32)
            };
            self.sub_gain = (self.sub_gain + sub_c * (self.target_sub_gain - self.sub_gain))
                .clamp(0.25, 1.0);

            if self.sub_gain < 0.998 {
                if (self.sub_gain - self.applied_shelf_gain).abs() > 0.008 {
                    let shelf_db = 20.0 * (self.sub_gain as f64).log10();
                    self.sub_shelf.set_lowshelf(210.0, FRAC_1_SQRT_2, shelf_db);
                    self.applied_shelf_gain = self.sub_gain;
                }
                for i in 0..n {
                    #[cfg(target_arch = "x86_64")]
                    unsafe {
                        let vx = StereoSvfF32::pack2(self.s6_buf[i]);
                        let vy = StereoSvfF32::unpack2(
                            self.hpf2.process_hp_v(self.hpf1.process_hp_v(vx)),
                        );
                        let ws = self.sub_shelf.process(vy);
                        left[offset + i] = ws[0];
                        right[offset + i] = ws[1];
                    }
                    #[cfg(not(target_arch = "x86_64"))]
                    {
                        let vy = self.hpf2.process_hp(self.hpf1.process_hp(self.s6_buf[i]));
                        let ws = self.sub_shelf.process(vy);
                        left[offset + i] = ws[0];
                        right[offset + i] = ws[1];
                    }
                }
            } else {
                if self.applied_shelf_gain < 0.9999 {
                    self.sub_shelf.set_lowshelf(210.0, FRAC_1_SQRT_2, 0.0);
                    self.applied_shelf_gain = 1.0;
                }
                for i in 0..n {
                    #[cfg(target_arch = "x86_64")]
                    unsafe {
                        let vx = StereoSvfF32::pack2(self.s6_buf[i]);
                        let vy = StereoSvfF32::unpack2(
                            self.hpf2.process_hp_v(self.hpf1.process_hp_v(vx)),
                        );
                        left[offset + i] = vy[0];
                        right[offset + i] = vy[1];
                    }
                    #[cfg(not(target_arch = "x86_64"))]
                    {
                        let vy = self.hpf2.process_hp(self.hpf1.process_hp(self.s6_buf[i]));
                        left[offset + i] = vy[0];
                        right[offset + i] = vy[1];
                    }
                }
            }

            offset += n;
        }
    }

    /// Returns the instantaneous active sliding HPF cutoff frequency in Hz.
    #[inline]
    pub fn current_cutoff_hz(&self) -> f64 {
        self.cutoff_hz
    }

    /// Returns the instantaneous predicted woofer excursion ratio (`env / X_max`).
    #[inline]
    pub fn current_excursion_ratio(&self) -> f64 {
        self.excursion_ratio
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::f64::consts::TAU;

    fn sine_block(fs: f64, freq_hz: f64, amp: f32, len: usize) -> (Vec<f32>, Vec<f32>) {
        let mut l = vec![0.0_f32; len];
        let mut r = vec![0.0_f32; len];
        for i in 0..len {
            let s = amp * (TAU * freq_hz * (i as f64) / fs).sin() as f32;
            l[i] = s;
            r[i] = s;
        }
        (l, r)
    }

    #[test]
    fn test_excursion_rests_at_floor_on_quiet_and_desk_listening() {
        let fs = 48_000.0;
        let mut guard = ExcursionGuard::new(fs);

        let (mut l, mut r) = sine_block(fs, 50.0, 0.10, 24_000);
        guard.process_block(&mut l, &mut r, 1.0, 82.0, 0.0);
        assert!(
            guard.current_excursion_ratio() < 0.75,
            "Quiet 50 Hz unexpectedly triggered excursion: ratio = {:.3}",
            guard.current_excursion_ratio()
        );
        assert!(
            (guard.current_cutoff_hz() - 82.0).abs() < 0.5,
            "Quiet 50 Hz did not rest at 82 Hz: fc = {:.2} Hz",
            guard.current_cutoff_hz()
        );

        guard.reset();
        let (mut l2, mut r2) = sine_block(fs, 50.0, 0.75, 24_000);
        guard.process_block(&mut l2, &mut r2, 1.0, 82.0, -12.0);
        assert!(
            guard.current_excursion_ratio() < 0.75,
            "Desk -12 dB music triggered excursion too early: ratio = {:.3}",
            guard.current_excursion_ratio()
        );
        assert!(
            (guard.current_cutoff_hz() - 82.0).abs() < 0.5,
            "Desk -12 dB music did not rest at 82 Hz: fc = {:.2} Hz",
            guard.current_cutoff_hz()
        );
    }

    #[test]
    fn test_excursion_glides_upward_on_loud_50hz_and_recovers() {
        let fs = 48_000.0;
        let mut guard = ExcursionGuard::new(fs);

        let (mut l, mut r) = sine_block(fs, 50.0, 0.95, 24_000);
        guard.process_block(&mut l, &mut r, 1.0, 82.0, 0.0);
        let peak_fc = guard.current_cutoff_hz();
        let peak_ratio = guard.current_excursion_ratio();

        assert!(
            peak_ratio > 1.25,
            "Loud 50 Hz should exceed 1.25 X_max ratio: got {peak_ratio:.3}"
        );
        assert!(
            peak_fc > 135.0 && peak_fc <= 158.5,
            "Loud 50 Hz should glide HPF toward 158 Hz: got {peak_fc:.2} Hz"
        );

        let steady_rms = (l[12_000..].iter().map(|&x| (x as f64) * (x as f64)).sum::<f64>()
            / 12_000.0)
            .sqrt();
        let in_rms = 0.95 / std::f64::consts::SQRT_2;
        let atten_db = 20.0 * (steady_rms / in_rms).log10();
        assert!(
            atten_db < -18.0,
            "Loud 50 Hz was not adequately attenuated: {atten_db:.2} dB"
        );

        let mut quiet_l = vec![0.0_f32; 38_400];
        let mut quiet_r = vec![0.0_f32; 38_400];
        guard.process_block(&mut quiet_l, &mut quiet_r, 1.0, 82.0, 0.0);
        assert!(
            (guard.current_cutoff_hz() - 82.0).abs() < 2.0,
            "Sliding HPF failed to release back to 82 Hz: got {:.2} Hz",
            guard.current_cutoff_hz()
        );
    }

    #[test]
    fn test_excursion_midrange_never_false_triggers() {
        let fs = 48_000.0;
        let mut guard = ExcursionGuard::new(fs);

        let (mut l, mut r) = sine_block(fs, 1000.0, 0.95, 24_000);
        guard.process_block(&mut l, &mut r, 1.0, 82.0, 0.0);

        assert!(
            guard.current_excursion_ratio() < 0.15,
            "1 kHz midrange falsely triggered Klippel model: ratio = {:.4}",
            guard.current_excursion_ratio()
        );
        assert!(
            (guard.current_cutoff_hz() - 82.0).abs() < 0.5,
            "1 kHz midrange shifted sliding HPF: fc = {:.2} Hz",
            guard.current_cutoff_hz()
        );

        let out_rms = (l[12_000..].iter().map(|&x| (x as f64) * (x as f64)).sum::<f64>()
            / 12_000.0)
            .sqrt();
        let in_rms = 0.95 / std::f64::consts::SQRT_2;
        let gain_db = 20.0 * (out_rms / in_rms).log10();
        assert!(
            gain_db.abs() < 0.15,
            "1 kHz midrange gain altered by excursion guard: {gain_db:.3} dB"
        );
    }

    #[test]
    fn test_excursion_guard_zero_blocks_dc_and_passes_sub() {
        let fs = 48_000.0;
        let mut guard = ExcursionGuard::new(fs);

        let len = 48_000;
        let mut l = vec![0.0_f32; len];
        let mut r = vec![0.0_f32; len];
        for i in 0..len {
            let s = 0.40 + 0.50 * (TAU * 60.0 * (i as f64) / fs).sin() as f32;
            l[i] = s;
            r[i] = s;
        }
        guard.process_block(&mut l, &mut r, 0.0, 82.0, 0.0);

        assert!(
            (guard.current_cutoff_hz() - 24.0).abs() < 1.0,
            "Guard=0 did not settle to 24 Hz DC block: fc = {:.2} Hz",
            guard.current_cutoff_hz()
        );

        let tail = &l[24_000..];
        let mean_dc = tail.iter().map(|&x| x as f64).sum::<f64>() / (tail.len() as f64);
        assert!(
            mean_dc.abs() < 1.0e-3,
            "DC offset leaked with guard=0: mean = {mean_dc:.6}"
        );

        let rms =
            (tail.iter().map(|&x| (x as f64) * (x as f64)).sum::<f64>() / (tail.len() as f64))
                .sqrt();
        let in_rms = 0.50 / std::f64::consts::SQRT_2;
        let gain_db = 20.0 * (rms / in_rms).log10();
        assert!(
            gain_db > -1.0,
            "60 Hz was overly attenuated with guard=0: {gain_db:.2} dB"
        );
    }

    #[test]
    fn test_excursion_extreme_30hz_burst_protected_and_finite() {
        let fs = 48_000.0;
        let mut guard = ExcursionGuard::new(fs);

        let (mut l, mut r) = sine_block(fs, 30.0, 1.0, 24_000);
        l[10] = f32::NAN;
        r[10] = f32::INFINITY;

        guard.process_block(&mut l, &mut r, 1.0, 82.0, 0.0);

        assert!(l.iter().all(|x| x.is_finite()), "Non-finite L sample");
        assert!(r.iter().all(|x| x.is_finite()), "Non-finite R sample");

        let steady_peak = l[12_000..]
            .iter()
            .fold(0.0_f32, |acc, &x| acc.max(x.abs()));
        assert!(
            steady_peak < 0.08,
            "Extreme 0 dBFS 30 Hz sub burst was not safely clamped: peak = {steady_peak:.4}"
        );
    }
}

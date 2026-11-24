//! Hard-Real-Time 7-Stage Studio Audio DSP Engine (`StudioDspEngine`).
//!
//! Scientifically ordered zero-allocation signal chain:
//! 1. 3-band equal-loudness missing-fundamental bass & chest warmth (`bass` — taps raw
//!    full-range sub-bass BEFORE any excursion HPF so `55+85 Hz` fundamentals synthesize
//!    rich `110–360 Hz` virtual-pitch harmonics!)
//! 2. Live volume-coupled ISO 226:2023 equal-loudness contour (`loudness`)
//! 3. Uniformly partitioned overlap-save stereo 4096-tap FIR convolver (`convolver`, `B=256`)
//! 4. Phase-coherent 4-band LR4 mastering multiband dynamics (`dynamics`)
//! 5. Post-dynamics nearfield depth & mono-anchored Choueiri FDTR-XTC (`spatial`)
//! 6. Post-spatial `< 215 Hz` LR4 mono-bass anchor + Klippel nonlinear woofer excursion
//!    predictor & dynamic sliding HPF (`excursion` — sees exact post-EQ/post-loudness/post-dynamics
//!    signal scaled by Stage 7 `drive_db` against `X_max = 0.45 mm`!)
//! 7. 4x oversampled Kaiser-Sinc + Catmull-Rom AVX2 true-peak mastering limiter (`limiter`)
//!
//! CRITICAL ACOUSTIC INVARIANTS:
//! - Stage 1 (`bass`) runs on full-range input BEFORE Stage 6 (`excursion`).
//! - Stage 5 (`spatial`) runs STRICTLY AFTER Stage 4 (`dynamics`) and BEFORE Stage 6/7.
//! - Stage 6 (`excursion`) locks `< 215 Hz` mono coherence (`r(<180 Hz) >= +0.992`) and bounds
//!   post-drive Klippel cone excursion strictly below `0.45 mm` `X_max`.

use std::time::Instant;

use crate::bass::BassEngine;
use crate::config::{load_acoustics_bundle, ControlParams, StudioMode};
use crate::convolver::PartitionedConvolver;
use crate::dynamics::MultibandDynamics;
use crate::excursion::ExcursionGuard;
use crate::limiter::TruePeakLimiter;
use crate::loudness::Iso226Loudness;
use crate::simd::FtzDazGuard;
use crate::spatial::SpatialEngine;

/// Fixed preallocated chunk size used for zero-allocation out-of-place & aliased stereo I/O.
pub const ENGINE_CHUNK_SIZE: usize = 256;

struct DspChain {
    bass: BassEngine,
    loudness: Iso226Loudness,
    convolver: PartitionedConvolver,
    dynamics: MultibandDynamics,
    spatial: SpatialEngine,
    excursion: ExcursionGuard,
    limiter: TruePeakLimiter,
    raw_mono: [f32; ENGINE_CHUNK_SIZE],
}

impl DspChain {
    #[inline]
    fn process_block_in_place(
        &mut self,
        left: &mut [f32],
        right: &mut [f32],
        params: &ControlParams,
    ) {
        let total = left.len().min(right.len());
        if total == 0 {
            return;
        }

        // Engage hardware FTZ/DAZ on x86_64 MXCSR for denormal immunity across all stages.
        let _ftz = FtzDazGuard::enter();
        let p = params.sanitized();

        let is_voice = p.mode == StudioMode::Voice;
        let loudness_amount: f32 = match p.mode {
            StudioMode::Deep => 1.0,
            StudioMode::Spatial => 0.90,
            StudioMode::Reference => 0.65,
            StudioMode::Voice => 0.35,
        };
        let ceiling_lin = 10.0_f32.powf(p.ceiling_dbfs / 20.0);

        self.convolver.select_slot(p.mode as usize);
        self.dynamics.set_mode_profile(p.mode as u8);

        let mut offset = 0usize;
        while offset < total {
            let n = (total - offset).min(ENGINE_CHUNK_SIZE);
            let l = &mut left[offset..offset + n];
            let r = &mut right[offset..offset + n];

            // Sanitize any hostile NaN/Inf inputs & capture raw mono for Stage 6 overload glide.
            for i in 0..n {
                let lv = if l[i].is_finite() { l[i] } else { 0.0 };
                let rv = if r[i].is_finite() { r[i] } else { 0.0 };
                l[i] = lv;
                r[i] = rv;
                self.raw_mono[i] = 0.5 * (lv + rv);
            }

            // Stage 1: 3-band missing-fundamental psychoacoustic bass & chest warmth (on raw input!)
            self.bass
                .process_block(l, r, p.bass_depth, p.warmth_body, is_voice);

            // Stage 2: Live volume-coupled ISO 226:2023 equal-loudness contour
            self.loudness
                .process_block(l, r, p.volume_db, loudness_amount);

            // Stage 3: Partitioned overlap-save 4096-tap minimum-phase acoustic FIR correction
            self.convolver.process_block(l, r);

            // Stage 4: Phase-coherent 4-band LR4 mastering multiband dynamics
            self.dynamics.process_block(l, r);

            // Stage 5: Post-dynamics nearfield stage depth & mono-anchored FDTR-XTC
            self.spatial.process_block(l, r, p.stage_depth, p.spatial_xtc);

            // Stage 6: Post-spatial < 215 Hz LR4 mono anchor + Klippel excursion guard (drive-aware!)
            self.excursion.process_block_stage6(
                l,
                r,
                &self.raw_mono[..n],
                p.excursion_guard,
                p.physical_bass_hz,
                p.volume_db,
                p.drive_db,
                p.ceiling_dbfs,
            );

            // Stage 7: 4x oversampled Kaiser-Sinc + Catmull-Rom true-peak mastering limiter
            self.limiter.process_block(l, r, p.drive_db, p.ceiling_dbfs);

            // Final hard-RT output safety guard: zero any non-finite value and enforce hard ceiling
            for i in 0..n {
                let lv = if l[i].is_finite() { l[i] } else { 0.0 };
                let rv = if r[i].is_finite() { r[i] } else { 0.0 };
                l[i] = lv.clamp(-ceiling_lin, ceiling_lin);
                r[i] = rv.clamp(-ceiling_lin, ceiling_lin);
            }

            offset += n;
        }
    }

    fn process_block_profiled(
        &mut self,
        left: &mut [f32],
        right: &mut [f32],
        params: &ControlParams,
    ) -> [u64; 7] {
        let n = left.len().min(right.len()).min(ENGINE_CHUNK_SIZE);
        if n == 0 {
            return [0; 7];
        }
        let l = &mut left[..n];
        let r = &mut right[..n];
        let _ftz = FtzDazGuard::enter();
        let p = params.sanitized();

        for i in 0..n {
            let lv = if l[i].is_finite() { l[i] } else { 0.0 };
            let rv = if r[i].is_finite() { r[i] } else { 0.0 };
            l[i] = lv;
            r[i] = rv;
            self.raw_mono[i] = 0.5 * (lv + rv);
        }

        let is_voice = p.mode == StudioMode::Voice;
        let loudness_amount: f32 = match p.mode {
            StudioMode::Deep => 1.0,
            StudioMode::Spatial => 0.90,
            StudioMode::Reference => 0.65,
            StudioMode::Voice => 0.35,
        };

        let mut ns = [0u64; 7];

        let t0 = Instant::now();
        self.bass
            .process_block(l, r, p.bass_depth, p.warmth_body, is_voice);
        ns[0] = t0.elapsed().as_nanos() as u64;

        let t1 = Instant::now();
        self.loudness
            .process_block(l, r, p.volume_db, loudness_amount);
        ns[1] = t1.elapsed().as_nanos() as u64;

        let t2 = Instant::now();
        self.convolver.select_slot(p.mode as usize);
        self.convolver.process_block(l, r);
        ns[2] = t2.elapsed().as_nanos() as u64;

        let t3 = Instant::now();
        self.dynamics.set_mode_profile(p.mode as u8);
        self.dynamics.process_block(l, r);
        ns[3] = t3.elapsed().as_nanos() as u64;

        let t4 = Instant::now();
        self.spatial.process_block(l, r, p.stage_depth, p.spatial_xtc);
        ns[4] = t4.elapsed().as_nanos() as u64;

        let t5 = Instant::now();
        self.excursion.process_block_stage6(
            l,
            r,
            &self.raw_mono[..n],
            p.excursion_guard,
            p.physical_bass_hz,
            p.volume_db,
            p.drive_db,
            p.ceiling_dbfs,
        );
        ns[5] = t5.elapsed().as_nanos() as u64;

        let t6 = Instant::now();
        self.limiter.process_block(l, r, p.drive_db, p.ceiling_dbfs);
        ns[6] = t6.elapsed().as_nanos() as u64;

        let ceiling_lin = 10.0_f32.powf(p.ceiling_dbfs / 20.0);
        for i in 0..n {
            let lv = if l[i].is_finite() { l[i] } else { 0.0 };
            let rv = if r[i].is_finite() { r[i] } else { 0.0 };
            l[i] = lv.clamp(-ceiling_lin, ceiling_lin);
            r[i] = rv.clamp(-ceiling_lin, ceiling_lin);
        }
        ns
    }

    fn reset(&mut self) {
        self.bass.reset();
        self.loudness.reset();
        self.convolver.reset();
        self.dynamics.reset();
        self.spatial.reset();
        self.excursion.reset();
        self.limiter.reset();
        self.raw_mono.fill(0.0);
    }
}

/// Complete hard-real-time stereo DSP engine for the Dell XPS 17 9720.
pub struct StudioDspEngine {
    sample_rate: f64,
    chain: DspChain,
    scratch_l: [f32; ENGINE_CHUNK_SIZE],
    scratch_r: [f32; ENGINE_CHUNK_SIZE],
    kernel_from_disk: [bool; 4],
    params_from_disk: bool,
}

impl StudioDspEngine {
    /// Creates a new studio DSP engine at `sample_rate` Hz.
    pub fn new(sample_rate: f64) -> Self {
        Self::new_with_fir(sample_rate, None, None)
    }

    /// Creates a new studio DSP engine with an optional explicit FIR impulse override.
    pub fn new_with_fir(
        sample_rate: f64,
        fir_l: Option<&[f32]>,
        fir_r: Option<&[f32]>,
    ) -> Self {
        let fs = if sample_rate.is_finite() && sample_rate >= 8000.0 {
            sample_rate
        } else {
            48000.0
        };

        let bundle = load_acoustics_bundle(fs);

        let mut excursion = ExcursionGuard::new(fs);
        excursion.set_params(bundle.excursion_params);

        let mut bass = BassEngine::new(fs);
        bass.set_params(bundle.bass_params);

        let loudness = Iso226Loudness::new(fs);
        let mut convolver = PartitionedConvolver::new();

        let mut kernel_from_disk = bundle.kernel_from_disk;
        if let Some(override_l) = fir_l {
            let override_r = fir_r.unwrap_or(override_l);
            for mode in StudioMode::ALL {
                convolver.load_impulse_slot(mode as usize, override_l, override_r);
            }
            kernel_from_disk = [true; 4];
        } else {
            for mode in StudioMode::ALL {
                let slot = mode as usize;
                let (ref ir_l, ref ir_r) = bundle.kernels[slot];
                convolver.load_impulse_slot(slot, ir_l, ir_r);
            }
        }
        convolver.select_slot(StudioMode::Deep as usize);
        convolver.set_bypass(false);

        let mut dynamics = MultibandDynamics::new(fs);
        dynamics.set_mode_profile(StudioMode::Deep as u8);

        let spatial = SpatialEngine::new(fs);
        let limiter = TruePeakLimiter::new(fs);

        Self {
            sample_rate: fs,
            chain: DspChain {
                bass,
                loudness,
                convolver,
                dynamics,
                spatial,
                excursion,
                limiter,
                raw_mono: [0.0; ENGINE_CHUNK_SIZE],
            },
            scratch_l: [0.0; ENGINE_CHUNK_SIZE],
            scratch_r: [0.0; ENGINE_CHUNK_SIZE],
            kernel_from_disk,
            params_from_disk: bundle.params_from_disk,
        }
    }

    pub fn load_fir_for_mode(&mut self, mode: StudioMode, ir_l: &[f32], ir_r: &[f32]) {
        self.chain
            .convolver
            .load_impulse_slot(mode as usize, ir_l, ir_r);
        self.chain.convolver.set_bypass(false);
    }

    pub fn load_fir_all_modes(&mut self, ir_l: &[f32], ir_r: &[f32]) {
        for mode in StudioMode::ALL {
            self.chain
                .convolver
                .load_impulse_slot(mode as usize, ir_l, ir_r);
        }
        self.chain.convolver.set_bypass(false);
    }

    #[inline]
    pub fn set_convolver_bypass(&mut self, bypass: bool) {
        self.chain.convolver.set_bypass(bypass);
    }

    #[inline]
    pub fn is_convolver_bypassed(&self) -> bool {
        self.chain.convolver.is_bypassed()
    }

    #[inline]
    pub fn convolver_active_partitions(&self) -> usize {
        self.chain.convolver.active_partitions()
    }

    #[inline]
    pub fn sample_rate(&self) -> f64 {
        self.sample_rate
    }

    #[inline]
    pub fn kernels_loaded_from_disk(&self) -> [bool; 4] {
        self.kernel_from_disk
    }

    #[inline]
    pub fn params_loaded_from_disk(&self) -> bool {
        self.params_from_disk
    }

    pub fn reset(&mut self) {
        self.chain.reset();
        self.scratch_l.fill(0.0);
        self.scratch_r.fill(0.0);
    }

    #[inline]
    pub fn total_latency_samples(&self) -> usize {
        self.chain.convolver.latency_samples() + self.chain.limiter.latency_samples()
    }

    #[inline]
    pub fn process_block(&mut self, left: &mut [f32], right: &mut [f32], params: &ControlParams) {
        self.chain.process_block_in_place(left, right, params);
    }

    /// Profiles per-stage execution time in nanoseconds across all 7 stages (used by `cli bench`).
    #[inline]
    pub fn process_block_profiled(
        &mut self,
        left: &mut [f32],
        right: &mut [f32],
        params: &ControlParams,
    ) -> [u64; 7] {
        self.chain.process_block_profiled(left, right, params)
    }

    #[inline]
    pub fn process_stereo(
        &mut self,
        in_l: &[f32],
        in_r: &[f32],
        out_l: &mut [f32],
        out_r: &mut [f32],
        params: &ControlParams,
    ) {
        let total = in_l.len().min(in_r.len()).min(out_l.len()).min(out_r.len());
        let mut offset = 0usize;
        while offset < total {
            let chunk = (total - offset).min(ENGINE_CHUNK_SIZE);
            self.scratch_l[..chunk].copy_from_slice(&in_l[offset..offset + chunk]);
            self.scratch_r[..chunk].copy_from_slice(&in_r[offset..offset + chunk]);
            self.chain.process_block_in_place(
                &mut self.scratch_l[..chunk],
                &mut self.scratch_r[..chunk],
                params,
            );
            out_l[offset..offset + chunk].copy_from_slice(&self.scratch_l[..chunk]);
            out_r[offset..offset + chunk].copy_from_slice(&self.scratch_r[..chunk]);
            offset += chunk;
        }
    }

    /// Raw-pointer stereo processing entry point for C/LADSPA hosts.
    ///
    /// # Safety
    /// Caller must ensure `in_l`, `in_r` are valid for reads of `frames` `f32` values and
    /// `out_l`, `out_r` are valid for writes of `frames` `f32` values.
    #[inline]
    pub unsafe fn process_raw_ptrs(
        &mut self,
        in_l: *const f32,
        in_r: *const f32,
        out_l: *mut f32,
        out_r: *mut f32,
        frames: usize,
        params: &ControlParams,
    ) {
        let mut offset = 0usize;
        while offset < frames {
            let chunk = (frames - offset).min(ENGINE_CHUNK_SIZE);
            core::ptr::copy_nonoverlapping(in_l.add(offset), self.scratch_l.as_mut_ptr(), chunk);
            core::ptr::copy_nonoverlapping(in_r.add(offset), self.scratch_r.as_mut_ptr(), chunk);

            self.chain.process_block_in_place(
                &mut self.scratch_l[..chunk],
                &mut self.scratch_r[..chunk],
                params,
            );

            core::ptr::copy_nonoverlapping(self.scratch_l.as_ptr(), out_l.add(offset), chunk);
            core::ptr::copy_nonoverlapping(self.scratch_r.as_ptr(), out_r.add(offset), chunk);
            offset += chunk;
        }
    }

    // ------------------------------------------------------------------------
    // Real-Time Safe Telemetry Getters
    // ------------------------------------------------------------------------

    #[inline]
    pub fn excursion_cutoff_hz(&self) -> f64 {
        self.chain.excursion.current_cutoff_hz()
    }

    #[inline]
    pub fn excursion_ratio(&self) -> f64 {
        self.chain.excursion.current_excursion_ratio()
    }

    #[inline]
    pub fn music_gate(&self) -> f32 {
        self.chain.bass.last_music_gate()
    }

    #[inline]
    pub fn loudness_bass_boost_db(&self) -> f64 {
        self.chain.loudness.current_bass_boost_db()
    }

    #[inline]
    pub fn loudness_air_boost_db(&self) -> f64 {
        self.chain.loudness.current_air_boost_db()
    }

    #[inline]
    pub fn limiter_gr_db(&self) -> f32 {
        self.chain.limiter.last_gain_reduction_db()
    }

    #[inline]
    pub fn limiter_tp_dbfs(&self) -> f32 {
        self.chain.limiter.last_true_peak_dbfs()
    }

    #[inline]
    pub fn band_gr_db(&self) -> [f32; 4] {
        self.chain.dynamics.band_gain_reduction_db()
    }
}

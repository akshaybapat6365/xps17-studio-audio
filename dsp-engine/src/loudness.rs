//! Live Volume-Coupled ISO 226:2023 Equal-Loudness Contour.
//!
//! Compensates for the level-dependent decline in human cochlear sensitivity at low
//! frequencies (`100–240 Hz` warmth body) and upper treble (`6–12 kHz` air) during
//! low-to-moderate nearfield desktop playback on the Dell XPS 17 9720.
//!
//! Filter topology:
//! - Low-frequency warmth shelf: Stereo Cytomic ZDF SVF LowShelf @ `165 Hz, Q = 0.68`
//! - Upper-air presence shelf: Stereo Cytomic ZDF SVF HighShelf @ `7500 Hz, Q = 0.70`
//! - Fast branchless steady-state stereo vector path (`0` transcendental calls per block when
//!   `volume_db` is stationary) + smooth `18 ms` exponential ramp during live slider motion.
//!
//! References:
//! - ISO 226:2023, *Acoustics — Normal equal-loudness-level contours* (3rd ed., 2023).
//! - Suzuki, Y. & Takeshima, H. (2004), "Equal-loudness-level contours for pure tones",
//!   *J. Acoust. Soc. Am.*, 116(2), 918–933.
//! - Simper, A. (2013), *Linear Trapezoidal Integrated State Variable Filter (SVF)*, Cytomic.

use crate::simd::FtzDazGuard;
use crate::svf::StereoSvfF32;

const BASS_SHELF_HZ: f64 = 165.0;
const BASS_SHELF_Q: f64 = 0.68;
const AIR_SHELF_HZ: f64 = 7500.0;
const AIR_SHELF_Q: f64 = 0.70;
const SMOOTH_TAU_S: f64 = 0.018;
const COEFF_UPDATE_STRIDE: usize = 8;

/// Live volume-coupled ISO 226:2023 equal-loudness processor.
#[derive(Debug, Clone)]
pub struct Iso226Loudness {
    sample_rate: f64,
    smooth_coeff: f64,
    smoothed_vol_db: f64,
    smoothed_amount: f64,
    bass_boost_db: f64,
    air_boost_db: f64,
    applied_bass_db: f64,
    applied_air_db: f64,
    initialized: bool,
    low_shelf: StereoSvfF32,
    air_shelf: StereoSvfF32,
}

impl Iso226Loudness {
    /// Creates a new ISO 226:2023 equal-loudness contour processor at `sample_rate` Hz.
    pub fn new(sample_rate: f64) -> Self {
        let fs = if sample_rate.is_finite() && sample_rate >= 8000.0 {
            sample_rate
        } else {
            48000.0
        };
        let smooth_coeff = 1.0 - (-1.0 / (SMOOTH_TAU_S * fs)).exp();

        let mut low_shelf = StereoSvfF32::new(fs);
        let mut air_shelf = StereoSvfF32::new(fs);
        low_shelf.set_lowshelf(BASS_SHELF_HZ, BASS_SHELF_Q, 0.0);
        air_shelf.set_highshelf(AIR_SHELF_HZ, AIR_SHELF_Q, 0.0);

        Self {
            sample_rate: fs,
            smooth_coeff,
            smoothed_vol_db: 0.0,
            smoothed_amount: 1.0,
            bass_boost_db: 0.0,
            air_boost_db: 0.0,
            applied_bass_db: 0.0,
            applied_air_db: 0.0,
            initialized: false,
            low_shelf,
            air_shelf,
        }
    }

    /// Resets filter states and parameter smoothers without reallocating.
    pub fn reset(&mut self) {
        self.low_shelf.reset();
        self.air_shelf.reset();
        self.smoothed_vol_db = 0.0;
        self.smoothed_amount = 1.0;
        self.bass_boost_db = 0.0;
        self.air_boost_db = 0.0;
        self.applied_bass_db = 0.0;
        self.applied_air_db = 0.0;
        self.initialized = false;
        self.low_shelf.set_lowshelf(BASS_SHELF_HZ, BASS_SHELF_Q, 0.0);
        self.air_shelf.set_highshelf(AIR_SHELF_HZ, AIR_SHELF_Q, 0.0);
    }

    /// Evaluates the monotonic ISO 226:2023 bass and air shelf boosts in dB.
    #[inline]
    fn compute_boosts(vol_db: f64, amount: f64) -> (f64, f64) {
        let atten = (-vol_db).clamp(0.0, 36.0);
        let amt = amount.clamp(0.0, 1.5);
        if atten <= 0.0 || amt <= 0.0 {
            return (0.0, 0.0);
        }
        let norm = atten / 30.0;
        let bass = (9.5 * norm.powf(1.12)).min(10.0) * amt;
        let air = (2.2 * norm.powf(1.15)).min(2.5) * amt;
        (bass, air)
    }

    #[inline]
    fn maybe_update_filters(&mut self, force: bool) {
        let bass_delta = (self.bass_boost_db - self.applied_bass_db).abs();
        let air_delta = (self.air_boost_db - self.applied_air_db).abs();
        if force || bass_delta > 1e-4 {
            let b_hz = BASS_SHELF_HZ.min(0.42 * self.sample_rate);
            self.low_shelf
                .set_lowshelf(b_hz, BASS_SHELF_Q, self.bass_boost_db);
            self.applied_bass_db = self.bass_boost_db;
        }
        if force || air_delta > 1e-4 {
            let a_hz = AIR_SHELF_HZ.min(0.42 * self.sample_rate);
            self.air_shelf
                .set_highshelf(a_hz, AIR_SHELF_Q, self.air_boost_db);
            self.applied_air_db = self.air_boost_db;
        }
    }

    /// Processes a stereo audio block in-place with live smoothed ISO 226:2023 shelves.
    pub fn process_block(
        &mut self,
        left: &mut [f32],
        right: &mut [f32],
        volume_db: f32,
        amount: f32,
    ) {
        let len = left.len().min(right.len());
        if len == 0 {
            return;
        }
        let _ftz = FtzDazGuard::enter();

        let target_vol = if volume_db.is_finite() {
            (volume_db as f64).clamp(-45.0, 0.0)
        } else {
            -12.0
        };
        let target_amt = if amount.is_finite() {
            (amount as f64).clamp(0.0, 1.5)
        } else {
            1.0
        };

        if !self.initialized {
            self.smoothed_vol_db = target_vol;
            self.smoothed_amount = target_amt;
            let (b, a) = Self::compute_boosts(self.smoothed_vol_db, self.smoothed_amount);
            self.bass_boost_db = b;
            self.air_boost_db = a;
            self.maybe_update_filters(true);
            self.initialized = true;
        }

        let stationary = (target_vol - self.smoothed_vol_db).abs() < 1e-5
            && (target_amt - self.smoothed_amount).abs() < 1e-5;

        if stationary {
            // Fast branchless steady-state path (zero transcendental calls!).
            if self.applied_bass_db.abs() < 1e-6 && self.applied_air_db.abs() < 1e-6 {
                // Flat unity (e.g., 0 dB volume or 0.0 amount): keep SVF state warm seamlessly
                for i in 0..len {
                    let y = self
                        .air_shelf
                        .process(self.low_shelf.process([left[i], right[i]]));
                    left[i] = y[0];
                    right[i] = y[1];
                }
            } else {
                for i in 0..len {
                    let y = self
                        .air_shelf
                        .process(self.low_shelf.process([left[i], right[i]]));
                    left[i] = y[0];
                    right[i] = y[1];
                }
            }
            return;
        }

        let c = self.smooth_coeff;
        for i in 0..len {
            let prev_vol = self.smoothed_vol_db;
            let prev_amt = self.smoothed_amount;
            self.smoothed_vol_db += c * (target_vol - prev_vol);
            self.smoothed_amount += c * (target_amt - prev_amt);

            if (i & (COEFF_UPDATE_STRIDE - 1)) == 0 {
                let (b, a) = Self::compute_boosts(self.smoothed_vol_db, self.smoothed_amount);
                self.bass_boost_db = b;
                self.air_boost_db = a;
                self.maybe_update_filters(false);
            }

            let y = self
                .air_shelf
                .process(self.low_shelf.process([left[i], right[i]]));
            left[i] = y[0];
            right[i] = y[1];
        }

        let (b, a) = Self::compute_boosts(self.smoothed_vol_db, self.smoothed_amount);
        self.bass_boost_db = b;
        self.air_boost_db = a;
        self.maybe_update_filters(false);
    }

    /// Returns the current active low-frequency warmth boost in dB.
    #[inline]
    pub fn current_bass_boost_db(&self) -> f64 {
        self.bass_boost_db
    }

    /// Returns the current active upper-air presence boost in dB.
    #[inline]
    pub fn current_air_boost_db(&self) -> f64 {
        self.air_boost_db
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use core::f64::consts::TAU;

    fn measure_sine_gain_db(
        loud: &mut Iso226Loudness,
        freq_hz: f64,
        vol_db: f32,
        amount: f32,
    ) -> f64 {
        loud.reset();
        let fs = 48000.0;
        let total = 9600;
        let settle = 4800;
        let mut l = vec![0.0f32; total];
        let mut r = vec![0.0f32; total];
        for i in 0..total {
            let s = (TAU * freq_hz * (i as f64) / fs).sin() as f32 * 0.25;
            l[i] = s;
            r[i] = s;
        }
        let in_rms = (l[settle..]
            .iter()
            .map(|&v| (v as f64) * (v as f64))
            .sum::<f64>()
            / ((total - settle) as f64))
            .sqrt();

        loud.process_block(&mut l, &mut r, vol_db, amount);

        let out_rms = (l[settle..]
            .iter()
            .map(|&v| (v as f64) * (v as f64))
            .sum::<f64>()
            / ((total - settle) as f64))
            .sqrt();
        20.0 * (out_rms / in_rms).log10()
    }

    #[test]
    fn test_iso226_gain_curve_targets_and_monotonicity() {
        let mut loud = Iso226Loudness::new(48000.0);
        let mut l = [0.0f32; 8];
        let mut r = [0.0f32; 8];

        let points: &[(f32, f64, f64, f64)] = &[
            (0.0, 0.0, 0.0, 1e-6),
            (-6.0, 1.566, 0.345, 0.05),
            (-12.0, 3.405, 0.767, 0.05),
            (-30.0, 9.500, 2.200, 0.01),
            (-36.0, 10.000, 2.500, 0.01),
            (-45.0, 10.000, 2.500, 0.01),
        ];

        let mut prev_bass = -1.0;
        let mut prev_air = -1.0;
        for &(vol, exp_bass, exp_air, tol) in points {
            loud.reset();
            loud.process_block(&mut l, &mut r, vol, 1.0);
            let b = loud.current_bass_boost_db();
            let a = loud.current_air_boost_db();
            assert!(
                (b - exp_bass).abs() <= tol,
                "vol={vol}: bass={b:.4}, expected={exp_bass:.4}"
            );
            assert!(
                (a - exp_air).abs() <= tol,
                "vol={vol}: air={a:.4}, expected={exp_air:.4}"
            );
            assert!(b >= prev_bass - 1e-9);
            assert!(a >= prev_air - 1e-9);
            prev_bass = b;
            prev_air = a;
        }

        loud.reset();
        loud.process_block(&mut l, &mut r, -30.0, 0.0);
        assert!(loud.current_bass_boost_db().abs() < 1e-9);
        assert!(loud.current_air_boost_db().abs() < 1e-9);
    }

    #[test]
    fn test_iso226_acoustic_shelf_response() {
        let mut loud = Iso226Loudness::new(48000.0);

        for &f in &[80.0, 165.0, 1000.0, 7500.0, 12000.0] {
            let g = measure_sine_gain_db(&mut loud, f, 0.0, 1.0);
            assert!(
                g.abs() < 0.02,
                "0 dB vol at {f} Hz should be flat, got {g:.4} dB"
            );
        }

        let g_80 = measure_sine_gain_db(&mut loud, 80.0, -30.0, 1.0);
        let g_1k = measure_sine_gain_db(&mut loud, 1000.0, -30.0, 1.0);
        let g_12k = measure_sine_gain_db(&mut loud, 12000.0, -30.0, 1.0);

        assert!(
            g_80 > 7.0 && g_80 < 10.2,
            "80 Hz bass boost at -30 dB out of range: {g_80:.3} dB"
        );
        assert!(
            g_1k.abs() < 0.8,
            "1 kHz reference should stay near 0 dB, got {g_1k:.3} dB"
        );
        assert!(
            g_12k > 1.3 && g_12k < 2.6,
            "12 kHz air boost at -30 dB out of range: {g_12k:.3} dB"
        );
    }

    #[test]
    fn test_volume_smoothing_zipper_free() {
        let mut loud = Iso226Loudness::new(48000.0);
        let n = 4800;
        let mut l = vec![0.0f32; n];
        let mut r = vec![0.0f32; n];
        for i in 0..n {
            let s = (TAU * 120.0 * (i as f64) / 48000.0).sin() as f32 * 0.1;
            l[i] = s;
            r[i] = s;
        }
        loud.process_block(&mut l[..240], &mut r[..240], 0.0, 1.0);
        assert!(loud.current_bass_boost_db().abs() < 1e-6);

        loud.process_block(&mut l[240..1104], &mut r[240..1104], -30.0, 1.0);
        let mid_boost = loud.current_bass_boost_db();
        assert!(
            mid_boost > 4.5 && mid_boost < 7.0,
            "Expected ~5.6 dB after 1 tau, got {mid_boost:.3} dB"
        );

        loud.process_block(&mut l[1104..], &mut r[1104..], -30.0, 1.0);
        assert!((loud.current_bass_boost_db() - 9.5).abs() < 0.15);

        let mut max_diff = 0.0f32;
        for i in 1..n {
            max_diff = max_diff.max((l[i] - l[i - 1]).abs());
        }
        assert!(
            max_diff < 0.04,
            "Detected click/discontinuity: max_diff={max_diff}"
        );
    }
}

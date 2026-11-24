//! Dell XPS 17 9720 Studio Audio DSP Engine
//!
//! Hard-real-time audio engine combining:
//! - Hand-tuned x86_64 AVX2/FMA GNU Assembly kernels (`asm/xps17_avx2_kernels.S`)
//! - Cytomic Zero-Delay-Feedback State-Variable Filters (`svf`)
//! - Klippel mechanical woofer excursion predictor & dynamic sliding HPF (`excursion`)
//! - 3-band equal-loudness missing-fundamental bass & chest warmth (`bass`)
//! - Live volume-coupled ISO 226:2023 equal-loudness contour (`loudness`)
//! - Uniformly partitioned overlap-save stereo FIR convolver (`convolver`)
//! - Phase-coherent 4-band LR4 mastering multiband dynamics (`dynamics`)
//! - Post-dynamics nearfield depth & mono-anchored Choueiri FDTR-XTC (`spatial`)
//! - 4x oversampled Catmull-Rom AVX2 true-peak mastering limiter (`limiter`)
//! - LADSPA v1.1 hard-RT shared library ABI (`ladspa`)

pub mod simd;
pub mod svf;
pub mod excursion;
pub mod bass;
pub mod convolver;
pub mod loudness;
pub mod dynamics;
pub mod spatial;
pub mod limiter;
pub mod config;
pub mod engine;
pub mod ladspa;

pub use config::{ControlParams, StudioMode};
pub use engine::StudioDspEngine;

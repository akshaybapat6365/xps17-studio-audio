//! Hard-Real-Time Zero-Allocation Integration Proof (`tests/zero_alloc.rs`).
//!
//! Uses a custom `#[global_allocator]` wrapping `std::alloc::System` to prove that
//! `StudioDspEngine::process_block`, `StudioDspEngine::process_stereo`, and the LADSPA
//! `run()` C callback perform EXACTLY ZERO heap allocations (`alloc`, `alloc_zeroed`,
//! `dealloc`, `realloc`) across all 4 studio modes, live mode switching, dynamic
//! parameter automation, and in-place buffer aliasing.

use std::alloc::{GlobalAlloc, Layout, System};
use std::f32::consts::TAU;
use std::sync::atomic::{compiler_fence, AtomicBool, AtomicUsize, Ordering};
use std::sync::Mutex;

use xps17_studio_dsp::config::{ControlParams, StudioMode};
use xps17_studio_dsp::engine::StudioDspEngine;
use xps17_studio_dsp::ladspa::ladspa_descriptor;

struct CountingAlloc;

static ARMED: AtomicBool = AtomicBool::new(false);
static ALLOC_CALLS: AtomicUsize = AtomicUsize::new(0);
static ALLOC_ZEROED_CALLS: AtomicUsize = AtomicUsize::new(0);
static DEALLOC_CALLS: AtomicUsize = AtomicUsize::new(0);
static REALLOC_CALLS: AtomicUsize = AtomicUsize::new(0);

/// Serializes global-allocator tests so concurrent Cargo test threads cannot
/// trigger unrelated allocations while another test has `ARMED == true`.
static ALLOC_TEST_LOCK: Mutex<()> = Mutex::new(());

unsafe impl GlobalAlloc for CountingAlloc {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        if ARMED.load(Ordering::Relaxed) {
            ALLOC_CALLS.fetch_add(1, Ordering::Relaxed);
        }
        System.alloc(layout)
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        if ARMED.load(Ordering::Relaxed) {
            ALLOC_ZEROED_CALLS.fetch_add(1, Ordering::Relaxed);
        }
        System.alloc_zeroed(layout)
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        if ARMED.load(Ordering::Relaxed) {
            DEALLOC_CALLS.fetch_add(1, Ordering::Relaxed);
        }
        System.dealloc(ptr, layout)
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        if ARMED.load(Ordering::Relaxed) {
            REALLOC_CALLS.fetch_add(1, Ordering::Relaxed);
        }
        System.realloc(ptr, layout, new_size)
    }
}

#[global_allocator]
static GLOBAL_ALLOC: CountingAlloc = CountingAlloc;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct AllocStats {
    alloc: usize,
    alloc_zeroed: usize,
    dealloc: usize,
    realloc: usize,
}

impl AllocStats {
    fn total(&self) -> usize {
        self.alloc + self.alloc_zeroed + self.dealloc + self.realloc
    }
}

fn arm() {
    ALLOC_CALLS.store(0, Ordering::SeqCst);
    ALLOC_ZEROED_CALLS.store(0, Ordering::SeqCst);
    DEALLOC_CALLS.store(0, Ordering::SeqCst);
    REALLOC_CALLS.store(0, Ordering::SeqCst);
    compiler_fence(Ordering::SeqCst);
    ARMED.store(true, Ordering::SeqCst);
}

fn disarm() -> AllocStats {
    ARMED.store(false, Ordering::SeqCst);
    compiler_fence(Ordering::SeqCst);
    AllocStats {
        alloc: ALLOC_CALLS.load(Ordering::SeqCst),
        alloc_zeroed: ALLOC_ZEROED_CALLS.load(Ordering::SeqCst),
        dealloc: DEALLOC_CALLS.load(Ordering::SeqCst),
        realloc: REALLOC_CALLS.load(Ordering::SeqCst),
    }
}

#[test]
fn zero_alloc_process_block_all_modes_2000_blocks() {
    let _guard = ALLOC_TEST_LOCK.lock().unwrap();

    const SR: f64 = 48_000.0;
    const BLOCK: usize = 128;
    const BLOCKS: usize = 2000;

    let mut engine = StudioDspEngine::new(SR);
    let mut l = [0.0f32; BLOCK];
    let mut r = [0.0f32; BLOCK];

    let mut non_finite = 0usize;
    let mut energy = 0.0f64;

    arm();
    for blk in 0..BLOCKS {
        // Live mode switch every 125 blocks across all 4 modes
        let mode = StudioMode::ALL[(blk / 125) % 4];
        let mut params = ControlParams::for_mode(mode);

        // Continuously sweep every control parameter across its full LADSPA range
        let frac = (blk % 250) as f32 / 249.0;
        params.volume_db = -45.0 + 45.0 * frac;
        params.bass_depth = 1.5 * frac;
        params.physical_bass_hz = 65.0 + 115.0 * (1.0 - frac);
        params.warmth_body = 1.5 * (1.0 - frac);
        params.stage_depth = 1.5 * frac;
        params.spatial_xtc = if mode == StudioMode::Spatial { frac } else { 0.0 };
        params.drive_db = 12.0 * frac;
        params.ceiling_dbfs = -1.0;
        params.excursion_guard = 1.0;

        for i in 0..BLOCK {
            let t = ((blk * BLOCK + i) as f32) / (SR as f32);
            l[i] = 0.35 * (TAU * 70.0 * t).sin() + 0.25 * (TAU * 880.0 * t).sin();
            r[i] = 0.35 * (TAU * 70.0 * t).sin() + 0.25 * (TAU * 1320.0 * t).cos();
        }

        engine.process_block(&mut l, &mut r, &params);

        for i in 0..BLOCK {
            if !l[i].is_finite() || !r[i].is_finite() {
                non_finite += 1;
            }
            energy += (l[i] as f64) * (l[i] as f64) + (r[i] as f64) * (r[i] as f64);
        }
    }
    let stats = disarm();

    assert_eq!(
        stats.total(),
        0,
        "HARD-RT VIOLATION in process_block: {:?}",
        stats
    );
    assert_eq!(non_finite, 0, "process_block emitted non-finite samples");
    assert!(energy > 10.0, "process_block produced near-silent output");
}

#[test]
fn zero_alloc_process_stereo_variable_chunks_2000_blocks() {
    let _guard = ALLOC_TEST_LOCK.lock().unwrap();

    const SR: f64 = 48_000.0;
    const MAX_CHUNK: usize = 512;
    const CHUNKS: [usize; 4] = [64, 128, 197, 512];
    const BLOCKS: usize = 2000;

    let mut engine = StudioDspEngine::new(SR);
    let mut in_l = [0.0f32; MAX_CHUNK];
    let mut in_r = [0.0f32; MAX_CHUNK];
    let mut out_l = [0.0f32; MAX_CHUNK];
    let mut out_r = [0.0f32; MAX_CHUNK];

    let mut non_finite = 0usize;
    let mut sample_idx = 0usize;

    arm();
    for blk in 0..BLOCKS {
        let n = CHUNKS[blk % CHUNKS.len()];
        let mode = StudioMode::ALL[blk % 4];
        let params = ControlParams::for_mode(mode);

        for i in 0..n {
            let t = ((sample_idx + i) as f32) / (SR as f32);
            in_l[i] = 0.30 * (TAU * 85.0 * t).sin() + 0.18 * (TAU * 2400.0 * t).sin();
            in_r[i] = 0.30 * (TAU * 85.0 * t).sin() + 0.18 * (TAU * 3100.0 * t).cos();
        }
        sample_idx += n;

        engine.process_stereo(
            &in_l[..n],
            &in_r[..n],
            &mut out_l[..n],
            &mut out_r[..n],
            &params,
        );

        for i in 0..n {
            if !out_l[i].is_finite() || !out_r[i].is_finite() {
                non_finite += 1;
            }
        }
    }
    let stats = disarm();

    assert_eq!(
        stats.total(),
        0,
        "HARD-RT VIOLATION in process_stereo: {:?}",
        stats
    );
    assert_eq!(non_finite, 0, "process_stereo emitted non-finite samples");
}

#[test]
fn zero_alloc_ladspa_run_out_of_place_and_aliased() {
    let _guard = ALLOC_TEST_LOCK.lock().unwrap();

    const BLOCK: usize = 128;

    unsafe {
        let desc = ladspa_descriptor(0);
        assert!(!desc.is_null(), "ladspa_descriptor(0) must be non-null");
        assert!(
            ladspa_descriptor(1).is_null(),
            "ladspa_descriptor(1) must be null"
        );

        let d = &*desc;
        let inst = (d.instantiate.unwrap())(desc, 48_000);
        assert!(!inst.is_null());

        let mut in_l = [0.0f32; BLOCK];
        let mut in_r = [0.0f32; BLOCK];
        let mut out_l = [0.0f32; BLOCK];
        let mut out_r = [0.0f32; BLOCK];

        let mut mode: f32 = 0.0;
        let mut volume_db: f32 = -12.0;
        let mut bass_depth: f32 = 1.0;
        let mut phys_bass_hz: f32 = 82.0;
        let mut warmth: f32 = 1.0;
        let mut stage: f32 = 0.85;
        let mut xtc: f32 = 0.75;
        let mut drive: f32 = 7.0;
        let mut ceiling: f32 = -1.0;
        let mut excursion: f32 = 1.0;

        let connect = d.connect_port.unwrap();
        connect(inst, 0, in_l.as_mut_ptr());
        connect(inst, 1, in_r.as_mut_ptr());
        connect(inst, 2, out_l.as_mut_ptr());
        connect(inst, 3, out_r.as_mut_ptr());
        let mode_ptr: *mut f32 = &mut mode;
        connect(inst, 4, mode_ptr);
        connect(inst, 5, &mut volume_db);
        connect(inst, 6, &mut bass_depth);
        connect(inst, 7, &mut phys_bass_hz);
        connect(inst, 8, &mut warmth);
        connect(inst, 9, &mut stage);
        connect(inst, 10, &mut xtc);
        connect(inst, 11, &mut drive);
        connect(inst, 12, &mut ceiling);
        connect(inst, 13, &mut excursion);

        (d.activate.unwrap())(inst);
        let run = d.run.unwrap();

        let mut non_finite = 0usize;

        arm();
        // 250 blocks out-of-place across all 4 modes
        for blk in 0..250usize {
            *mode_ptr = (blk % 4) as f32;
            for i in 0..BLOCK {
                let t = ((blk * BLOCK + i) as f32) / 48_000.0;
                in_l[i] = 0.25 * (TAU * 100.0 * t).sin();
                in_r[i] = 0.25 * (TAU * 440.0 * t).cos();
            }
            run(inst, BLOCK as libc::c_ulong);
            for i in 0..BLOCK {
                if !out_l[i].is_finite() || !out_r[i].is_finite() {
                    non_finite += 1;
                }
            }
        }

        // Reconnect for in-place aliasing (Out == In) and run 250 more blocks
        connect(inst, 2, in_l.as_mut_ptr());
        connect(inst, 3, in_r.as_mut_ptr());
        for blk in 0..250usize {
            *mode_ptr = (blk % 4) as f32;
            for i in 0..BLOCK {
                let t = ((blk * BLOCK + i) as f32) / 48_000.0;
                in_l[i] = 0.25 * (TAU * 120.0 * t).sin();
                in_r[i] = 0.25 * (TAU * 520.0 * t).cos();
            }
            run(inst, BLOCK as libc::c_ulong);
            for i in 0..BLOCK {
                if !in_l[i].is_finite() || !in_r[i].is_finite() {
                    non_finite += 1;
                }
            }
        }
        let stats = disarm();

        (d.deactivate.unwrap())(inst);
        (d.cleanup.unwrap())(inst);

        assert_eq!(
            stats.total(),
            0,
            "HARD-RT VIOLATION in LADSPA run(): {:?}",
            stats
        );
        assert_eq!(non_finite, 0, "LADSPA run() emitted non-finite samples");
    }
}

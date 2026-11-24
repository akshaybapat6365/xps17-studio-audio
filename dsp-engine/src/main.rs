//! `xps17-studio-cli`: Offline Renderer, SIMD/RT Benchmark Suite,
//! Impulse Response Dumper, and Automated Acoustic/Safety Verifier
//! for the Dell XPS 17 9720 Studio Audio DSP Engine.

use std::alloc::{GlobalAlloc, Layout, System};
use std::env;
use std::f32::consts::TAU;
use std::process;
use std::sync::atomic::{compiler_fence, AtomicBool, AtomicUsize, Ordering};
use std::time::Instant;

use xps17_studio_dsp::config::{read_wav_stereo, write_wav_stereo_f32, ControlParams, StudioMode};
use xps17_studio_dsp::engine::StudioDspEngine;
use xps17_studio_dsp::simd::{has_avx2_fma, run_simd_benchmarks, true_peak_4x_f32};

// ============================================================================
// Counting Global Allocator for Live Hard-RT Zero-Allocation Proof
// ============================================================================

struct CountingAlloc;

static COUNTING_ACTIVE: AtomicBool = AtomicBool::new(false);
static RT_ALLOC_COUNT: AtomicUsize = AtomicUsize::new(0);

unsafe impl GlobalAlloc for CountingAlloc {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        if COUNTING_ACTIVE.load(Ordering::Relaxed) {
            RT_ALLOC_COUNT.fetch_add(1, Ordering::Relaxed);
        }
        System.alloc(layout)
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        if COUNTING_ACTIVE.load(Ordering::Relaxed) {
            RT_ALLOC_COUNT.fetch_add(1, Ordering::Relaxed);
        }
        System.alloc_zeroed(layout)
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        if COUNTING_ACTIVE.load(Ordering::Relaxed) {
            RT_ALLOC_COUNT.fetch_add(1, Ordering::Relaxed);
        }
        System.dealloc(ptr, layout)
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        if COUNTING_ACTIVE.load(Ordering::Relaxed) {
            RT_ALLOC_COUNT.fetch_add(1, Ordering::Relaxed);
        }
        System.realloc(ptr, layout, new_size)
    }
}

#[global_allocator]
static GLOBAL_ALLOC: CountingAlloc = CountingAlloc;

fn arm_alloc_counter() {
    RT_ALLOC_COUNT.store(0, Ordering::SeqCst);
    compiler_fence(Ordering::SeqCst);
    COUNTING_ACTIVE.store(true, Ordering::SeqCst);
}

fn disarm_alloc_counter() -> usize {
    COUNTING_ACTIVE.store(false, Ordering::SeqCst);
    compiler_fence(Ordering::SeqCst);
    RT_ALLOC_COUNT.load(Ordering::SeqCst)
}

// ============================================================================
// CLI Entry Point & Argument Parser
// ============================================================================

fn print_usage() {
    eprintln!(
        "xps17-studio-cli — Dell XPS 17 9720 Studio Audio DSP Engine\n\
         \n\
         USAGE:\n\
           xps17-studio-cli render --in <in.wav> --out <out.wav> --mode <deep|spatial|reference|voice>\n\
                                   [--volume-db -12] [--fir <kernel.wav>] [--bass-depth F]\n\
                                   [--physical-bass-hz F] [--warmth-body F] [--stage-depth F]\n\
                                   [--spatial-xtc F] [--drive-db F] [--ceiling-dbfs F]\n\
                                   [--excursion-guard F]\n\
           xps17-studio-cli bench\n\
           xps17-studio-cli impulse --out <ir.wav> --mode <deep|spatial|reference|voice>\n\
                                    [--length 8192] [--volume-db -12] [--fir <kernel.wav>]\n\
           xps17-studio-cli verify\n"
    );
}

fn main() {
    let args: Vec<String> = env::args().skip(1).collect();
    if args.is_empty() {
        print_usage();
        process::exit(2);
    }

    let res = match args[0].as_str() {
        "render" => cmd_render(&args[1..]),
        "bench" => cmd_bench(),
        "impulse" => cmd_impulse(&args[1..]),
        "verify" => cmd_verify(),
        "-h" | "--help" | "help" => {
            print_usage();
            Ok(())
        }
        other => Err(format!("unknown subcommand '{}'", other)),
    };

    if let Err(err) = res {
        eprintln!("ERROR: {}", err);
        process::exit(1);
    }
}

fn next_arg<'a>(args: &'a [String], i: &mut usize, flag: &str) -> Result<&'a str, String> {
    *i += 1;
    args.get(*i)
        .map(|s| s.as_str())
        .ok_or_else(|| format!("missing value for {}", flag))
}

fn parse_f32_arg(args: &[String], i: &mut usize, flag: &str) -> Result<f32, String> {
    let s = next_arg(args, i, flag)?;
    s.parse::<f32>()
        .map_err(|_| format!("invalid float '{}' for {}", s, flag))
}

// ============================================================================
// Subcommand 1: `render`
// ============================================================================

fn cmd_render(args: &[String]) -> Result<(), String> {
    let mut in_path: Option<String> = None;
    let mut out_path: Option<String> = None;
    let mut fir_path: Option<String> = None;
    let mut mode = StudioMode::Deep;

    let mut ov_volume_db: Option<f32> = None;
    let mut ov_bass_depth: Option<f32> = None;
    let mut ov_physical_bass_hz: Option<f32> = None;
    let mut ov_warmth_body: Option<f32> = None;
    let mut ov_stage_depth: Option<f32> = None;
    let mut ov_spatial_xtc: Option<f32> = None;
    let mut ov_drive_db: Option<f32> = None;
    let mut ov_ceiling_dbfs: Option<f32> = None;
    let mut ov_excursion_guard: Option<f32> = None;

    let mut i = 0usize;
    while i < args.len() {
        match args[i].as_str() {
            "--in" => in_path = Some(next_arg(args, &mut i, "--in")?.to_string()),
            "--out" => out_path = Some(next_arg(args, &mut i, "--out")?.to_string()),
            "--fir" => fir_path = Some(next_arg(args, &mut i, "--fir")?.to_string()),
            "--mode" => {
                let m = next_arg(args, &mut i, "--mode")?;
                mode = StudioMode::from_str_name(m).ok_or_else(|| {
                    format!("invalid mode '{}' (expected deep|spatial|reference|voice)", m)
                })?;
            }
            "--volume-db" => ov_volume_db = Some(parse_f32_arg(args, &mut i, "--volume-db")?),
            "--bass-depth" => ov_bass_depth = Some(parse_f32_arg(args, &mut i, "--bass-depth")?),
            "--physical-bass-hz" => {
                ov_physical_bass_hz = Some(parse_f32_arg(args, &mut i, "--physical-bass-hz")?)
            }
            "--warmth-body" => ov_warmth_body = Some(parse_f32_arg(args, &mut i, "--warmth-body")?),
            "--stage-depth" => ov_stage_depth = Some(parse_f32_arg(args, &mut i, "--stage-depth")?),
            "--spatial-xtc" => ov_spatial_xtc = Some(parse_f32_arg(args, &mut i, "--spatial-xtc")?),
            "--drive-db" => ov_drive_db = Some(parse_f32_arg(args, &mut i, "--drive-db")?),
            "--ceiling-dbfs" => {
                ov_ceiling_dbfs = Some(parse_f32_arg(args, &mut i, "--ceiling-dbfs")?)
            }
            "--excursion-guard" => {
                ov_excursion_guard = Some(parse_f32_arg(args, &mut i, "--excursion-guard")?)
            }
            unknown => return Err(format!("unknown render argument '{}'", unknown)),
        }
        i += 1;
    }

    let in_path = in_path.ok_or_else(|| "render requires --in <in.wav>".to_string())?;
    let out_path = out_path.ok_or_else(|| "render requires --out <out.wav>".to_string())?;

    let wav = read_wav_stereo(&in_path)?;
    let mut params = ControlParams::for_mode(mode);
    if let Some(v) = ov_volume_db {
        params.volume_db = v;
    }
    if let Some(v) = ov_bass_depth {
        params.bass_depth = v;
    }
    if let Some(v) = ov_physical_bass_hz {
        params.physical_bass_hz = v;
    }
    if let Some(v) = ov_warmth_body {
        params.warmth_body = v;
    }
    if let Some(v) = ov_stage_depth {
        params.stage_depth = v;
    }
    if let Some(v) = ov_spatial_xtc {
        params.spatial_xtc = v;
    }
    if let Some(v) = ov_drive_db {
        params.drive_db = v;
    }
    if let Some(v) = ov_ceiling_dbfs {
        params.ceiling_dbfs = v;
    }
    if let Some(v) = ov_excursion_guard {
        params.excursion_guard = v;
    }
    params = params.sanitized();

    let mut engine = if let Some(ref fp) = fir_path {
        let fir = read_wav_stereo(fp)?;
        StudioDspEngine::new_with_fir(wav.sample_rate as f64, Some(&fir.left), Some(&fir.right))
    } else {
        StudioDspEngine::new(wav.sample_rate as f64)
    };

    let n = wav.left.len();
    let mut out_l = vec![0.0f32; n];
    let mut out_r = vec![0.0f32; n];

    let block = 128usize;
    let mut off = 0usize;
    while off < n {
        let chunk = (n - off).min(block);
        engine.process_stereo(
            &wav.left[off..off + chunk],
            &wav.right[off..off + chunk],
            &mut out_l[off..off + chunk],
            &mut out_r[off..off + chunk],
            &params,
        );
        off += chunk;
    }

    write_wav_stereo_f32(&out_path, wav.sample_rate, &out_l, &out_r)?;

    let peak = out_l
        .iter()
        .chain(out_r.iter())
        .fold(0.0f32, |acc, &x| acc.max(x.abs()));
    let peak_dbfs = 20.0 * peak.max(1e-9).log10();
    let lat = engine.total_latency_samples();
    let lat_ms = (lat as f64) * 1000.0 / (wav.sample_rate as f64);

    println!(
        "Rendered '{}' -> '{}' | mode={} | {} frames @ {} Hz | peak={:.2} dBFS | limiter_gr={:.2} dB | hpf={:.1} Hz | latency={} samp ({:.2} ms)",
        in_path,
        out_path,
        mode.as_str(),
        n,
        wav.sample_rate,
        peak_dbfs,
        engine.limiter_gr_db(),
        engine.excursion_cutoff_hz(),
        lat,
        lat_ms,
    );
    Ok(())
}

// ============================================================================
// Subcommand 2: `bench`
// ============================================================================

fn cmd_bench() -> Result<(), String> {
    println!("=== Dell XPS 17 9720 Studio Audio DSP Engine — SIMD & Hard-RT Benchmark ===");
    println!("CPU AVX2 + FMA detected : {}", has_avx2_fma());
    println!();

    // 1. SIMD/ASM vs Scalar micro-benchmarks
    let results = run_simd_benchmarks();
    println!(
        "{:<28} | {:>12} | {:>12} | {:>9} | {:>12}",
        "Kernel", "AVX2 (ns)", "Scalar (ns)", "Speedup", "Max Abs Err"
    );
    println!("{}", "-".repeat(85));
    for r in &results {
        println!(
            "{:<28} | {:>12.1} | {:>12.1} | {:>8.2}x | {:>12.3e}",
            r.name, r.avx2_ns, r.scalar_ns, r.speedup, r.max_abs_err
        );
    }
    println!();

    // 2. Full 7-stage StudioDspEngine benchmark: 2000 blocks x 256 samples @ 48 kHz (10.67 s)
    const SR: f64 = 48_000.0;
    const BLOCK: usize = 256;
    const WARMUP_BLOCKS: usize = 100;
    const MEASURED_BLOCKS: usize = 2000;
    const BUDGET_US: f64 = (BLOCK as f64) * 1_000_000.0 / SR; // 5333.33 us per block

    let mut engine = StudioDspEngine::new(SR);
    let params = ControlParams::for_mode(StudioMode::Spatial); // Full chain including FDTR-XTC

    // Generate realistic music-like multi-tone + transient stereo signal
    let total_src = (WARMUP_BLOCKS + MEASURED_BLOCKS) * BLOCK;
    let mut src_l = vec![0.0f32; total_src];
    let mut src_r = vec![0.0f32; total_src];
    for i in 0..total_src {
        let t = (i as f32) / (SR as f32);
        let beat_phase = (t * 2.0).fract();
        let kick_env = (-beat_phase * 14.0).exp();
        let sub = 0.30 * kick_env * (TAU * 55.0 * t).sin();
        let bass = 0.22 * (TAU * 98.0 * t).sin();
        let body = 0.16 * (TAU * 245.0 * t).sin();
        let vox = 0.14 * (TAU * 720.0 * t).sin() + 0.10 * (TAU * 1480.0 * t).sin();
        let shim_l = 0.08 * (TAU * 3900.0 * t).sin() + 0.05 * (TAU * 8400.0 * t).sin();
        let shim_r = 0.08 * (TAU * 4120.0 * t).cos() + 0.05 * (TAU * 8900.0 * t).sin();
        src_l[i] = sub + bass + body + vox + shim_l;
        src_r[i] = sub + bass + body + vox * 0.96 + shim_r;
    }

    let mut buf_l = [0.0f32; BLOCK];
    let mut buf_r = [0.0f32; BLOCK];
    let mut block_ns = vec![0u64; MEASURED_BLOCKS];

    // Warmup
    for b in 0..WARMUP_BLOCKS {
        let off = b * BLOCK;
        buf_l.copy_from_slice(&src_l[off..off + BLOCK]);
        buf_r.copy_from_slice(&src_r[off..off + BLOCK]);
        engine.process_block(&mut buf_l, &mut buf_r, &params);
    }

    // Armed hard-RT zero-allocation benchmark pass
    let mut sink_acc = 0.0f32;
    arm_alloc_counter();
    let wall_start = Instant::now();
    for b in 0..MEASURED_BLOCKS {
        let off = (WARMUP_BLOCKS + b) * BLOCK;
        buf_l.copy_from_slice(&src_l[off..off + BLOCK]);
        buf_r.copy_from_slice(&src_r[off..off + BLOCK]);

        let t0 = Instant::now();
        engine.process_block(&mut buf_l, &mut buf_r, &params);
        let dt_ns = t0.elapsed().as_nanos() as u64;

        block_ns[b] = dt_ns;
        sink_acc += buf_l[0] + buf_r[BLOCK - 1];
    }
    let total_wall_us = wall_start.elapsed().as_secs_f64() * 1_000_000.0;
    let rt_allocs = disarm_alloc_counter();

    // Prevent dead-code elimination
    std::hint::black_box(sink_acc);

    block_ns.sort_unstable();
    let sum_ns: u128 = block_ns.iter().map(|&x| x as u128).sum();
    let mean_us = (sum_ns as f64) / (MEASURED_BLOCKS as f64) / 1000.0;
    let p50_us = (block_ns[MEASURED_BLOCKS * 50 / 100] as f64) / 1000.0;
    let p95_us = (block_ns[MEASURED_BLOCKS * 95 / 100] as f64) / 1000.0;
    let p99_us = (block_ns[MEASURED_BLOCKS * 99 / 100] as f64) / 1000.0;
    let max_us = (block_ns[MEASURED_BLOCKS - 1] as f64) / 1000.0;

    let cpu_pct = (mean_us / BUDGET_US) * 100.0;
    let rt_speedup = BUDGET_US / mean_us.max(1e-6);
    let lat_samp = engine.total_latency_samples();
    let lat_ms = (lat_samp as f64) * 1000.0 / SR;

    println!("=== Full 7-Stage StudioDspEngine RT Benchmark (Spatial Mode, 4096-tap FIR + XTC) ===");
    println!(
        "Blocks processed        : {} x {} stereo frames ({:.2} s audio @ 48 kHz)",
        MEASURED_BLOCKS,
        BLOCK,
        (MEASURED_BLOCKS * BLOCK) as f64 / SR
    );
    println!("Block RT budget         : {:.2} us", BUDGET_US);
    println!("Mean block latency      : {:.2} us", mean_us);
    println!("p50 / p95 / p99 / max   : {:.2} / {:.2} / {:.2} / {:.2} us", p50_us, p95_us, p99_us, max_us);
    println!("Total wall time         : {:.2} ms", total_wall_us / 1000.0);
    println!("CPU usage (1 E-core)    : {:.2}%", cpu_pct);
    println!("Realtime speedup        : {:.1}x realtime", rt_speedup);
    println!("Algorithmic latency     : {} samples ({:.2} ms)", lat_samp, lat_ms);
    println!("Convolver partitions    : {}", engine.convolver_active_partitions());
    println!(
        "Acoustics loaded        : kernels_from_disk={:?} | dsp_params_json={}",
        engine.kernels_loaded_from_disk(),
        engine.params_loaded_from_disk()
    );
    println!("RT heap allocations     : {} (HARD-RT PROOF: {})", rt_allocs, if rt_allocs == 0 { "PASS" } else { "FAIL" });

    // Stage-by-stage microsecond breakdown (500 blocks, printed after CPU % line)
    let mut stage_ns_sum = [0u128; 7];
    const PROF_BLOCKS: usize = 500;
    for b in 0..PROF_BLOCKS {
        let off = b * BLOCK;
        buf_l.copy_from_slice(&src_l[off..off + BLOCK]);
        buf_r.copy_from_slice(&src_r[off..off + BLOCK]);
        let sns = engine.process_block_profiled(&mut buf_l, &mut buf_r, &params);
        for s in 0..7 {
            stage_ns_sum[s] += sns[s] as u128;
        }
    }
    let st_us: [f64; 7] = std::array::from_fn(|s| (stage_ns_sum[s] as f64) / (PROF_BLOCKS as f64) / 1000.0);
    println!(
        "Stage breakdown (us)    : bass={:.2} | loud={:.2} | conv={:.2} | dyn={:.2} | spat={:.2} | exc={:.2} | lim={:.2}",
        st_us[0], st_us[1], st_us[2], st_us[3], st_us[4], st_us[5], st_us[6]
    );

    if rt_allocs != 0 {
        return Err(format!("HARD-RT VIOLATION: {} heap allocations during audio loop", rt_allocs));
    }
    Ok(())
}

// ============================================================================
// Subcommand 3: `impulse`
// ============================================================================

fn cmd_impulse(args: &[String]) -> Result<(), String> {
    let mut out_path: Option<String> = None;
    let mut fir_path: Option<String> = None;
    let mut mode = StudioMode::Deep;
    let mut length: usize = 8192;
    let mut volume_db: f32 = -12.0;

    let mut i = 0usize;
    while i < args.len() {
        match args[i].as_str() {
            "--out" => out_path = Some(next_arg(args, &mut i, "--out")?.to_string()),
            "--fir" => fir_path = Some(next_arg(args, &mut i, "--fir")?.to_string()),
            "--mode" => {
                let m = next_arg(args, &mut i, "--mode")?;
                mode = StudioMode::from_str_name(m).ok_or_else(|| {
                    format!("invalid mode '{}' (expected deep|spatial|reference|voice)", m)
                })?;
            }
            "--length" => {
                let s = next_arg(args, &mut i, "--length")?;
                length = s
                    .parse::<usize>()
                    .map_err(|_| format!("invalid integer '{}' for --length", s))?;
                if length < 64 {
                    return Err("--length must be >= 64".to_string());
                }
            }
            "--volume-db" => volume_db = parse_f32_arg(args, &mut i, "--volume-db")?,
            unknown => return Err(format!("unknown impulse argument '{}'", unknown)),
        }
        i += 1;
    }

    let out_path = out_path.ok_or_else(|| "impulse requires --out <ir.wav>".to_string())?;
    const SR: u32 = 48_000;

    let mut params = ControlParams::for_mode(mode);
    params.volume_db = volume_db;
    params = params.sanitized();

    let mut engine = if let Some(ref fp) = fir_path {
        let fir = read_wav_stereo(fp)?;
        StudioDspEngine::new_with_fir(SR as f64, Some(&fir.left), Some(&fir.right))
    } else {
        StudioDspEngine::new(SR as f64)
    };

    // -18 dBFS impulse keeps compressor/limiter linear so transfer response is clean
    let amp = 10.0_f32.powf(-18.0 / 20.0);
    let mut in_l = vec![0.0f32; length];
    let mut in_r = vec![0.0f32; length];
    in_l[0] = amp;
    in_r[0] = amp;

    let mut out_l = vec![0.0f32; length];
    let mut out_r = vec![0.0f32; length];

    let block = 128usize;
    let mut off = 0usize;
    while off < length {
        let chunk = (length - off).min(block);
        engine.process_stereo(
            &in_l[off..off + chunk],
            &in_r[off..off + chunk],
            &mut out_l[off..off + chunk],
            &mut out_r[off..off + chunk],
            &params,
        );
        off += chunk;
    }

    write_wav_stereo_f32(&out_path, SR, &out_l, &out_r)?;

    let peak = out_l
        .iter()
        .chain(out_r.iter())
        .fold(0.0f32, |acc, &x| acc.max(x.abs()));
    let peak_dbfs = 20.0 * peak.max(1e-9).log10();

    println!(
        "Impulse written -> '{}' | mode={} | length={} @ {} Hz | volume={:.1} dB | peak={:.2} dBFS | latency={} samp",
        out_path,
        mode.as_str(),
        length,
        SR,
        params.volume_db,
        peak_dbfs,
        engine.total_latency_samples()
    );
    Ok(())
}

// ============================================================================
// Subcommand 4: `verify`
// ============================================================================

/// Simple 2nd-order Butterworth LPF biquad used offline to isolate < 180 Hz bass energy.
struct BiquadLpf {
    b0: f64,
    b1: f64,
    b2: f64,
    a1: f64,
    a2: f64,
    z1: f64,
    z2: f64,
}

impl BiquadLpf {
    fn butterworth(fs: f64, fc: f64, q: f64) -> Self {
        let w0 = 2.0 * std::f64::consts::PI * fc / fs;
        let cos_w = w0.cos();
        let alpha = w0.sin() / (2.0 * q);
        let a0 = 1.0 + alpha;
        Self {
            b0: ((1.0 - cos_w) * 0.5) / a0,
            b1: (1.0 - cos_w) / a0,
            b2: ((1.0 - cos_w) * 0.5) / a0,
            a1: (-2.0 * cos_w) / a0,
            a2: (1.0 - alpha) / a0,
            z1: 0.0,
            z2: 0.0,
        }
    }

    #[inline]
    fn tick(&mut self, x: f64) -> f64 {
        let y = self.b0 * x + self.z1;
        self.z1 = self.b1 * x - self.a1 * y + self.z2;
        self.z2 = self.b2 * x - self.a2 * y;
        y
    }
}

fn lowpass_4th_order(samples: &[f32], fs: f64, fc: f64) -> Vec<f64> {
    let mut s1 = BiquadLpf::butterworth(fs, fc, 0.5411961);
    let mut s2 = BiquadLpf::butterworth(fs, fc, 1.3065630);
    samples.iter().map(|&x| s2.tick(s1.tick(x as f64))).collect()
}

fn measure_true_peak_dbfs(channel: &[f32]) -> f32 {
    let n = channel.len();
    if n == 0 {
        return -120.0;
    }
    let mut padded = vec![0.0f32; n + 3];
    padded[1..1 + n].copy_from_slice(channel);
    let mut peaks = vec![0.0f32; n];
    let tp = true_peak_4x_f32(&padded, &mut peaks);
    20.0 * tp.max(1e-9).log10()
}

fn cmd_verify() -> Result<(), String> {
    const SR: f64 = 48_000.0;
    const BLOCK: usize = 128;
    let mut failures: Vec<String> = Vec::new();

    println!("=== Dell XPS 17 9720 Studio Audio DSP Engine — Automated Verification Suite ===");

    for mode in StudioMode::ALL {
        println!("\n[Mode: {}]", mode.as_str());
        let mut engine = StudioDspEngine::new(SR);
        let params = ControlParams::for_mode(mode);

        // ------------------------------------------------------------------
        // Check 1: Zero NaN/Inf + Denormal Immunity + Zero RT Allocations
        // ------------------------------------------------------------------
        let mut l = [0.0f32; BLOCK];
        let mut r = [0.0f32; BLOCK];
        let mut bad_samples = 0usize;

        arm_alloc_counter();
        for blk in 0..64 {
            for i in 0..BLOCK {
                let t = ((blk * BLOCK + i) as f32) / (SR as f32);
                let mut sl = 0.25 * (TAU * 440.0 * t).sin();
                let mut sr = 0.25 * (TAU * 660.0 * t).sin();
                if blk == 4 && i == 10 {
                    sl = f32::NAN;
                    sr = f32::INFINITY;
                } else if blk == 5 && i == 20 {
                    sl = f32::NEG_INFINITY;
                    sr = 1.0e-40_f32; // denormal
                } else if blk >= 16 && blk < 32 {
                    sl = 1.0e-40_f32;
                    sr = -1.0e-41_f32;
                }
                l[i] = sl;
                r[i] = sr;
            }
            engine.process_block(&mut l, &mut r, &params);
            for i in 0..BLOCK {
                if !l[i].is_finite() || !r[i].is_finite() {
                    bad_samples += 1;
                }
            }
        }
        let allocs = disarm_alloc_counter();
        println!(
            "  1. NaN/Inf & Denormal Immunity : bad_samples={} allocs={} -> {}",
            bad_samples,
            allocs,
            if bad_samples == 0 && allocs == 0 { "PASS" } else { "FAIL" }
        );
        if bad_samples > 0 {
            failures.push(format!("{}: produced {} non-finite samples", mode.as_str(), bad_samples));
        }
        if allocs > 0 {
            failures.push(format!("{}: {} heap allocations on RT path", mode.as_str(), allocs));
        }

        // ------------------------------------------------------------------
        // Check 2: True-Peak Ceiling Enforcement (<= ceiling_dbfs + 0.05 dB)
        // ------------------------------------------------------------------
        engine.reset();
        let mut hot_params = params;
        hot_params.drive_db = 10.0;
        hot_params.ceiling_dbfs = -1.0;

        let n_tp = 16_384usize;
        let mut tp_l = vec![0.0f32; n_tp];
        let mut tp_r = vec![0.0f32; n_tp];
        for i in 0..n_tp {
            let t = (i as f32) / (SR as f32);
            // Hot +3 dBFS composite signal with sharp transient bursts
            let burst = if i % 2048 < 16 { 1.4 } else { 0.0 };
            tp_l[i] = 0.75 * (TAU * 120.0 * t).sin()
                + 0.65 * (TAU * 997.0 * t).sin()
                + 0.45 * (TAU * 5400.0 * t).sin()
                + burst;
            tp_r[i] = 0.75 * (TAU * 120.0 * t).sin()
                + 0.65 * (TAU * 1234.0 * t).cos()
                + 0.45 * (TAU * 6100.0 * t).sin()
                - burst;
        }
        for off in (0..n_tp).step_by(BLOCK) {
            engine.process_block(
                &mut tp_l[off..off + BLOCK],
                &mut tp_r[off..off + BLOCK],
                &hot_params,
            );
        }
        let max_tp_dbfs = measure_true_peak_dbfs(&tp_l).max(measure_true_peak_dbfs(&tp_r));
        let tp_ok = max_tp_dbfs <= hot_params.ceiling_dbfs + 0.05;
        println!(
            "  2. True-Peak Ceiling (-1.0 dB) : measured={:.3} dBFS (limit <= {:.2} dBFS) -> {}",
            max_tp_dbfs,
            hot_params.ceiling_dbfs + 0.05,
            if tp_ok { "PASS" } else { "FAIL" }
        );
        if !tp_ok {
            failures.push(format!(
                "{}: true peak {:.3} dBFS exceeded ceiling {:.2} + 0.05 dB",
                mode.as_str(),
                max_tp_dbfs,
                hot_params.ceiling_dbfs
            ));
        }

        // ------------------------------------------------------------------
        // Check 3: Mono Bass Correlation < 180 Hz (> 0.99)
        // ------------------------------------------------------------------
        engine.reset();
        let n_mb = 24_576usize; // ~512 ms
        let mut mb_l = vec![0.0f32; n_mb];
        let mut mb_r = vec![0.0f32; n_mb];
        for i in 0..n_mb {
            let t = (i as f32) / (SR as f32);
            // Uncorrelated quadrature 90 Hz bass + stereo 1.2 kHz content
            mb_l[i] = 0.30 * (TAU * 90.0 * t).sin() + 0.15 * (TAU * 1200.0 * t).sin();
            mb_r[i] = 0.30 * (TAU * 90.0 * t).cos() - 0.15 * (TAU * 1200.0 * t).sin();
        }
        for off in (0..n_mb).step_by(BLOCK) {
            engine.process_block(
                &mut mb_l[off..off + BLOCK],
                &mut mb_r[off..off + BLOCK],
                &params,
            );
        }
        let lp_l = lowpass_4th_order(&mb_l, SR, 145.0);
        let lp_r = lowpass_4th_order(&mb_r, SR, 145.0);
        let skip = 8192usize; // skip filter settle transient
        let mut dot = 0.0f64;
        let mut norm_l = 0.0f64;
        let mut norm_r = 0.0f64;
        for i in skip..n_mb {
            dot += lp_l[i] * lp_r[i];
            norm_l += lp_l[i] * lp_l[i];
            norm_r += lp_r[i] * lp_r[i];
        }
        let corr = if norm_l > 1e-12 && norm_r > 1e-12 {
            dot / (norm_l * norm_r).sqrt()
        } else {
            1.0
        };
        let mb_ok = corr > 0.99;
        println!(
            "  3. Mono Bass Corr (< 180 Hz)   : r={:.5} (target > 0.99000) -> {}",
            corr,
            if mb_ok { "PASS" } else { "FAIL" }
        );
        if !mb_ok {
            failures.push(format!("{}: mono bass correlation {:.5} <= 0.99", mode.as_str(), corr));
        }

        // ------------------------------------------------------------------
        // Check 4: Klippel Excursion HPF Glide under 0 dBFS 50 Hz Burst
        // ------------------------------------------------------------------
        engine.reset();
        let mut exc_params = ControlParams::for_mode(mode);
        exc_params.volume_db = 0.0;
        exc_params.excursion_guard = 1.0;
        exc_params.physical_bass_hz = 82.0;

        // Phase A: quiet -36 dBFS 50 Hz baseline
        for blk in 0..80 {
            for i in 0..BLOCK {
                let t = ((blk * BLOCK + i) as f32) / (SR as f32);
                let s = 0.015 * (TAU * 50.0 * t).sin();
                l[i] = s;
                r[i] = s;
            }
            engine.process_block(&mut l, &mut r, &exc_params);
        }
        let rest_cutoff = engine.excursion_cutoff_hz();

        // Phase B: loud 0 dBFS 50 Hz woofer stress burst
        let mut peak_cutoff = rest_cutoff;
        let mut peak_ratio = 0.0f64;
        for blk in 0..160 {
            for i in 0..BLOCK {
                let t = ((blk * BLOCK + i) as f32) / (SR as f32);
                let s = 0.99 * (TAU * 50.0 * t).sin();
                l[i] = s;
                r[i] = s;
            }
            engine.process_block(&mut l, &mut r, &exc_params);
            peak_cutoff = peak_cutoff.max(engine.excursion_cutoff_hz());
            peak_ratio = peak_ratio.max(engine.excursion_ratio());
        }
        let glide_hz = peak_cutoff - rest_cutoff;
        let glide_ok = glide_hz > 15.0;
        println!(
            "  4. Klippel Excursion HPF Glide : rest={:.1} Hz -> peak={:.1} Hz (delta=+{:.1} Hz, x/Xmax={:.2}) -> {}",
            rest_cutoff,
            peak_cutoff,
            glide_hz,
            peak_ratio,
            if glide_ok { "PASS" } else { "FAIL" }
        );
        if !glide_ok {
            failures.push(format!(
                "{}: excursion HPF failed to glide upward under 0 dBFS 50 Hz burst (rest={:.1} Hz, peak={:.1} Hz)",
                mode.as_str(),
                rest_cutoff,
                peak_cutoff
            ));
        }
    }

    println!("\n=====================================================================");
    if failures.is_empty() {
        println!("ALL VERIFICATION CHECKS PASSED ACROSS ALL 4 STUDIO MODES.");
        Ok(())
    } else {
        for f in &failures {
            eprintln!("  FAIL: {}", f);
        }
        Err(format!("{} verification check(s) failed", failures.len()))
    }
}

//! Configuration, Studio Modes, Pure-Rust Stereo WAV I/O,
//! Optional Lead 2 Acoustic Parameter/Kernel Loader, and
//! Embedded XPS 17 9720 Minimum-Phase 4096-Tap FIR Synthesizer.

use std::fs::{self, File};
use std::io::{BufReader, BufWriter, Read, Write};
use std::path::{Path, PathBuf};

use crate::bass::BassEngineParams;
use crate::excursion::ExcursionParams;

/// Default directory where Lead 2 (`acoustics-research`) stages finalized
/// `dsp_params.json` and `kernels/xps17-studio-*.wav`.
pub const DEFAULT_ACOUSTICS_DIR: &str =
    "/home/akshaybapat/project/xps17-studio-audio/acoustics-research";

/// Environment variable overriding the acoustics directory at runtime.
pub const ACOUSTICS_DIR_ENV: &str = "XPS17_STUDIO_ACOUSTICS_DIR";

/// Target FIR impulse length in taps at 48 kHz.
pub const FIR_TAPS: usize = 4096;

/// FFT length used for real-cepstrum minimum-phase FIR construction (power of 2, >= 2 * FIR_TAPS).
const CEPSTRUM_NFFT: usize = 8192;

/// Four studio-calibrated operating modes exposed via LADSPA port 4 (`Mode`).
#[repr(u8)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StudioMode {
    /// Mode 0: Flagship warm, deep, lush studio target.
    Deep = 0,
    /// Mode 1: Cinema/wide balanced target with mono-anchored FDTR-XTC.
    Spatial = 1,
    /// Mode 2: Harman/Olive studio neutral reference target.
    Reference = 2,
    /// Mode 3: Ultra-clear vocal/podcast/speech target.
    Voice = 3,
}

impl StudioMode {
    pub const ALL: [StudioMode; 4] = [
        StudioMode::Deep,
        StudioMode::Spatial,
        StudioMode::Reference,
        StudioMode::Voice,
    ];

    #[inline]
    pub fn from_f32(v: f32) -> Self {
        if !v.is_finite() {
            return Self::Deep;
        }
        match v.round().clamp(0.0, 3.0) as u8 {
            0 => Self::Deep,
            1 => Self::Spatial,
            2 => Self::Reference,
            _ => Self::Voice,
        }
    }

    pub fn from_str_name(s: &str) -> Option<Self> {
        match s.trim().to_ascii_lowercase().as_str() {
            "deep" => Some(Self::Deep),
            "spatial" => Some(Self::Spatial),
            "reference" => Some(Self::Reference),
            "voice" => Some(Self::Voice),
            _ => None,
        }
    }

    #[inline]
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Deep => "deep",
            Self::Spatial => "spatial",
            Self::Reference => "reference",
            Self::Voice => "voice",
        }
    }

    #[inline]
    pub fn kernel_filename(&self) -> &'static str {
        match self {
            Self::Deep => "xps17-studio-deep.wav",
            Self::Spatial => "xps17-studio-spatial.wav",
            Self::Reference => "xps17-studio-reference.wav",
            Self::Voice => "xps17-studio-voice.wav",
        }
    }
}

impl Default for StudioMode {
    #[inline]
    fn default() -> Self {
        Self::Deep
    }
}

/// Complete set of 10 LADSPA control parameters matching `COORDINATOR-NOTES.md`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ControlParams {
    /// Port 4: `Mode` (0 = Deep, 1 = Spatial, 2 = Reference, 3 = Voice)
    pub mode: StudioMode,
    /// Port 5: `Volume (dB)` (`-45.0 ..= 0.0`, default `-12.0`)
    pub volume_db: f32,
    /// Port 6: `Bass Depth` (`0.0 ..= 1.5`, default `1.0`)
    pub bass_depth: f32,
    /// Port 7: `Physical Bass Hz` (`65.0 ..= 180.0`, default `82.0`)
    pub physical_bass_hz: f32,
    /// Port 8: `Warmth Body` (`0.0 ..= 1.5`, default `1.0`)
    pub warmth_body: f32,
    /// Port 9: `Stage Depth` (`0.0 ..= 1.5`, default `0.85`)
    pub stage_depth: f32,
    /// Port 10: `Spatial XTC` (`0.0 ..= 1.0`, default `0.0` Deep / `0.75` Spatial)
    pub spatial_xtc: f32,
    /// Port 11: `Drive (dB)` (`0.0 ..= 12.0`, default `7.0`)
    pub drive_db: f32,
    /// Port 12: `Ceiling (dBFS)` (`-9.0 ..= -0.3`, default `-1.0`)
    pub ceiling_dbfs: f32,
    /// Port 13: `Excursion Guard` (`0.0 ..= 1.0`, default `1.0`)
    pub excursion_guard: f32,
}

impl ControlParams {
    pub const VOLUME_DB_MIN: f32 = -45.0;
    pub const VOLUME_DB_MAX: f32 = 0.0;
    pub const BASS_DEPTH_MIN: f32 = 0.0;
    pub const BASS_DEPTH_MAX: f32 = 1.5;
    pub const PHYSICAL_BASS_HZ_MIN: f32 = 65.0;
    pub const PHYSICAL_BASS_HZ_MAX: f32 = 180.0;
    pub const WARMTH_BODY_MIN: f32 = 0.0;
    pub const WARMTH_BODY_MAX: f32 = 1.5;
    pub const STAGE_DEPTH_MIN: f32 = 0.0;
    pub const STAGE_DEPTH_MAX: f32 = 1.5;
    pub const SPATIAL_XTC_MIN: f32 = 0.0;
    pub const SPATIAL_XTC_MAX: f32 = 1.0;
    pub const DRIVE_DB_MIN: f32 = 0.0;
    pub const DRIVE_DB_MAX: f32 = 12.0;
    pub const CEILING_DBFS_MIN: f32 = -9.0;
    pub const CEILING_DBFS_MAX: f32 = -0.3;
    pub const EXCURSION_GUARD_MIN: f32 = 0.0;
    pub const EXCURSION_GUARD_MAX: f32 = 1.0;

    /// Studio-calibrated defaults per operating mode.
    #[inline]
    pub fn for_mode(mode: StudioMode) -> Self {
        match mode {
            StudioMode::Deep => Self {
                mode,
                volume_db: -12.0,
                bass_depth: 1.0,
                physical_bass_hz: 82.0,
                warmth_body: 1.0,
                stage_depth: 0.85,
                spatial_xtc: 0.0,
                drive_db: 7.0,
                ceiling_dbfs: -1.0,
                excursion_guard: 1.0,
            },
            StudioMode::Spatial => Self {
                mode,
                volume_db: -12.0,
                bass_depth: 0.95,
                physical_bass_hz: 85.0,
                warmth_body: 0.90,
                stage_depth: 1.15,
                spatial_xtc: 0.75,
                drive_db: 7.0,
                ceiling_dbfs: -1.0,
                excursion_guard: 1.0,
            },
            StudioMode::Reference => Self {
                mode,
                volume_db: -12.0,
                bass_depth: 0.55,
                physical_bass_hz: 90.0,
                warmth_body: 0.45,
                stage_depth: 0.35,
                spatial_xtc: 0.0,
                drive_db: 5.5,
                ceiling_dbfs: -1.0,
                excursion_guard: 1.0,
            },
            StudioMode::Voice => Self {
                mode,
                volume_db: -12.0,
                bass_depth: 0.20,
                physical_bass_hz: 115.0,
                warmth_body: 0.35,
                stage_depth: 0.15,
                spatial_xtc: 0.0,
                drive_db: 6.5,
                ceiling_dbfs: -1.0,
                excursion_guard: 1.0,
            },
        }
    }

    /// Replaces any NaN/Inf with the mode default and clamps every field to its valid LADSPA range.
    #[inline]
    pub fn sanitized(&self) -> Self {
        let def = Self::for_mode(self.mode);
        Self {
            mode: self.mode,
            volume_db: sanitize_clamp(
                self.volume_db,
                def.volume_db,
                Self::VOLUME_DB_MIN,
                Self::VOLUME_DB_MAX,
            ),
            bass_depth: sanitize_clamp(
                self.bass_depth,
                def.bass_depth,
                Self::BASS_DEPTH_MIN,
                Self::BASS_DEPTH_MAX,
            ),
            physical_bass_hz: sanitize_clamp(
                self.physical_bass_hz,
                def.physical_bass_hz,
                Self::PHYSICAL_BASS_HZ_MIN,
                Self::PHYSICAL_BASS_HZ_MAX,
            ),
            warmth_body: sanitize_clamp(
                self.warmth_body,
                def.warmth_body,
                Self::WARMTH_BODY_MIN,
                Self::WARMTH_BODY_MAX,
            ),
            stage_depth: sanitize_clamp(
                self.stage_depth,
                def.stage_depth,
                Self::STAGE_DEPTH_MIN,
                Self::STAGE_DEPTH_MAX,
            ),
            spatial_xtc: sanitize_clamp(
                self.spatial_xtc,
                def.spatial_xtc,
                Self::SPATIAL_XTC_MIN,
                Self::SPATIAL_XTC_MAX,
            ),
            drive_db: sanitize_clamp(
                self.drive_db,
                def.drive_db,
                Self::DRIVE_DB_MIN,
                Self::DRIVE_DB_MAX,
            ),
            ceiling_dbfs: sanitize_clamp(
                self.ceiling_dbfs,
                def.ceiling_dbfs,
                Self::CEILING_DBFS_MIN,
                Self::CEILING_DBFS_MAX,
            ),
            excursion_guard: sanitize_clamp(
                self.excursion_guard,
                def.excursion_guard,
                Self::EXCURSION_GUARD_MIN,
                Self::EXCURSION_GUARD_MAX,
            ),
        }
    }
}

impl Default for ControlParams {
    #[inline]
    fn default() -> Self {
        Self::for_mode(StudioMode::Deep)
    }
}

#[inline]
fn sanitize_clamp(v: f32, default: f32, min: f32, max: f32) -> f32 {
    if v.is_finite() {
        v.clamp(min, max)
    } else {
        default
    }
}

// ============================================================================
// Pure-Rust Stereo WAV Reader & Writer (IEEE float32/64 & PCM 16/24/32-bit)
// ============================================================================

/// Decoded stereo WAV audio buffer.
#[derive(Debug, Clone)]
pub struct StereoWav {
    pub sample_rate: u32,
    pub left: Vec<f32>,
    pub right: Vec<f32>,
}

/// Reads a WAV file from disk into stereo `f32` channels (`[-1.0, 1.0]` nominal).
///
/// Supports:
/// - IEEE float32 (tag `3`) and IEEE float64 (tag `3`, 64-bit)
/// - Signed integer PCM 16-bit, 24-bit, 32-bit (tag `1`)
/// - `WAVE_FORMAT_EXTENSIBLE` (tag `0xFFFE`) wrapping PCM or IEEE float
/// - Arbitrary RIFF chunks (`JUNK`, `LIST`, `fact`, `PEAK`, `bext`, etc.)
/// - Mono inputs (duplicated to L=R) and multi-channel inputs (first 2 channels)
pub fn read_wav_stereo<P: AsRef<Path>>(path: P) -> Result<StereoWav, String> {
    let file = File::open(path.as_ref())
        .map_err(|e| format!("failed to open WAV '{}': {}", path.as_ref().display(), e))?;
    let mut reader = BufReader::new(file);
    let mut bytes = Vec::new();
    reader
        .read_to_end(&mut bytes)
        .map_err(|e| format!("failed to read WAV '{}': {}", path.as_ref().display(), e))?;

    decode_wav_stereo(&bytes)
}

/// Decodes stereo `f32` audio from raw RIFF/WAVE bytes.
pub fn decode_wav_stereo(bytes: &[u8]) -> Result<StereoWav, String> {
    if bytes.len() < 12 {
        return Err("WAV too short (< 12 bytes)".to_string());
    }
    if &bytes[0..4] != b"RIFF" || &bytes[8..12] != b"WAVE" {
        return Err("not a valid RIFF/WAVE file".to_string());
    }

    let mut pos: usize = 12;
    let mut fmt_parsed = false;
    let mut audio_format: u16 = 0;
    let mut num_channels: u16 = 0;
    let mut sample_rate: u32 = 48000;
    let mut bits_per_sample: u16 = 0;
    let mut data_slice: Option<&[u8]> = None;

    while pos + 8 <= bytes.len() {
        let chunk_id = &bytes[pos..pos + 4];
        let chunk_size = u32::from_le_bytes([
            bytes[pos + 4],
            bytes[pos + 5],
            bytes[pos + 6],
            bytes[pos + 7],
        ]) as usize;
        pos += 8;

        let available = bytes.len().saturating_sub(pos);
        let payload_len = chunk_size.min(available);
        let payload = &bytes[pos..pos + payload_len];

        if chunk_id == b"fmt " {
            if payload.len() < 16 {
                return Err("WAV fmt chunk shorter than 16 bytes".to_string());
            }
            let raw_tag = u16::from_le_bytes([payload[0], payload[1]]);
            num_channels = u16::from_le_bytes([payload[2], payload[3]]);
            sample_rate = u32::from_le_bytes([payload[4], payload[5], payload[6], payload[7]]);
            bits_per_sample = u16::from_le_bytes([payload[14], payload[15]]);

            if raw_tag == 0xFFFE {
                // WAVE_FORMAT_EXTENSIBLE: SubFormat GUID starts at byte 24 of fmt payload.
                if payload.len() < 40 {
                    return Err("WAVE_FORMAT_EXTENSIBLE fmt chunk truncated".to_string());
                }
                audio_format = u16::from_le_bytes([payload[24], payload[25]]);
            } else {
                audio_format = raw_tag;
            }
            fmt_parsed = true;
        } else if chunk_id == b"data" {
            data_slice = Some(payload);
        }

        // RIFF chunks are word-aligned (padded to even byte boundary).
        let advance = chunk_size.saturating_add(chunk_size & 1);
        pos = pos.saturating_add(advance);
    }

    if !fmt_parsed {
        return Err("WAV missing fmt chunk".to_string());
    }
    let data = data_slice.ok_or_else(|| "WAV missing data chunk".to_string())?;
    if num_channels == 0 {
        return Err("WAV has 0 channels".to_string());
    }
    if sample_rate == 0 {
        return Err("WAV has 0 sample rate".to_string());
    }

    let bytes_per_sample = match (audio_format, bits_per_sample) {
        (1, 16) => 2usize,
        (1, 24) => 3usize,
        (1, 32) => 4usize,
        (3, 32) => 4usize,
        (3, 64) => 8usize,
        (fmt, bits) => {
            return Err(format!(
                "unsupported WAV format tag={} bits_per_sample={}",
                fmt, bits
            ));
        }
    };

    let ch = num_channels as usize;
    let frame_bytes = bytes_per_sample * ch;
    if frame_bytes == 0 {
        return Err("invalid WAV frame stride".to_string());
    }
    let num_frames = data.len() / frame_bytes;
    let mut left = Vec::with_capacity(num_frames);
    let mut right = Vec::with_capacity(num_frames);

    for frame_idx in 0..num_frames {
        let base = frame_idx * frame_bytes;
        let l = decode_sample(&data[base..base + bytes_per_sample], audio_format, bits_per_sample);
        let r = if ch >= 2 {
            let off = base + bytes_per_sample;
            decode_sample(&data[off..off + bytes_per_sample], audio_format, bits_per_sample)
        } else {
            l
        };
        left.push(if l.is_finite() { l } else { 0.0 });
        right.push(if r.is_finite() { r } else { 0.0 });
    }

    Ok(StereoWav {
        sample_rate,
        left,
        right,
    })
}

#[inline]
fn decode_sample(raw: &[u8], audio_format: u16, bits_per_sample: u16) -> f32 {
    match (audio_format, bits_per_sample) {
        (1, 16) => i16::from_le_bytes([raw[0], raw[1]]) as f32 / 32768.0,
        (1, 24) => {
            let v = (raw[0] as i32) | ((raw[1] as i32) << 8) | ((raw[2] as i32) << 16);
            // Arithmetic shift sign-extends bit 23 into bits 24..31.
            let signed = (v << 8) >> 8;
            signed as f32 / 8388608.0
        }
        (1, 32) => {
            i32::from_le_bytes([raw[0], raw[1], raw[2], raw[3]]) as f32 / 2147483648.0
        }
        (3, 32) => f32::from_le_bytes([raw[0], raw[1], raw[2], raw[3]]),
        (3, 64) => f64::from_le_bytes([
            raw[0], raw[1], raw[2], raw[3], raw[4], raw[5], raw[6], raw[7],
        ]) as f32,
        _ => 0.0,
    }
}

/// Writes stereo `f32` channels as an IEEE float32 (format tag `3`) RIFF/WAVE file,
/// including the standard `fact` chunk required by strict float WAV parsers.
pub fn write_wav_stereo_f32<P: AsRef<Path>>(
    path: P,
    sample_rate: u32,
    left: &[f32],
    right: &[f32],
) -> Result<(), String> {
    let path_ref = path.as_ref();
    if let Some(parent) = path_ref.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent).map_err(|e| {
                format!(
                    "failed to create output directory '{}': {}",
                    parent.display(),
                    e
                )
            })?;
        }
    }

    let n = left.len().min(right.len());
    let num_channels: u16 = 2;
    let bits_per_sample: u16 = 32;
    let block_align: u16 = num_channels * (bits_per_sample / 8);
    let byte_rate: u32 = sample_rate.saturating_mul(block_align as u32);
    let data_bytes: u32 = (n * block_align as usize)
        .try_into()
        .map_err(|_| "WAV output exceeds 4 GiB RIFF limit".to_string())?;

    // RIFF payload = "WAVE" (4) + "fmt " (8+18) + "fact" (8+4) + "data" (8+data_bytes)
    let riff_size: u32 = 4u32
        .checked_add(8 + 18)
        .and_then(|x| x.checked_add(8 + 4))
        .and_then(|x| x.checked_add(8))
        .and_then(|x| x.checked_add(data_bytes))
        .ok_or_else(|| "WAV RIFF chunk size overflow".to_string())?;

    let file = File::create(path_ref)
        .map_err(|e| format!("failed to create WAV '{}': {}", path_ref.display(), e))?;
    let mut w = BufWriter::new(file);

    let write_err = |e: std::io::Error| format!("WAV write error '{}': {}", path_ref.display(), e);

    // RIFF header
    w.write_all(b"RIFF").map_err(write_err)?;
    w.write_all(&riff_size.to_le_bytes()).map_err(write_err)?;
    w.write_all(b"WAVE").map_err(write_err)?;

    // fmt chunk (18 bytes for IEEE float: includes cbSize = 0)
    w.write_all(b"fmt ").map_err(write_err)?;
    w.write_all(&18u32.to_le_bytes()).map_err(write_err)?;
    w.write_all(&3u16.to_le_bytes()).map_err(write_err)?; // WAVE_FORMAT_IEEE_FLOAT
    w.write_all(&num_channels.to_le_bytes()).map_err(write_err)?;
    w.write_all(&sample_rate.to_le_bytes()).map_err(write_err)?;
    w.write_all(&byte_rate.to_le_bytes()).map_err(write_err)?;
    w.write_all(&block_align.to_le_bytes()).map_err(write_err)?;
    w.write_all(&bits_per_sample.to_le_bytes()).map_err(write_err)?;
    w.write_all(&0u16.to_le_bytes()).map_err(write_err)?; // cbSize = 0

    // fact chunk (sample frames per channel)
    w.write_all(b"fact").map_err(write_err)?;
    w.write_all(&4u32.to_le_bytes()).map_err(write_err)?;
    w.write_all(&(n as u32).to_le_bytes()).map_err(write_err)?;

    // data chunk
    w.write_all(b"data").map_err(write_err)?;
    w.write_all(&data_bytes.to_le_bytes()).map_err(write_err)?;
    for i in 0..n {
        let l = if left[i].is_finite() { left[i] } else { 0.0 };
        let r = if right[i].is_finite() { right[i] } else { 0.0 };
        w.write_all(&l.to_le_bytes()).map_err(write_err)?;
        w.write_all(&r.to_le_bytes()).map_err(write_err)?;
    }
    w.flush().map_err(write_err)?;
    Ok(())
}

// ============================================================================
// Optional Lead 2 Parameter & Kernel Loader
// ============================================================================

/// Loaded or synthesized acoustic parameters and 4-mode stereo FIR kernels.
#[derive(Debug, Clone)]
pub struct AcousticsBundle {
    pub excursion_params: ExcursionParams,
    pub bass_params: BassEngineParams,
    /// Stereo FIR impulse responses `(ir_l, ir_r)` for slots `0..4` (`StudioMode`).
    pub kernels: [(Vec<f32>, Vec<f32>); 4],
    /// Whether each mode slot was loaded from Lead 2's external WAV kernel (`true`)
    /// or synthesized from the measured XPS 17 9720 acoustic curve (`false`).
    pub kernel_from_disk: [bool; 4],
    /// Whether `dsp_params.json` was found and parsed successfully.
    pub params_from_disk: bool,
}

/// Resolves the Lead 2 acoustics directory (non-fatal if absent).
pub fn resolve_acoustics_dir() -> PathBuf {
    if let Ok(val) = std::env::var(ACOUSTICS_DIR_ENV) {
        let trimmed = val.trim();
        if !trimmed.is_empty() {
            return PathBuf::from(trimmed);
        }
    }
    PathBuf::from(DEFAULT_ACOUSTICS_DIR)
}

/// Loads optional Lead 2 `dsp_params.json` and `kernels/xps17-studio-*.wav`,
/// gracefully falling back to calibrated defaults and synthesized 4096-tap
/// minimum-phase XPS 17 9720 FIR kernels if any files are absent or invalid.
pub fn load_acoustics_bundle(sample_rate: f64) -> AcousticsBundle {
    let dir = resolve_acoustics_dir();
    let mut excursion_params = ExcursionParams::default();
    let mut bass_params = BassEngineParams::default();
    let mut params_from_disk = false;

    let params_path = dir.join("dsp_params.json");
    if let Ok(text) = fs::read_to_string(&params_path) {
        if let Ok(root) = serde_json::from_str::<serde_json::Value>(&text) {
            apply_json_params(&root, &mut excursion_params, &mut bass_params);
            params_from_disk = true;
        }
    }

    let kernels_dir = dir.join("kernels");
    let mut kernel_from_disk = [false; 4];
    let mut kernels_vec: Vec<(Vec<f32>, Vec<f32>)> = Vec::with_capacity(4);

    for mode in StudioMode::ALL {
        let slot = mode as usize;
        let kpath = kernels_dir.join(mode.kernel_filename());
        if let Ok(wav) = read_wav_stereo(&kpath) {
            if !wav.left.is_empty() && wav.left.len() == wav.right.len() {
                let len = wav.left.len().min(FIR_TAPS);
                let l = wav.left[..len].to_vec();
                let r = wav.right[..len].to_vec();
                kernels_vec.push((l, r));
                kernel_from_disk[slot] = true;
                continue;
            }
        }
        // Fallback: synthesize genuine 4096-tap minimum-phase XPS 17 9720 FIR kernel
        let ir = synthesize_xps17_min_phase_fir(mode, sample_rate);
        kernels_vec.push((ir.clone(), ir));
    }

    let mut it = kernels_vec.into_iter();
    let kernels = [
        it.next().unwrap(),
        it.next().unwrap(),
        it.next().unwrap(),
        it.next().unwrap(),
    ];

    AcousticsBundle {
        excursion_params,
        bass_params,
        kernels,
        kernel_from_disk,
        params_from_disk,
    }
}

/// Polymorphic safe JSON assignment helper so numeric fields of either `f32`/`f64`
/// or fixed-size 1D/2D arrays are populated cleanly without panics or type fragility.
trait AssignJsonField {
    fn assign_json_bounded(&mut self, val: &serde_json::Value, min: f64, max: f64);
}

impl AssignJsonField for f64 {
    fn assign_json_bounded(&mut self, val: &serde_json::Value, min: f64, max: f64) {
        if let Some(x) = val.as_f64() {
            if x.is_finite() {
                *self = x.clamp(min, max);
            }
        }
    }
}

impl AssignJsonField for f32 {
    fn assign_json_bounded(&mut self, val: &serde_json::Value, min: f64, max: f64) {
        if let Some(x) = val.as_f64() {
            if x.is_finite() {
                *self = x.clamp(min, max) as f32;
            }
        }
    }
}

impl<T: AssignJsonField, const N: usize> AssignJsonField for [T; N] {
    fn assign_json_bounded(&mut self, val: &serde_json::Value, min: f64, max: f64) {
        if let Some(arr) = val.as_array() {
            for (slot, item) in self.iter_mut().zip(arr.iter()) {
                slot.assign_json_bounded(item, min, max);
            }
        }
    }
}

fn lookup_first<'a>(
    root: &'a serde_json::Value,
    paths: &[&[&str]],
) -> Option<&'a serde_json::Value> {
    for path in paths {
        let mut cur = root;
        let mut ok = true;
        for key in *path {
            if let Some(next) = cur.get(*key) {
                cur = next;
            } else {
                ok = false;
                break;
            }
        }
        if ok && !cur.is_null() {
            return Some(cur);
        }
    }
    None
}

fn apply_field<T: AssignJsonField>(
    root: &serde_json::Value,
    paths: &[&[&str]],
    dst: &mut T,
    min: f64,
    max: f64,
) {
    if let Some(val) = lookup_first(root, paths) {
        dst.assign_json_bounded(val, min, max);
    }
}

fn apply_json_params(
    root: &serde_json::Value,
    excursion: &mut ExcursionParams,
    bass: &mut BassEngineParams,
) {
    // Excursion / Klippel woofer model parameters
    apply_field(
        root,
        &[
            &["excursion", "f0_hz"],
            &["excursion_model", "f0_hz"],
            &["woofer", "f0_hz"],
            &["f0_hz"],
        ],
        &mut excursion.f0_hz,
        80.0,
        450.0,
    );
    apply_field(
        root,
        &[
            &["excursion", "q_ts"],
            &["excursion", "q_m"],
            &["excursion_model", "q_ts"],
            &["excursion_model", "q_m"],
            &["q_ts"],
        ],
        &mut excursion.q_ts,
        0.3,
        4.0,
    );
    apply_field(
        root,
        &[
            &["excursion", "x_max_norm"],
            &["excursion_model", "x_max_norm"],
            &["x_max_norm"],
        ],
        &mut excursion.x_max_norm,
        0.05,
        2.0,
    );
    apply_field(
        root,
        &[
            &["excursion", "f_min_hz"],
            &["sliding_hpf", "f_min_hz"],
            &["excursion_model", "f_min_hz"],
            &["f_min_hz"],
        ],
        &mut excursion.f_min_hz,
        50.0,
        160.0,
    );
    apply_field(
        root,
        &[
            &["excursion", "f_max_hz"],
            &["sliding_hpf", "f_max_hz"],
            &["excursion_model", "f_max_hz"],
            &["f_max_hz"],
        ],
        &mut excursion.f_max_hz,
        120.0,
        320.0,
    );
    apply_field(
        root,
        &[
            &["excursion", "attack_ms"],
            &["sliding_hpf", "attack_ms"],
            &["excursion_attack_ms"],
        ],
        &mut excursion.attack_ms,
        0.5,
        100.0,
    );
    apply_field(
        root,
        &[
            &["excursion", "release_ms"],
            &["sliding_hpf", "release_ms"],
            &["excursion_release_ms"],
        ],
        &mut excursion.release_ms,
        10.0,
        1500.0,
    );

    // 3-band missing-fundamental psychoacoustic bass & chest warmth parameters
    apply_field(
        root,
        &[
            &["bass", "mono_crossover_hz"],
            &["psychoacoustic_bass", "mono_crossover_hz"],
            &["mono_crossover_hz"],
        ],
        &mut bass.mono_crossover_hz,
        80.0,
        280.0,
    );
    apply_field(
        root,
        &[
            &["bass", "band_edges_hz"],
            &["psychoacoustic_bass", "band_edges_hz"],
            &["band_edges_hz"],
        ],
        &mut bass.band_edges_hz,
        20.0,
        400.0,
    );
    apply_field(
        root,
        &[
            &["bass", "harmonic_weights"],
            &["psychoacoustic_bass", "harmonic_weights"],
            &["harmonic_weights"],
        ],
        &mut bass.harmonic_weights,
        0.0,
        2.0,
    );
    apply_field(
        root,
        &[
            &["bass", "band_drives"],
            &["psychoacoustic_bass", "band_drives"],
            &["band_drives"],
        ],
        &mut bass.band_drives,
        0.1,
        12.0,
    );
    apply_field(
        root,
        &[
            &["bass", "band_gains"],
            &["psychoacoustic_bass", "band_gains"],
            &["band_gains"],
        ],
        &mut bass.band_gains,
        0.0,
        3.0,
    );
    apply_field(
        root,
        &[
            &["bass", "synth_mix"],
            &["psychoacoustic_bass", "synth_mix"],
            &["synth_mix"],
        ],
        &mut bass.synth_mix,
        0.0,
        2.0,
    );
    apply_field(
        root,
        &[
            &["bass", "warmth_freq_hz"],
            &["psychoacoustic_bass", "warmth_freq_hz"],
            &["warmth_freq_hz"],
        ],
        &mut bass.warmth_freq_hz,
        140.0,
        420.0,
    );
    apply_field(
        root,
        &[
            &["bass", "warmth_q"],
            &["psychoacoustic_bass", "warmth_q"],
            &["warmth_q"],
        ],
        &mut bass.warmth_q,
        0.3,
        3.0,
    );
    apply_field(
        root,
        &[
            &["bass", "warmth_max_db"],
            &["psychoacoustic_bass", "warmth_max_db"],
            &["warmth_max_db"],
        ],
        &mut bass.warmth_max_db,
        0.0,
        10.0,
    );
}

// ============================================================================
// Embedded XPS 17 9720 Minimum-Phase 4096-Tap Studio FIR Synthesizer
// ============================================================================

/// Measured Dell XPS 17 9720 stock acoustic response (Blue Yeti X, 455 sweeps,
/// 0.076 dB repeatability, `/home/akshaybapat/project/omarchy-audio-tune/REPORT.md`),
/// referenced to 1.0 kHz = 0.0 dB.
const XPS17_MEASURED_ANCHORS: &[(f64, f64)] = &[
    (20.0, -34.0),
    (50.0, -28.0),
    (80.0, -24.0),
    (120.0, -20.0),
    (160.0, -17.2),
    (200.0, -14.8),
    (250.0, -10.5),
    (300.0, -6.6),
    (450.0, -3.0),
    (700.0, -0.8),
    (1000.0, 0.0),
    (1500.0, 4.2),
    (2200.0, 2.0),
    (3000.0, -1.2),
    (4000.0, -3.6),
    (6000.0, -7.8),
    (8000.0, -8.0),
    (10000.0, -13.1),
    (13000.0, -14.0),
    (16000.0, -16.0),
    (20000.0, -20.0),
];

fn mode_target_anchors(mode: StudioMode) -> &'static [(f64, f64)] {
    match mode {
        // Warm, deep, lush studio target (rich upper-bass/low-mid, smooth non-fatiguing presence)
        StudioMode::Deep => &[
            (20.0, 0.0),
            (150.0, 2.8),
            (220.0, 3.2),
            (300.0, 1.8),
            (500.0, 0.6),
            (1000.0, 0.0),
            (1600.0, -0.6),
            (2800.0, -0.8),
            (4500.0, -0.5),
            (7000.0, -0.8),
            (10000.0, -1.8),
            (14000.0, -3.2),
            (20000.0, -5.0),
        ],
        // Balanced wide cinematic target (crisp pinna cues at 6-10 kHz for FDTR-XTC localization)
        StudioMode::Spatial => &[
            (20.0, 0.0),
            (150.0, 1.8),
            (220.0, 2.0),
            (300.0, 1.0),
            (500.0, 0.3),
            (1000.0, 0.0),
            (1600.0, -0.4),
            (3000.0, 0.0),
            (6000.0, 0.2),
            (9000.0, -0.4),
            (12000.0, -1.5),
            (16000.0, -3.0),
            (20000.0, -4.8),
        ],
        // Harman/Olive studio neutral nearfield target
        StudioMode::Reference => &[
            (20.0, 0.0),
            (150.0, 1.0),
            (250.0, 0.8),
            (400.0, 0.3),
            (1000.0, 0.0),
            (2000.0, 0.0),
            (4000.0, -0.2),
            (8000.0, -0.8),
            (12000.0, -1.6),
            (16000.0, -2.6),
            (20000.0, -4.0),
        ],
        // Ultra-clear vocal/podcast target (tames boxiness, elevates 2.5-4.5 kHz intelligibility)
        StudioMode::Voice => &[
            (20.0, -4.0),
            (150.0, -2.5),
            (220.0, -0.5),
            (350.0, 0.2),
            (800.0, 0.0),
            (1500.0, 0.4),
            (2800.0, 1.8),
            (4200.0, 1.5),
            (6500.0, -0.5),
            (9000.0, -2.2),
            (13000.0, -4.0),
            (20000.0, -6.5),
        ],
    }
}

fn log_interp(anchors: &[(f64, f64)], freq_hz: f64) -> f64 {
    if freq_hz <= anchors[0].0 {
        return anchors[0].1;
    }
    let last = anchors.len() - 1;
    if freq_hz >= anchors[last].0 {
        return anchors[last].1;
    }
    let lf = freq_hz.ln();
    for w in anchors.windows(2) {
        if freq_hz <= w[1].0 {
            let l0 = w[0].0.ln();
            let l1 = w[1].0.ln();
            let t = ((lf - l0) / (l1 - l0).max(1e-12)).clamp(0.0, 1.0);
            return w[0].1 + t * (w[1].1 - w[0].1);
        }
    }
    anchors[last].1
}

/// Synthesizes a 4096-tap minimum-phase studio FIR correction kernel for the Dell XPS 17 9720
/// using the real-cepstrum Hilbert-transform method (Oppenheim & Schafer).
///
/// Runs ONLY during initialization (never on the audio thread).
pub fn synthesize_xps17_min_phase_fir(mode: StudioMode, sample_rate: f64) -> Vec<f32> {
    let fs = if sample_rate.is_finite() && sample_rate >= 8000.0 {
        sample_rate
    } else {
        48000.0
    };
    let nfft = CEPSTRUM_NFFT;
    let half = nfft / 2;
    let target = mode_target_anchors(mode);

    // 1. Raw target - measured correction in dB on linear FFT grid
    let mut raw_corr = vec![0.0f64; half + 1];
    let mut freqs = vec![0.0f64; half + 1];
    for k in 0..=half {
        let f = (k as f64) * fs / (nfft as f64);
        freqs[k] = f;
        let f_clamped = f.max(20.0);
        let meas_db = log_interp(XPS17_MEASURED_ANCHORS, f_clamped);
        let tgt_db = log_interp(target, f_clamped);
        raw_corr[k] = tgt_db - meas_db;
    }

    // 2. Smooth correction (~1/6 octave log-frequency window) and bound to safe acoustic rails
    let mut corr_db = vec![0.0f64; half + 1];
    let half_band_ratio = 2.0_f64.powf(1.0 / 12.0); // +/- 1/12 oct = 1/6 oct span
    let df = fs / (nfft as f64);
    for k in 0..=half {
        let f = freqs[k];
        if f < 20.0 {
            corr_db[k] = raw_corr[k];
        } else {
            let k_lo = ((f / half_band_ratio) / df).floor().max(1.0) as usize;
            let k_hi = ((f * half_band_ratio) / df).ceil().min(half as f64) as usize;
            let mut sum = 0.0;
            let mut cnt = 0usize;
            for j in k_lo..=k_hi {
                sum += raw_corr[j];
                cnt += 1;
            }
            corr_db[k] = if cnt > 0 { sum / (cnt as f64) } else { raw_corr[k] };
        }
        // Raw correction rails before loudness normalization
        corr_db[k] = corr_db[k].clamp(-12.0, 8.0);
    }

    // 3. Taper safely outside the trustworthy speaker band:
    //    - Below 160 Hz, taper smoothly toward 0.0 dB by 80 Hz so the FIR NEVER boosts
    //      sub-bass into woofer over-excursion and NEVER fights Stage 1's Klippel sliding HPF!
    //    - Above 14.5 kHz, taper smoothly toward 0.0 dB at Nyquist.
    let k_160 = ((160.0 / df).round() as usize).clamp(1, half);
    let edge_lo = corr_db[k_160].min(6.0);
    let log_80 = 80.0_f64.ln();
    let log_160 = 160.0_f64.ln();
    for k in 0..k_160 {
        let f = freqs[k];
        if f <= 80.0 {
            corr_db[k] = if mode == StudioMode::Voice { -3.0 } else { 0.0 };
        } else {
            let t = ((f.ln() - log_80) / (log_160 - log_80)).clamp(0.0, 1.0);
            let floor_db = if mode == StudioMode::Voice { -3.0 } else { 0.0 };
            corr_db[k] = floor_db + t * (edge_lo - floor_db);
        }
    }

    let k_hi_taper = ((14500.0 / df).round() as usize).clamp(1, half);
    if k_hi_taper < half {
        let edge_hi = corr_db[k_hi_taper];
        let span = (half - k_hi_taper) as f64;
        for k in k_hi_taper..=half {
            let t = (k - k_hi_taper) as f64 / span;
            corr_db[k] = edge_hi * (1.0 - t);
        }
    }

    // 4. Pink-weighted 0 dB loudness-neutral anchor over 100 Hz .. 16 kHz
    let mut pink_num = 0.0;
    let mut pink_den = 0.0;
    for k in 1..=half {
        let f = freqs[k];
        if (100.0..=16000.0).contains(&f) {
            let w = 1.0 / f;
            pink_num += corr_db[k] * w;
            pink_den += w;
        }
    }
    let pink_mean = if pink_den > 0.0 {
        pink_num / pink_den
    } else {
        0.0
    };
    for c in &mut corr_db {
        *c = (*c - pink_mean).clamp(-12.0, 4.0);
    }

    // 5. Real-cepstrum minimum-phase construction
    let mut buf = vec![(0.0f64, 0.0f64); nfft];
    for k in 0..=half {
        let mag = 10.0_f64.powf(corr_db[k] / 20.0).max(1e-6);
        let log_mag = mag.ln();
        buf[k] = (log_mag, 0.0);
        if k > 0 && k < half {
            buf[nfft - k] = (log_mag, 0.0);
        }
    }

    fft_in_place(&mut buf, true); // real cepstrum

    // Fold anticausal cepstrum onto causal side for minimum phase
    buf[0].1 = 0.0;
    for k in 1..half {
        buf[k] = (buf[k].0 * 2.0, 0.0);
    }
    buf[half] = (buf[half].0, 0.0);
    for k in (half + 1)..nfft {
        buf[k] = (0.0, 0.0);
    }

    fft_in_place(&mut buf, false); // complex log minimum-phase spectrum

    for bin in &mut buf {
        let r = bin.0.exp();
        let (s, c) = bin.1.sin_cos();
        *bin = (r * c, r * s);
    }

    fft_in_place(&mut buf, true); // minimum-phase impulse response

    // Truncate to FIR_TAPS with cosine half-Hann tail fade
    let mut taps = vec![0.0f64; FIR_TAPS];
    let fade = FIR_TAPS / 8;
    let fade_start = FIR_TAPS - fade;
    for i in 0..FIR_TAPS {
        let w = if i < fade_start {
            1.0
        } else {
            let phase = std::f64::consts::PI * ((i - fade_start) as f64) / (fade as f64);
            0.5 * (1.0 + phase.cos())
        };
        taps[i] = buf[i].0 * w;
    }

    // 6. Final realized pink-weighted 0 dB trim
    for bin in &mut buf {
        *bin = (0.0, 0.0);
    }
    for i in 0..FIR_TAPS {
        buf[i].0 = taps[i];
    }
    fft_in_place(&mut buf, false);
    let mut r_num = 0.0;
    let mut r_den = 0.0;
    for k in 1..=half {
        let f = freqs[k];
        if (100.0..=16000.0).contains(&f) {
            let mag = (buf[k].0 * buf[k].0 + buf[k].1 * buf[k].1).sqrt().max(1e-9);
            let db = 20.0 * mag.log10();
            let w = 1.0 / f;
            r_num += db * w;
            r_den += w;
        }
    }
    let trim_db = if r_den > 0.0 { r_num / r_den } else { 0.0 };
    let trim_gain = 10.0_f64.powf(-trim_db / 20.0);

    taps.iter()
        .map(|&x| {
            let y = (x * trim_gain) as f32;
            if y.is_finite() { y } else { 0.0 }
        })
        .collect()
}

/// Iterative in-place radix-2 Cooley-Tukey FFT/IFFT (`inverse = true` divides by `N`).
fn fft_in_place(buf: &mut [(f64, f64)], inverse: bool) {
    let n = buf.len();
    debug_assert!(n.is_power_of_two());

    // Bit-reversal permutation
    let mut j = 0usize;
    for i in 1..n {
        let mut bit = n >> 1;
        while j & bit != 0 {
            j ^= bit;
            bit >>= 1;
        }
        j ^= bit;
        if i < j {
            buf.swap(i, j);
        }
    }

    let sign = if inverse { 1.0 } else { -1.0 };
    let mut len = 2usize;
    while len <= n {
        let half = len >> 1;
        let angle = sign * 2.0 * std::f64::consts::PI / (len as f64);
        let (wlen_im, wlen_re) = angle.sin_cos();

        let mut start = 0usize;
        while start < n {
            let mut w_re = 1.0f64;
            let mut w_im = 0.0f64;
            for k in 0..half {
                let u = buf[start + k];
                let v = buf[start + k + half];
                let t_re = v.0 * w_re - v.1 * w_im;
                let t_im = v.0 * w_im + v.1 * w_re;
                buf[start + k] = (u.0 + t_re, u.1 + t_im);
                buf[start + k + half] = (u.0 - t_re, u.1 - t_im);

                let next_re = w_re * wlen_re - w_im * wlen_im;
                let next_im = w_re * wlen_im + w_im * wlen_re;
                w_re = next_re;
                w_im = next_im;
            }
            start += len;
        }
        len <<= 1;
    }

    if inverse {
        let inv_n = 1.0 / (n as f64);
        for bin in buf.iter_mut() {
            bin.0 *= inv_n;
            bin.1 *= inv_n;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_studio_mode_and_control_sanitization() {
        assert_eq!(StudioMode::from_f32(-5.0), StudioMode::Deep);
        assert_eq!(StudioMode::from_f32(0.4), StudioMode::Deep);
        assert_eq!(StudioMode::from_f32(1.0), StudioMode::Spatial);
        assert_eq!(StudioMode::from_f32(2.0), StudioMode::Reference);
        assert_eq!(StudioMode::from_f32(9.0), StudioMode::Voice);
        assert_eq!(StudioMode::from_f32(f32::NAN), StudioMode::Deep);

        let mut p = ControlParams::for_mode(StudioMode::Spatial);
        p.volume_db = f32::NAN;
        p.bass_depth = 99.0;
        p.physical_bass_hz = 10.0;
        p.ceiling_dbfs = 5.0;
        let s = p.sanitized();
        assert_eq!(s.volume_db, -12.0);
        assert_eq!(s.bass_depth, ControlParams::BASS_DEPTH_MAX);
        assert_eq!(s.physical_bass_hz, ControlParams::PHYSICAL_BASS_HZ_MIN);
        assert_eq!(s.ceiling_dbfs, ControlParams::CEILING_DBFS_MAX);
    }

    #[test]
    fn test_wav_f32_roundtrip_and_synth_min_phase_fir() {
        let ir = synthesize_xps17_min_phase_fir(StudioMode::Deep, 48_000.0);
        assert_eq!(ir.len(), FIR_TAPS);
        assert!(ir.iter().all(|v| v.is_finite()));
        // Minimum-phase energy must be heavily concentrated in the causal prefix (first 256 taps)
        let e_early: f64 = ir[..256].iter().map(|&x| (x as f64) * (x as f64)).sum();
        let e_total: f64 = ir.iter().map(|&x| (x as f64) * (x as f64)).sum();
        assert!(
            e_early / e_total > 0.90,
            "synthesized FIR should be minimum-phase causal: early_ratio={:.4}",
            e_early / e_total
        );

        let tmp = std::env::temp_dir().join("xps17_config_roundtrip_test.wav");
        write_wav_stereo_f32(&tmp, 48_000, &ir, &ir).expect("write wav");
        let back = read_wav_stereo(&tmp).expect("read wav");
        let _ = std::fs::remove_file(&tmp);
        assert_eq!(back.sample_rate, 48_000);
        assert_eq!(back.left.len(), FIR_TAPS);
        assert_eq!(back.left, ir);
        assert_eq!(back.right, ir);
    }

    #[test]
    fn test_load_acoustics_bundle_loads_lead2_kernels_when_present() {
        let bundle = load_acoustics_bundle(48_000.0);
        for slot in 0..4 {
            assert_eq!(bundle.kernels[slot].0.len(), FIR_TAPS);
            assert_eq!(bundle.kernels[slot].1.len(), FIR_TAPS);
            assert!(bundle.kernels[slot].0.iter().all(|v| v.is_finite()));
            assert!(bundle.kernels[slot].1.iter().all(|v| v.is_finite()));
        }
        // If Lead 2's kernels exist on disk, verify all 4 loaded from disk
        let kdir = resolve_acoustics_dir().join("kernels");
        if kdir.join("xps17-studio-deep.wav").exists() {
            assert_eq!(bundle.kernel_from_disk, [true, true, true, true]);
        }
    }
}

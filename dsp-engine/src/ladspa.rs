//! LADSPA v1.1 Hard-Real-Time C ABI Export (`libxps17_studio_dsp.so`).
//!
//! ABI Contract (`COORDINATOR-NOTES.md`):
//! - UniqueID: `597201`
//! - Label: `"xps17_studio_dsp"`
//! - Properties: `LADSPA_PROPERTY_HARD_RT_CAPABLE` (`0x04`)
//! - 14 Ports: 4 Audio (`In L`, `In R`, `Out L`, `Out R`) + 10 Control ports
//! - Strictly ZERO heap allocations inside `run()`
//! - In-place buffer aliasing (`In L == Out L`, `In R == Out R`) is 100% safe

use core::ffi::c_void;
use core::ptr;
use libc::{c_char, c_int, c_ulong};

use crate::config::{ControlParams, StudioMode};
use crate::engine::StudioDspEngine;
use crate::simd::FtzDazGuard;

pub type LadspaData = f32;
pub type LadspaProperties = c_int;
pub type LadspaPortDescriptor = c_int;
pub type LadspaPortRangeHintDescriptor = c_int;
pub type LadspaHandle = *mut c_void;

// LADSPA v1.1 Property flags
pub const LADSPA_PROPERTY_REALTIME: LadspaProperties = 0x1;
pub const LADSPA_PROPERTY_INPLACE_BROKEN: LadspaProperties = 0x2;
pub const LADSPA_PROPERTY_HARD_RT_CAPABLE: LadspaProperties = 0x4;

// LADSPA v1.1 Port descriptor flags
pub const LADSPA_PORT_INPUT: LadspaPortDescriptor = 0x1;
pub const LADSPA_PORT_OUTPUT: LadspaPortDescriptor = 0x2;
pub const LADSPA_PORT_CONTROL: LadspaPortDescriptor = 0x4;
pub const LADSPA_PORT_AUDIO: LadspaPortDescriptor = 0x8;

// LADSPA v1.1 Port range hint flags
pub const LADSPA_HINT_BOUNDED_BELOW: LadspaPortRangeHintDescriptor = 0x1;
pub const LADSPA_HINT_BOUNDED_ABOVE: LadspaPortRangeHintDescriptor = 0x2;
pub const LADSPA_HINT_TOGGLED: LadspaPortRangeHintDescriptor = 0x4;
pub const LADSPA_HINT_SAMPLE_RATE: LadspaPortRangeHintDescriptor = 0x8;
pub const LADSPA_HINT_LOGARITHMIC: LadspaPortRangeHintDescriptor = 0x10;
pub const LADSPA_HINT_INTEGER: LadspaPortRangeHintDescriptor = 0x20;
pub const LADSPA_HINT_DEFAULT_NONE: LadspaPortRangeHintDescriptor = 0x0;
pub const LADSPA_HINT_DEFAULT_MINIMUM: LadspaPortRangeHintDescriptor = 0x40;
pub const LADSPA_HINT_DEFAULT_LOW: LadspaPortRangeHintDescriptor = 0x80;
pub const LADSPA_HINT_DEFAULT_MIDDLE: LadspaPortRangeHintDescriptor = 0xC0;
pub const LADSPA_HINT_DEFAULT_HIGH: LadspaPortRangeHintDescriptor = 0x100;
pub const LADSPA_HINT_DEFAULT_MAXIMUM: LadspaPortRangeHintDescriptor = 0x140;
pub const LADSPA_HINT_DEFAULT_0: LadspaPortRangeHintDescriptor = 0x200;
pub const LADSPA_HINT_DEFAULT_1: LadspaPortRangeHintDescriptor = 0x240;

pub const LADSPA_UNIQUE_ID: c_ulong = 597201;
pub const PORT_COUNT: usize = 14;

// Port index constants
pub const PORT_IN_L: usize = 0;
pub const PORT_IN_R: usize = 1;
pub const PORT_OUT_L: usize = 2;
pub const PORT_OUT_R: usize = 3;
pub const PORT_MODE: usize = 4;
pub const PORT_VOLUME_DB: usize = 5;
pub const PORT_BASS_DEPTH: usize = 6;
pub const PORT_PHYSICAL_BASS_HZ: usize = 7;
pub const PORT_WARMTH_BODY: usize = 8;
pub const PORT_STAGE_DEPTH: usize = 9;
pub const PORT_SPATIAL_XTC: usize = 10;
pub const PORT_DRIVE_DB: usize = 11;
pub const PORT_CEILING_DBFS: usize = 12;
pub const PORT_EXCURSION_GUARD: usize = 13;

#[repr(C)]
#[derive(Clone, Copy)]
pub struct LadspaPortRangeHint {
    pub hint_descriptor: LadspaPortRangeHintDescriptor,
    pub lower_bound: LadspaData,
    pub upper_bound: LadspaData,
}

#[repr(C)]
pub struct LadspaDescriptor {
    pub unique_id: c_ulong,
    pub label: *const c_char,
    pub properties: LadspaProperties,
    pub name: *const c_char,
    pub maker: *const c_char,
    pub copyright: *const c_char,
    pub port_count: c_ulong,
    pub port_descriptors: *const LadspaPortDescriptor,
    pub port_names: *const *const c_char,
    pub port_range_hints: *const LadspaPortRangeHint,
    pub implementation_data: *mut c_void,
    pub instantiate: Option<
        unsafe extern "C" fn(
            descriptor: *const LadspaDescriptor,
            sample_rate: c_ulong,
        ) -> LadspaHandle,
    >,
    pub connect_port: Option<
        unsafe extern "C" fn(
            instance: LadspaHandle,
            port: c_ulong,
            data_location: *mut LadspaData,
        ),
    >,
    pub activate: Option<unsafe extern "C" fn(instance: LadspaHandle)>,
    pub run: Option<unsafe extern "C" fn(instance: LadspaHandle, sample_count: c_ulong)>,
    pub run_adding: Option<unsafe extern "C" fn(instance: LadspaHandle, sample_count: c_ulong)>,
    pub set_run_adding_gain: Option<unsafe extern "C" fn(instance: LadspaHandle, gain: LadspaData)>,
    pub deactivate: Option<unsafe extern "C" fn(instance: LadspaHandle)>,
    pub cleanup: Option<unsafe extern "C" fn(instance: LadspaHandle)>,
}

// Static NUL-terminated C strings
const LABEL_CSTR: &[u8] = b"xps17_studio_dsp\0";
const NAME_CSTR: &[u8] = b"XPS 17 9720 Studio Audio Engine\0";
const MAKER_CSTR: &[u8] = b"XPS17 Studio Audio Engineering Stack\0";
const COPYRIGHT_CSTR: &[u8] = b"MIT\0";

const PORT_NAME_0: &[u8] = b"In L\0";
const PORT_NAME_1: &[u8] = b"In R\0";
const PORT_NAME_2: &[u8] = b"Out L\0";
const PORT_NAME_3: &[u8] = b"Out R\0";
const PORT_NAME_4: &[u8] = b"Mode\0";
const PORT_NAME_5: &[u8] = b"Volume (dB)\0";
const PORT_NAME_6: &[u8] = b"Bass Depth\0";
const PORT_NAME_7: &[u8] = b"Physical Bass Hz\0";
const PORT_NAME_8: &[u8] = b"Warmth Body\0";
const PORT_NAME_9: &[u8] = b"Stage Depth\0";
const PORT_NAME_10: &[u8] = b"Spatial XTC\0";
const PORT_NAME_11: &[u8] = b"Drive (dB)\0";
const PORT_NAME_12: &[u8] = b"Ceiling (dBFS)\0";
const PORT_NAME_13: &[u8] = b"Excursion Guard\0";

static PORT_DESCRIPTORS: [LadspaPortDescriptor; PORT_COUNT] = [
    LADSPA_PORT_INPUT | LADSPA_PORT_AUDIO,
    LADSPA_PORT_INPUT | LADSPA_PORT_AUDIO,
    LADSPA_PORT_OUTPUT | LADSPA_PORT_AUDIO,
    LADSPA_PORT_OUTPUT | LADSPA_PORT_AUDIO,
    LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
    LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
    LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
    LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
    LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
    LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
    LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
    LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
    LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
    LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
];

#[repr(transparent)]
struct SyncPortNames([*const c_char; PORT_COUNT]);
unsafe impl Sync for SyncPortNames {}

static PORT_NAMES: SyncPortNames = SyncPortNames([
    PORT_NAME_0.as_ptr() as *const c_char,
    PORT_NAME_1.as_ptr() as *const c_char,
    PORT_NAME_2.as_ptr() as *const c_char,
    PORT_NAME_3.as_ptr() as *const c_char,
    PORT_NAME_4.as_ptr() as *const c_char,
    PORT_NAME_5.as_ptr() as *const c_char,
    PORT_NAME_6.as_ptr() as *const c_char,
    PORT_NAME_7.as_ptr() as *const c_char,
    PORT_NAME_8.as_ptr() as *const c_char,
    PORT_NAME_9.as_ptr() as *const c_char,
    PORT_NAME_10.as_ptr() as *const c_char,
    PORT_NAME_11.as_ptr() as *const c_char,
    PORT_NAME_12.as_ptr() as *const c_char,
    PORT_NAME_13.as_ptr() as *const c_char,
]);

static PORT_RANGE_HINTS: [LadspaPortRangeHint; PORT_COUNT] = [
    // 0..4: Audio ports (no range hints)
    LadspaPortRangeHint {
        hint_descriptor: LADSPA_HINT_DEFAULT_NONE,
        lower_bound: 0.0,
        upper_bound: 0.0,
    },
    LadspaPortRangeHint {
        hint_descriptor: LADSPA_HINT_DEFAULT_NONE,
        lower_bound: 0.0,
        upper_bound: 0.0,
    },
    LadspaPortRangeHint {
        hint_descriptor: LADSPA_HINT_DEFAULT_NONE,
        lower_bound: 0.0,
        upper_bound: 0.0,
    },
    LadspaPortRangeHint {
        hint_descriptor: LADSPA_HINT_DEFAULT_NONE,
        lower_bound: 0.0,
        upper_bound: 0.0,
    },
    // 4: Mode (0..=3, INTEGER, default 0)
    LadspaPortRangeHint {
        hint_descriptor: LADSPA_HINT_BOUNDED_BELOW
            | LADSPA_HINT_BOUNDED_ABOVE
            | LADSPA_HINT_INTEGER
            | LADSPA_HINT_DEFAULT_0,
        lower_bound: 0.0,
        upper_bound: 3.0,
    },
    // 5: Volume (dB) (-45.0..=0.0)
    LadspaPortRangeHint {
        hint_descriptor: LADSPA_HINT_BOUNDED_BELOW
            | LADSPA_HINT_BOUNDED_ABOVE
            | LADSPA_HINT_DEFAULT_HIGH,
        lower_bound: ControlParams::VOLUME_DB_MIN,
        upper_bound: ControlParams::VOLUME_DB_MAX,
    },
    // 6: Bass Depth (0.0..=1.5)
    LadspaPortRangeHint {
        hint_descriptor: LADSPA_HINT_BOUNDED_BELOW
            | LADSPA_HINT_BOUNDED_ABOVE
            | LADSPA_HINT_DEFAULT_1,
        lower_bound: ControlParams::BASS_DEPTH_MIN,
        upper_bound: ControlParams::BASS_DEPTH_MAX,
    },
    // 7: Physical Bass Hz (65.0..=180.0)
    LadspaPortRangeHint {
        hint_descriptor: LADSPA_HINT_BOUNDED_BELOW
            | LADSPA_HINT_BOUNDED_ABOVE
            | LADSPA_HINT_LOGARITHMIC
            | LADSPA_HINT_DEFAULT_LOW,
        lower_bound: ControlParams::PHYSICAL_BASS_HZ_MIN,
        upper_bound: ControlParams::PHYSICAL_BASS_HZ_MAX,
    },
    // 8: Warmth Body (0.0..=1.5)
    LadspaPortRangeHint {
        hint_descriptor: LADSPA_HINT_BOUNDED_BELOW
            | LADSPA_HINT_BOUNDED_ABOVE
            | LADSPA_HINT_DEFAULT_1,
        lower_bound: ControlParams::WARMTH_BODY_MIN,
        upper_bound: ControlParams::WARMTH_BODY_MAX,
    },
    // 9: Stage Depth (0.0..=1.5)
    LadspaPortRangeHint {
        hint_descriptor: LADSPA_HINT_BOUNDED_BELOW
            | LADSPA_HINT_BOUNDED_ABOVE
            | LADSPA_HINT_DEFAULT_MIDDLE,
        lower_bound: ControlParams::STAGE_DEPTH_MIN,
        upper_bound: ControlParams::STAGE_DEPTH_MAX,
    },
    // 10: Spatial XTC (0.0..=1.0)
    LadspaPortRangeHint {
        hint_descriptor: LADSPA_HINT_BOUNDED_BELOW
            | LADSPA_HINT_BOUNDED_ABOVE
            | LADSPA_HINT_DEFAULT_0,
        lower_bound: ControlParams::SPATIAL_XTC_MIN,
        upper_bound: ControlParams::SPATIAL_XTC_MAX,
    },
    // 11: Drive (dB) (0.0..=12.0)
    LadspaPortRangeHint {
        hint_descriptor: LADSPA_HINT_BOUNDED_BELOW
            | LADSPA_HINT_BOUNDED_ABOVE
            | LADSPA_HINT_DEFAULT_MIDDLE,
        lower_bound: ControlParams::DRIVE_DB_MIN,
        upper_bound: ControlParams::DRIVE_DB_MAX,
    },
    // 12: Ceiling (dBFS) (-9.0..=-0.3)
    LadspaPortRangeHint {
        hint_descriptor: LADSPA_HINT_BOUNDED_BELOW
            | LADSPA_HINT_BOUNDED_ABOVE
            | LADSPA_HINT_DEFAULT_HIGH,
        lower_bound: ControlParams::CEILING_DBFS_MIN,
        upper_bound: ControlParams::CEILING_DBFS_MAX,
    },
    // 13: Excursion Guard (0.0..=1.0)
    LadspaPortRangeHint {
        hint_descriptor: LADSPA_HINT_BOUNDED_BELOW
            | LADSPA_HINT_BOUNDED_ABOVE
            | LADSPA_HINT_DEFAULT_1,
        lower_bound: ControlParams::EXCURSION_GUARD_MIN,
        upper_bound: ControlParams::EXCURSION_GUARD_MAX,
    },
];

#[repr(transparent)]
struct SyncDescriptor(LadspaDescriptor);
unsafe impl Sync for SyncDescriptor {}

static DESCRIPTOR: SyncDescriptor = SyncDescriptor(LadspaDescriptor {
    unique_id: LADSPA_UNIQUE_ID,
    label: LABEL_CSTR.as_ptr() as *const c_char,
    properties: LADSPA_PROPERTY_HARD_RT_CAPABLE,
    name: NAME_CSTR.as_ptr() as *const c_char,
    maker: MAKER_CSTR.as_ptr() as *const c_char,
    copyright: COPYRIGHT_CSTR.as_ptr() as *const c_char,
    port_count: PORT_COUNT as c_ulong,
    port_descriptors: PORT_DESCRIPTORS.as_ptr(),
    port_names: PORT_NAMES.0.as_ptr(),
    port_range_hints: PORT_RANGE_HINTS.as_ptr(),
    implementation_data: ptr::null_mut(),
    instantiate: Some(ladspa_instantiate),
    connect_port: Some(ladspa_connect_port),
    activate: Some(ladspa_activate),
    run: Some(ladspa_run),
    run_adding: None,
    set_run_adding_gain: None,
    deactivate: Some(ladspa_deactivate),
    cleanup: Some(ladspa_cleanup),
});

struct LadspaInstance {
    engine: StudioDspEngine,
    in_l: *const LadspaData,
    in_r: *const LadspaData,
    out_l: *mut LadspaData,
    out_r: *mut LadspaData,
    controls: [*const LadspaData; 10],
}

#[inline]
unsafe fn read_control_or(ptr: *const LadspaData, fallback: f32) -> f32 {
    if ptr.is_null() {
        fallback
    } else {
        *ptr
    }
}

unsafe extern "C" fn ladspa_instantiate(
    _descriptor: *const LadspaDescriptor,
    sample_rate: c_ulong,
) -> LadspaHandle {
    let fs = if sample_rate >= 8000 {
        sample_rate as f64
    } else {
        48000.0
    };
    let inst = Box::new(LadspaInstance {
        engine: StudioDspEngine::new(fs),
        in_l: ptr::null(),
        in_r: ptr::null(),
        out_l: ptr::null_mut(),
        out_r: ptr::null_mut(),
        controls: [ptr::null(); 10],
    });
    Box::into_raw(inst) as LadspaHandle
}

unsafe extern "C" fn ladspa_connect_port(
    instance: LadspaHandle,
    port: c_ulong,
    data_location: *mut LadspaData,
) {
    if instance.is_null() {
        return;
    }
    let inst = &mut *(instance as *mut LadspaInstance);
    match port as usize {
        PORT_IN_L => inst.in_l = data_location as *const LadspaData,
        PORT_IN_R => inst.in_r = data_location as *const LadspaData,
        PORT_OUT_L => inst.out_l = data_location,
        PORT_OUT_R => inst.out_r = data_location,
        p @ PORT_MODE..=PORT_EXCURSION_GUARD => {
            inst.controls[p - PORT_MODE] = data_location as *const LadspaData;
        }
        _ => {}
    }
}

unsafe extern "C" fn ladspa_activate(instance: LadspaHandle) {
    if instance.is_null() {
        return;
    }
    let inst = &mut *(instance as *mut LadspaInstance);
    inst.engine.reset();
}

unsafe extern "C" fn ladspa_run(instance: LadspaHandle, sample_count: c_ulong) {
    if instance.is_null() || sample_count == 0 {
        return;
    }
    let inst = &mut *(instance as *mut LadspaInstance);
    let frames = sample_count as usize;

    // If audio buffers are not fully connected, zero any connected output safely.
    if inst.in_l.is_null() || inst.in_r.is_null() || inst.out_l.is_null() || inst.out_r.is_null() {
        if !inst.out_l.is_null() {
            ptr::write_bytes(inst.out_l, 0, frames);
        }
        if !inst.out_r.is_null() {
            ptr::write_bytes(inst.out_r, 0, frames);
        }
        return;
    }

    let _ftz = FtzDazGuard::enter();

    let mode = if inst.controls[0].is_null() {
        StudioMode::Deep
    } else {
        StudioMode::from_f32(*inst.controls[0])
    };
    let def = ControlParams::for_mode(mode);

    let params = ControlParams {
        mode,
        volume_db: read_control_or(inst.controls[1], def.volume_db),
        bass_depth: read_control_or(inst.controls[2], def.bass_depth),
        physical_bass_hz: read_control_or(inst.controls[3], def.physical_bass_hz),
        warmth_body: read_control_or(inst.controls[4], def.warmth_body),
        stage_depth: read_control_or(inst.controls[5], def.stage_depth),
        spatial_xtc: read_control_or(inst.controls[6], def.spatial_xtc),
        drive_db: read_control_or(inst.controls[7], def.drive_db),
        ceiling_dbfs: read_control_or(inst.controls[8], def.ceiling_dbfs),
        excursion_guard: read_control_or(inst.controls[9], def.excursion_guard),
    };

    // Aliasing-safe, strictly zero-allocation stereo processing.
    inst.engine
        .process_raw_ptrs(inst.in_l, inst.in_r, inst.out_l, inst.out_r, frames, &params);
}

unsafe extern "C" fn ladspa_deactivate(_instance: LadspaHandle) {
    // Hard-RT safe no-op; state is cleanly reset on next activate().
}

unsafe extern "C" fn ladspa_cleanup(instance: LadspaHandle) {
    if instance.is_null() {
        return;
    }
    drop(Box::from_raw(instance as *mut LadspaInstance));
}

/// Official LADSPA v1.1 shared library entry point.
#[no_mangle]
pub extern "C" fn ladspa_descriptor(index: c_ulong) -> *const LadspaDescriptor {
    if index == 0 {
        &DESCRIPTOR.0 as *const LadspaDescriptor
    } else {
        ptr::null()
    }
}

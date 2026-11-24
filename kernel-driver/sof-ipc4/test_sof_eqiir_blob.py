#!/usr/bin/env python3
"""Comprehensive verification suite for SOF IPC4 EQIIR binary blobs & generator."""
from __future__ import annotations

import hashlib
import json
import math
import struct
import sys
import unittest
import zlib
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import gen_sof_eqiir_blob as eq

BLOBS_DIR = ROOT_DIR / "blobs"
UPSTREAM_IPC4_EQIIR_DIR = Path("/usr/share/alsa/ucm2/blobs/sof/ipc4/eq_iir")
TPLG_PATH = Path("/usr/lib/firmware/intel/sof-ipc4-tplg/sof-adl-rt711-l0-rt1316-l12-rt714-l3.tplg")


def simulate_sof_df2t_int32(
    sections: tuple[eq.SofDf2tBiquad, ...],
    samples_q31: list[int],
) -> tuple[list[int], int, int]:
    """Simulate SOF's fixed-point 32x32->64 DF2T biquad cascade in exact integer arithmetic.

    Returns (output_samples_q31, internal_acc_saturations, output_saturations).
    """
    acc_sats = 0
    out_sats = 0
    cur = samples_q31
    for bq in sections:
        d0 = 0  # int64 state (Q2.61)
        d1 = 0  # int64 state (Q2.61)
        nxt: list[int] = []
        a1, a2 = bq.a1, bq.a2
        b0, b1, b2 = bq.b0, bq.b1, bq.b2
        gain = bq.output_gain
        shift = bq.output_shift
        for x in cur:
            # acc = round((b0 * x + d0) / 2^30) -> Q1.31 (6 dB headroom before output_gain)
            acc64 = b0 * x + d0
            acc = (acc64 + (1 << 29)) >> 30
            if acc > eq.INT32_MAX:
                acc = eq.INT32_MAX
                acc_sats += 1
            elif acc < eq.INT32_MIN:
                acc = eq.INT32_MIN
                acc_sats += 1

            # Update DF2T state registers (SOF sign convention: +a1*acc, +a2*acc)
            d0 = b1 * x + a1 * acc + d1
            d1 = b2 * x + a2 * acc

            # Output scaling: Q1.31 * Q2.14 >> (14 + output_shift)
            scaled64 = acc * gain
            total_rshift = 14 + shift
            if total_rshift >= 0:
                y = (scaled64 + (1 << (total_rshift - 1))) >> total_rshift if total_rshift > 0 else scaled64
            else:
                y = scaled64 << (-total_rshift)

            if y > eq.INT32_MAX:
                y = eq.INT32_MAX
                out_sats += 1
            elif y < eq.INT32_MIN:
                y = eq.INT32_MIN
                out_sats += 1
            nxt.append(y)
        cur = nxt
    return cur, acc_sats, out_sats


def extract_tplg_eqiir_21_1(tplg_path: Path) -> tuple[int, bytes]:
    """Extract (max_size, priv_bytes) for `Post Mixer Speaker Playback IIR Eq bytes` from .tplg."""
    data = tplg_path.read_bytes()
    off = 0
    while off + 36 <= len(data):
        magic, _abi, _ver, typ, hdr_sz, _vtype, pay_sz, _idx, cnt = struct.unpack_from("<9I", data, off)
        assert magic == 0x41536F43, f"Bad tplg magic 0x{magic:08x}"
        payload = data[off + hdr_sz : off + hdr_sz + pay_sz]
        if typ == 5:  # SND_SOC_TPLG_TYPE_DAPM_WIDGET
            po = 0
            for _ in range(cnt):
                wsz, _wid = struct.unpack_from("<2I", payload, po)
                num_kctls = struct.unpack_from("<I", payload, po + wsz - 8)[0]
                priv_sz = struct.unpack_from("<I", payload, po + wsz - 4)[0]
                po += wsz + priv_sz
                for _ in range(num_kctls):
                    khdr_sz, ktype = struct.unpack_from("<2I", payload, po)
                    kname = payload[po + 8 : po + 52].split(b"\x00")[0].decode("ascii", "replace")
                    c_size = struct.unpack_from("<I", payload, po + khdr_sz)[0]
                    kpriv_sz = struct.unpack_from("<I", payload, po + c_size - 4)[0]
                    kpriv = payload[po + c_size : po + c_size + kpriv_sz]
                    if ktype == 2 and kname == "Post Mixer Speaker Playback IIR Eq bytes":
                        c_max = struct.unpack_from("<I", payload, po + khdr_sz + 4)[0]
                        return c_max, kpriv
                    po += c_size + kpriv_sz
        off += hdr_sz + pay_sz
    raise AssertionError("Post Mixer Speaker Playback IIR Eq bytes not found in topology")


class TestSofEqIirBlobs(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.built = eq.build_xps17_blobs(fs=eq.SAMPLE_RATE_HZ)
        cls.grid = eq.log_frequency_grid(20.0, 20000.0, 1000)

    def test_binary_abi_header_and_alignment_invariants(self) -> None:
        expected_sizes = {
            "eqiir_subsonic_80hz_safety.bin": 156,     # 8 + 32 + (28 + 8 + 24 + 2*28)
            "eqiir_subsonic_plus_preshape.bin": 184,   # 8 + 32 + (28 + 8 + 24 + 3*28)
            "eqiir_passthrough_flat.bin": 128,         # 8 + 32 + (28 + 8 + 24 + 1*28)
        }
        for fname, exp_len in expected_sizes.items():
            blob, meta = self.built[fname]
            self.assertEqual(len(blob), exp_len, fname)
            self.assertEqual(len(blob) % 4, 0, fname)

            parsed = eq.parse_sof_eqiir_blob(blob)
            # Matches snd-sof.ko sof_ipc4_bytes_ext_put exact validation checks
            self.assertEqual(parsed.tlv_type, eq.ALSA_TLV_TYPE_SOF_BINARY)
            self.assertEqual(parsed.tlv_length, len(blob) - 8)
            self.assertLessEqual(parsed.tlv_length, eq.SOF_IPC4_EQIIR_MAX_TLV_LEN)
            self.assertEqual(parsed.abi_magic, eq.SOF_IPC4_ABI_MAGIC)
            self.assertEqual(parsed.abi_type, eq.SOF_IPC4_EQ_IIR_PARAM_ID)
            self.assertEqual(parsed.abi_size, parsed.tlv_length - 32)
            self.assertEqual(parsed.abi_size, parsed.cfg_size)
            self.assertEqual((parsed.abi_version >> 24) & 0xFF, 3)  # SOF_ABI_MAJOR == 3
            self.assertEqual(parsed.abi_reserved, (0, 0, 0, 0))
            self.assertEqual(parsed.cfg_reserved, (0, 0, 0, 0))
            self.assertEqual(parsed.channels_in_config, 2)
            self.assertEqual(parsed.number_of_responses, 1)

            num_sec, num_series, biqs = parsed.responses[0]
            self.assertEqual(num_sec, num_series)
            self.assertEqual(len(biqs), num_sec)
            for bq in biqs:
                self.assertEqual(bq.output_shift, 0)
                self.assertGreaterEqual(bq.output_gain, 1)
                self.assertLessEqual(bq.output_gain, 32767)
                for c in (bq.a2, bq.a1, bq.b2, bq.b1, bq.b0):
                    self.assertGreaterEqual(c, eq.INT32_MIN)
                    self.assertLessEqual(c, eq.INT32_MAX)

            if fname == "eqiir_passthrough_flat.bin":
                self.assertEqual(parsed.assign_response, (-1, -1))
            else:
                self.assertEqual(parsed.assign_response, (0, 0))

            # Metadata checksum integrity
            self.assertEqual(meta["sha256"], hashlib.sha256(blob).hexdigest())
            self.assertEqual(meta["crc32"], f"0x{zlib.crc32(blob) & 0xFFFFFFFF:08x}")

    def test_subsonic_80hz_safety_response_and_stability(self) -> None:
        blob, meta = self.built["eqiir_subsonic_80hz_safety.bin"]
        parsed = eq.parse_sof_eqiir_blob(blob)
        _, _, biqs = parsed.responses[0]
        f_secs = eq.design_butterworth_hpf_biquads(80.0, order=4)

        # Stability: all poles strictly inside unit circle
        for fb, sb in zip(f_secs, biqs):
            self.assertLess(fb.max_pole_radius(), 0.9999)
            self.assertLess(sb.max_pole_radius(), 0.9999)

        # Acoustic targets
        mag_40 = eq.cascade_mag_db(biqs, 40.0)
        mag_80 = eq.cascade_mag_db(biqs, 80.0)
        self.assertLessEqual(mag_40, -18.0, f"40 Hz attenuation insufficient: {mag_40:.2f} dB")
        self.assertAlmostEqual(mag_80, -3.0103, delta=0.2, msg=f"80 Hz cutoff off: {mag_80:.2f} dB")

        # Passband flatness (250 Hz .. 20 kHz within 0.0 +/- 0.05 dB)
        passband = [eq.cascade_mag_db(biqs, f) for f in self.grid if f >= 250.0]
        for db in passband:
            self.assertAlmostEqual(db, 0.0, delta=0.05)

        # Zero clipping risk: peak gain <= +0.5 dB across entire 20 Hz .. 20 kHz
        all_quant = [eq.cascade_mag_db(biqs, f) for f in self.grid]
        all_float = [eq.cascade_mag_db(f_secs, f) for f in self.grid]
        self.assertLessEqual(max(all_quant), 0.5)
        self.assertLessEqual(meta["intermediate_stage_peak_gain_db"], 0.05)

        # Quantization error < 0.05 dB across 20 Hz .. 20 kHz
        max_err = max(abs(a - b) for a, b in zip(all_float, all_quant))
        self.assertLess(max_err, 0.05, f"Q2.30 error {max_err:.5f} dB >= 0.05 dB")

    def test_subsonic_plus_preshape_response_and_stability(self) -> None:
        blob, meta = self.built["eqiir_subsonic_plus_preshape.bin"]
        parsed = eq.parse_sof_eqiir_blob(blob)
        _, _, biqs = parsed.responses[0]
        f_secs = eq.design_butterworth_hpf_biquads(80.0, order=4) + [
            eq.design_peaking_eq_biquad(1500.0, -2.5, 1.4)
        ]
        self.assertEqual(len(biqs), 3)

        for fb, sb in zip(f_secs, biqs):
            self.assertLess(fb.max_pole_radius(), 0.9999)
            self.assertLess(sb.max_pole_radius(), 0.9999)

        self.assertLessEqual(eq.cascade_mag_db(biqs, 40.0), -18.0)
        self.assertAlmostEqual(eq.cascade_mag_db(biqs, 80.0), -3.0103, delta=0.2)
        self.assertAlmostEqual(eq.cascade_mag_db(biqs, 1500.0), -2.50, delta=0.05)

        # Strictly <= +0.5 dB peak gain everywhere (never clips before PipeWire/amp!)
        all_quant = [eq.cascade_mag_db(biqs, f) for f in self.grid]
        all_float = [eq.cascade_mag_db(f_secs, f) for f in self.grid]
        self.assertLessEqual(max(all_quant), 0.5)
        self.assertLessEqual(meta["intermediate_stage_peak_gain_db"], 0.05)

        max_err = max(abs(a - b) for a, b in zip(all_float, all_quant))
        self.assertLess(max_err, 0.05)

    def test_passthrough_blob_unity_and_bypass(self) -> None:
        blob, _ = self.built["eqiir_passthrough_flat.bin"]
        parsed = eq.parse_sof_eqiir_blob(blob)
        self.assertEqual(parsed.assign_response, (-1, -1))
        _, _, biqs = parsed.responses[0]
        self.assertEqual(len(biqs), 1)
        for f in (20.0, 80.0, 1000.0, 10000.0, 20000.0):
            self.assertAlmostEqual(eq.cascade_mag_db(biqs, f), 0.0, delta=0.001)

    def test_fixed_point_df2t_time_domain_simulation_zero_clipping(self) -> None:
        """Feed 0 dBFS step/impulse & tones through exact SOF int64/Q2.30/Q2.14 DF2T arithmetic."""
        fs = eq.SAMPLE_RATE_HZ
        amp_step = eq.INT32_MAX
        amp_ceiling = int((10.0 ** (-1.1 / 20.0)) * eq.INT32_MAX)  # below -1.0 dBFS PipeWire ceiling
        amp_steady = int(0.99 * eq.INT32_MAX)                      # -0.09 dBFS steady-state

        for fname in ("eqiir_subsonic_80hz_safety.bin", "eqiir_subsonic_plus_preshape.bin"):
            blob, _ = self.built[fname]
            _, _, biqs = eq.parse_sof_eqiir_blob(blob).responses[0]

            # 1) Full-scale 0.0 dBFS unit step & impulse: internal accumulator MUST NEVER clip
            step_sig = [amp_step] * 2400
            _, acc_sats_step, _ = simulate_sof_df2t_int32(biqs, step_sig)
            self.assertEqual(acc_sats_step, 0, f"{fname} internal DF2T acc saturated on 0 dBFS step")

            imp_sig = [amp_step] + [0] * 2399
            _, acc_sats_imp, out_sats_imp = simulate_sof_df2t_int32(biqs, imp_sig)
            self.assertEqual(acc_sats_imp, 0, f"{fname} internal DF2T acc saturated on 0 dBFS impulse")
            self.assertEqual(out_sats_imp, 0, f"{fname} output saturated on 0 dBFS impulse")

            # 2) Abrupt unwindowed -1.1 dBFS bursts (respecting PipeWire's -1.0 dBFS ceiling): ZERO clipping
            for freq in (40.0, 80.0, 200.0, 1000.0, 1500.0, 8000.0):
                n_total = 4800  # 100 ms
                n_settle = 2400
                x_burst = [int(round(amp_ceiling * math.sin(2.0 * math.pi * freq * n / fs))) for n in range(n_total)]
                y_burst, acc_sats, out_sats = simulate_sof_df2t_int32(biqs, x_burst)
                self.assertEqual(acc_sats, 0, f"{fname} internal acc saturated on {freq} Hz burst")
                self.assertEqual(out_sats, 0, f"{fname} output saturated on -1.1 dBFS {freq} Hz burst")

                # Verify exact steady-state RMS gain vs analytical transfer function
                rms_in = math.sqrt(sum(v * v for v in x_burst[n_settle:]) / (n_total - n_settle))
                rms_out = math.sqrt(sum(v * v for v in y_burst[n_settle:]) / (n_total - n_settle))
                sim_db = 20.0 * math.log10(max(rms_out / rms_in, 1e-12))
                expected_db = eq.cascade_mag_db(biqs, freq, fs)
                self.assertAlmostEqual(
                    sim_db,
                    expected_db,
                    delta=0.01,
                    msg=f"{fname} @ {freq} Hz: sim={sim_db:.4f} dB vs analytic={expected_db:.4f} dB",
                )

                # 3) Steady-state -0.09 dBFS after smooth 50 ms raised-cosine onset: ZERO clipping
                n_ramp = 2400
                n_long = 7200
                x_ramp = [
                    int(
                        round(
                            amp_steady
                            * (0.5 * (1.0 - math.cos(math.pi * n / n_ramp)) if n < n_ramp else 1.0)
                            * math.sin(2.0 * math.pi * freq * n / fs)
                        )
                    )
                    for n in range(n_long)
                ]
                _, acc_sats_r, out_sats_r = simulate_sof_df2t_int32(biqs, x_ramp)
                self.assertEqual(acc_sats_r, 0, f"{fname} acc saturated on ramped {freq} Hz")
                self.assertEqual(out_sats_r, 0, f"{fname} output saturated on ramped -0.09 dBFS {freq} Hz")

    def test_cross_verify_upstream_ipc4_blobs_and_topology(self) -> None:
        """Verify our parser & ABI match REAL upstream alsa-ucm-conf blobs and XPS 17 .tplg."""
        self.assertTrue(UPSTREAM_IPC4_EQIIR_DIR.is_dir(), "Missing upstream UCM2 IPC4 eq_iir blobs")
        for p in sorted(UPSTREAM_IPC4_EQIIR_DIR.glob("*.bin")):
            parsed = eq.parse_sof_eqiir_blob(p.read_bytes())
            self.assertEqual(parsed.abi_magic, eq.SOF_IPC4_ABI_MAGIC)
            self.assertEqual(parsed.tlv_type, eq.ALSA_TLV_TYPE_SOF_BINARY)
            self.assertEqual(parsed.abi_type, eq.SOF_IPC4_EQ_IIR_PARAM_ID)

        # Reproduce upstream 100 Hz 2nd-order HPF with our generator and confirm < 0.005 dB match
        up_100 = eq.parse_sof_eqiir_blob((UPSTREAM_IPC4_EQIIR_DIR / "highpass_100hz_0db_48khz.bin").read_bytes())
        our_100_biqs = [eq.quantize_biquad_sof_df2t(b) for b in eq.design_butterworth_hpf_biquads(100.0, order=2)]
        our_100_blob = eq.serialize_sof_ipc4_eqiir_blob(our_100_biqs)
        self.assertEqual(len(our_100_blob), 128)
        for f in (20.0, 50.0, 100.0, 200.0, 1000.0, 10000.0):
            up_db = eq.cascade_mag_db(up_100.responses[0][2], f)
            our_db = eq.cascade_mag_db(our_100_biqs, f)
            self.assertAlmostEqual(up_db, our_db, delta=0.005)

        # Verify against real topology file on disk
        self.assertTrue(TPLG_PATH.is_file(), f"Missing topology {TPLG_PATH}")
        max_sz, priv = extract_tplg_eqiir_21_1(TPLG_PATH)
        self.assertEqual(max_sz, eq.SOF_IPC4_EQIIR_MAX_TLV_LEN)
        wrapped_tplg = struct.pack("<2I", eq.ALSA_TLV_TYPE_SOF_BINARY, len(priv)) + priv
        tplg_parsed = eq.parse_sof_eqiir_blob(wrapped_tplg)
        self.assertEqual(tplg_parsed.abi_magic, eq.SOF_IPC4_ABI_MAGIC)
        self.assertEqual(tplg_parsed.abi_version, eq.SOF_ABI_VERSION_IPC4)
        self.assertEqual(tplg_parsed.abi_type, eq.SOF_IPC4_EQ_IIR_PARAM_ID)

    def test_staged_blobs_on_disk_match_metadata_and_regeneration(self) -> None:
        meta_path = BLOBS_DIR / "blobs_metadata.json"
        self.assertTrue(meta_path.is_file(), "blobs_metadata.json missing — run gen_sof_eqiir_blob.py")
        manifest = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(set(manifest["blobs"].keys()), set(self.built.keys()))
        for fname, (expected_bytes, expected_meta) in self.built.items():
            disk_bytes = (BLOBS_DIR / fname).read_bytes()
            self.assertEqual(disk_bytes, expected_bytes, f"Staged {fname} out of sync with generator")
            self.assertEqual(manifest["blobs"][fname]["sha256"], expected_meta["sha256"])
            self.assertEqual(manifest["blobs"][fname]["crc32"], expected_meta["crc32"])

    def test_negative_validation_rejects_corrupt_and_unstable_inputs(self) -> None:
        good_blob, _ = self.built["eqiir_subsonic_80hz_safety.bin"]

        # Corrupt TLV type (must be 3)
        bad_tlv = bytearray(good_blob)
        struct.pack_into("<I", bad_tlv, 0, 0)
        with self.assertRaises(ValueError):
            eq.parse_sof_eqiir_blob(bytes(bad_tlv))

        # Corrupt ABI magic
        bad_magic = bytearray(good_blob)
        struct.pack_into("<I", bad_magic, 8, 0xDEADBEEF)
        with self.assertRaises(ValueError):
            eq.parse_sof_eqiir_blob(bytes(bad_magic))

        # Truncated blob
        with self.assertRaises(ValueError):
            eq.parse_sof_eqiir_blob(good_blob[:-4])

        # Unstable biquad (pole radius > 1)
        unstable = eq.FloatBiquad("unstable", b0=1.0, b1=0.0, b2=0.0, a1=-1.5, a2=1.05)
        with self.assertRaises(ValueError):
            eq.quantize_biquad_sof_df2t(unstable)


if __name__ == "__main__":
    unittest.main(verbosity=2)

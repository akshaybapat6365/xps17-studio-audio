# Dell XPS 17 9720 (`SUBSYS 0AFF1028` / `Stradale`) — Silicon & Windows Reverse-Engineering Audit

**Auditor:** `rt1316-silicon-auditor` (Lead 1 — Hardware, Kernel Driver, SoundWire & SOF IPC4)
**Target Hardware:** Dell XPS 17 9720 (`DMI product_sku = 0AFF`, `SUBSYS 0AFF1028`, Alder Lake cAVS 2.5, Dual Realtek `ALC1316`/`ALC1319D` `rt1316-sdca` on SoundWire Links 1 & 2, 2× 2 W avg / 2.5 W peak woofers + 2× 2 W avg / 2.5 W peak tweeters)
**Windows Reference Package:** Dell Realtek HDA Driver `10.0.251.7, A85 (W0C3W, 2026-07-20)` mapped via `RTKSdWHwCodec.ini:261-264` to `SDCA_06_Alc1316_Aggr{LL,RR}_SM_0AFE1028`, `RTAIODAT` entry 13 (`RTSPKPT`), and Waves `StradaleMLK_ADL_MAPro11_DSP_{EFX,MFX,SFX}_Render.mps` (`DeployTable.utf8.ini:6273`, `[10280AFF_Win10RS2]`).
**Reproducible Parser & Machine-Readable Twin:**
- Analyzer: [`analyze_windows_tables.py`](file:///home/akshaybapat/project/xps17-studio-audio/kernel-driver/audit/analyze_windows_tables.py) (verified on E-cores CPUs 12–19, exit 0)
- Machine-readable JSON: [`waves_stradale_summary.json`](file:///home/akshaybapat/project/xps17-studio-audio/kernel-driver/audit/waves_stradale_summary.json)

**Epistemic Tags Used Throughout:**
- **PROVEN**: Directly verified from source file:line, live register dump, or deterministic parser output.
- **INFERRED**: Reasoned from proven primary evidence (confidence stated).
- **UNKNOWN**: Not determinable without hardware measurement or closed-binary disassembly.

---

## Executive Summary of Critical Findings

1. **Both In-Amp Hardware Crossovers Are Switched OFF on Linux (`0xC202 = 0x00`, PROVEN):**
   - Windows enables the in-amp 2-way biquad crossover at every stream open (`0xC202 = 0xC0`, then `0xC200 = 0xC0` update pulse; `EP001_Open/OpenCtl.txt:3-4`) and disables it on close (`0xC202 = 0x00`, then `0xC200 = 0xC0`; `CloseCtl.txt:8-9`).
   - Upstream Linux `dell_0b00_bq_params[]` (`soc_sdw_rt_amp_coeff_tables.h:163-298`) writes all **134 registers** (`0xC203..0xC252`, `0xC00B`, `0xC380..0xC3A3`, `0xC261..0xC270`, `0xC200`) — **never `0xC202`**. Both live chip reads show `0000c202: 00` (`amp1.registers.txt:538`, `amp2.registers.txt:538`).
   - Additionally, `rt1316_sdw_component_probe()` (`rt1316-sdw.c:599-607`) misses whichever amp enumerates after card probe: live Amp 1 holds `134/134` Dell bytes (`0xC200 = 0xC0`), whereas live Amp 2 holds reset identity biquads (`08 00 00 00`, `0xC200 = 0x40`, only `34/134` accidental zero/default matches).
   - Furthermore, `0xC200` is non-volatile in stock `rt1316-sdw.c:166-180`: `regcache_sync()` replays cached `0xC200 = 0xC0` in ascending address order **before** `0xC203..0xC252`, risking latching stale/reset coefficients. `0xC200` MUST be marked volatile and pulsed explicitly **after** coefficients and `0xC202`.
   - **Sample-Rate Finding (PROVEN):** All four Dell biquads re-derive bit-exact (`0 LSB error`) from their `0xC380..0xC3A3` UI design fields using RBJ formulas at **`fs = 44.1 kHz`**, even though the ALC1316 PLL runs at **`48 kHz`** (`0xD103 = 0x9B`, `12.288 MHz = 256 fs`). Consequently, the shipped filters shift upward by `+8.84%` (`SPOL` woofer LPF `-3 dB` at `6164 Hz` instead of `5674 Hz`; `SPOR` tweeter HPF `-3 dB` at `838.4 Hz` instead of `770.2 Hz`).
   - **Clarification on Register Range:** The RT1316 BQ coefficient block is **`0xC203..0xC252`** (80 bytes across 4 biquads, plus `0xC00B`, `0xC380..0xC3A3`, `0xC261..0xC270`, `0xC200` = 134 writes = 402 bytes of `{addr_lo, addr_hi, val}` triples). The `0xD470..0xD497` range belongs to **`rt1320-sdw.c:479`**, NOT RT1316.

2. **Amp 2 IV-Sense Slot Collision & Clock Differences (`0x3004`/`0x3005`, `0xC007`/`0xC008`, PROVEN):**
   - Linux writes `0x3004 = 0xA0`, `0x3005 = 0xB1` on **BOTH** amplifiers (`rt1316-sdw.c:130-131`, live `amp{1,2}.registers.txt:5-6`).
   - Windows differentiates LL (Amp 1: `0x3004 = 0xA0`, `0x3005 = 0xB1`) from RR (Amp 2: **`0x3004 = 0xE4`, `0x3005 = 0xF5`**; `AggrRR/CODEC_Init.txt:243-244`) so all 4 feedback streams carry distinct 3-bit slot tags in aggregated capture.
   - Additional LL vs RR differences exist ONLY in `0xC0B1` (`0x1` LL vs `0x3` RR; Linux uses SDCA `0x41080100 = 0x01 / 0x05` instead) and `EndPoint002.txt` aggregation masks (Windows LL=`0xC`, RR=`0x3` — opposite to Linux IPC4 ALH link1=`0x3`, link2=`0xC`).
   - I/V sense clocks also differ: Windows writes `0xC007 = 0x22`, `0xC008 = 0x22` (`"256 fs, I-V=24KHz"`), `0x2F0A = 0x01`, and keeps `PDE24 (0x41001A08) = 0x00 (PS0)` during playback; Linux writes `0xC007/0xC008 = 0x11`, `0x2F0A = 0x00`, and leaves `PDE24 = 0x03 (PS3)` until IPC4 PCM 3 capture opens.

3. **Gain Staging (`0xC60F` + `0xD001..0xD004`) — MUST Remain Behind P4 250 ms Deadman Gate:**
   - Linux live: `0xC60F = 0x01` (chip reset; `~+6.6 dB` if linear), `0xD001:0xD002 = 0x03:0xBF`, `0xD003:0xD004 = 0x03:0xBF` (`-6.00 dB` under `diff/` inferred `0.09375 dB/step` scale).
   - Windows Basic (`SmartAmpModes/Basic_cmd.txt:6-8`): `0xC60F = 0x05` (`"+9 dB"`), `0xD002/0xD004 = 0xA9` (`-8.06 dB` under `0.09375 dB/step` -> net `~+0.34 dB` vs Linux). Windows Advance (`Advance_cmd.txt:6-8`): `0xC60F = 0x05`, `0xD002/0xD004 = 0xCA` (`-4.97 dB` -> net `~+3.43 dB`).
   - **Critical Safety Caveat (PROVEN conflict in vendor comments):** `Basic_cmd.txt:7` labels `0xA9` `"(30 L, 0dB=2.8Vrms)"` whereas `Advance_cmd.txt:7` labels `0xCA` `"(30 L, -6dB=2Vrms)"`. Read literally, higher code = quieter, meaning `0xA9` could be up to **`+4.0 dB` louder** than `0xBF`, and `0xC60F = 0x05` could add `+2.4` to `+6 dB` or alter the Class-D supply rail. Because Linux has no active `RtkIntU64.dll` thermal protection today, **`0xC60F = 0x05` and `0xD002/0xD004 = 0xA9` MUST ONLY be unlocked behind the P4 250 ms deadman thermal/IV-sense gate** (and Advance `0xCA` MUST NEVER be enabled).

4. **Waves `StradaleMLK` BPAM Binary Format Fully Cracked (`0` Leftover Bytes Across All 741 Blocks) — Including Previously Undecoded Volume-Adaptive `LINKLAWS` & Exact XTC Geometry (NEW PROVEN DISCOVERY):**
   - Exact TLV parsing of `StradaleMLK_ADL_MAPro11_DSP_{EFX,MFX,SFX}_Render.mps` recovers `dev_id` for every block, cleanly separating **`dev_id = 0` (`Internal Speaker`)** and `dev_id = 7` (`IS - secondary`) from Headphones, External Speakers, and TV (`dev_id = 7` in MFX).
   - **Polarity Resolution (INFERRED, very high confidence):** Across all three `.mps` files, `MASTERBP` uses `0 = active / 1 = bypassed`, whereas legacy module flags (`PEQ_BYPASS`, `MAXX_BASS_BYPASS`, `MAXX_TREBLE_BYPASS`, `MAXX_STEREO_BYPASS`, `MAXX_VOLUME_BYPASS`) use **`1 = ENABLED / 0 = DISABLED`** (confirmed by 5-way state inversion across `MFX Default` vs `MFX Master Off`, `EFX Speaker protection`, `TV Notification`, and `SFX NxS N360 On`).
   - **Newly Decoded `MFX[0]` 432-Byte `LINKLAWS` Blob (PROVEN):** Six volume-adaptive lookup curves driven by system volume (`0..100%`):
     - **`64 Hz` bass EQ (`BAND_2_GAIN`)**: stays at **`-0.2 dB`** from `0..75%` volume, then ducks linearly to **`-4.5 dB`** at `100%` volume (woofer excursion guard!).
     - **`MAXXBASS_COMP_INTENSITY`**: `47.0` (`0..45%`) -> `35.0` (`75%`) -> `14.0` (`100%`).
     - **`MAXXBASS_INTENSITY_RATIO`**: `1.61` (`0..45%`) -> `0.46` (`75..100%`).
     - **`MAXXBASS_OUTPUT_LP_RATIO_A`**: `2.02` (`0%`) -> `3.04` (`75%`) -> `2.02` (`100%`).
     - **`MAXXBASS_AMOUNT`**: `1.00` (`0..45%`) -> `0.88` (`75..100%`).
   - **Exact Waves `Nx Speakers` XTC Geometry (`SFX[100]`, PROVEN):** `XTC_SPEAKER_DISTANCE = 34 cm` (exact match to physical `S = 0.340 m`!), `XTC_SPEAKER_EAR_DISTANCE = 72 cm`, **`XTC_LF_SPLIT_FREQ = 351 Hz`, `XTC_LF_SPLIT_Q = 1.6`** (mono-anchored bass below 351 Hz!), `AMBISONICSV2_STEREO_ANGLE = 75°`, `N360_BASS_CUT = 80 Hz`, `N360_BASS_GAIN = +3.0 dB`.
   - **Asymmetric L/R Chassis Resonance Notches (`EFX[0] Speaker protection` + `MFX[0]`):** Both channels share severe narrow chassis/grille resonances at **`344–382 Hz` (`-7.5 / -8.4 dB`, Q 6.0)**, **`778–928 Hz` (`-4.9 to -7.6 dB`)**, **`1.0–1.4 kHz` (`-3.2 to -5.2 dB` — matches Blue Yeti `+4.2 dB` shoutiness peak!)**, **`1.73–1.88 kHz` (`-6.7 / -6.4 dB`)**, and **`3.11–3.14 kHz` (`-8.8 / -9.5 dB` — deepest cut in the entire stack!)**.

---

## 1. Biquad Crossover Activation (`0xC202` + `0xC200`) & 134-Write Coefficient Table

### 1.1 Exact Windows Runtime Open/Close Sequence vs. Live Linux Bug (PROVEN)

Source files under `/home/akshaybapat/project/xps17-speaker-stack/work/win/dec/codec_tree/Realtek/SDCA_06_Alc1316_Aggr{LL,RR}_SM_0AFE1028/`:

| Event | File:Line | Register / SDCA Control | Value | Vendor Comment | Live Linux Amp 1 (`amp1.registers.txt`) | Live Linux Amp 2 (`amp2.registers.txt`) |
|---|---|---|---|---|---|---|
| **Stream Open** | `EP001_Open/OpenCtl.txt:3` | **`0x0000C202`** | **`0xC0`** | `enable BQ1, BQ2` (bit 7=BQ1, bit 6=BQ2) | **`0x00`** (`L538`, chip read) | **`0x00`** (`L538`, chip read) |
| **Stream Open** | `EP001_Open/OpenCtl.txt:4` | **`0x0000C200`** | **`0xC0`** | `[7]=1, write "1" to send parameter update pluse` | `0xC0` (`L536`, Dell table) | **`0x40`** (`L536`, chip reset!) |
| Stream Open | `EP001_Open/OpenCtl.txt:6-9` | `PDE23/27/22/24` (`0x41001888..1A08`) | `0x00` (PS0) | Power up playback + **I/V sense (`PDE24`)** | `0x00/0x00/0x00/0x03` (`L2011-2014`) | `0x00/0x00/0x00/0x03` (`L2011-2014`) |
| Stream Open | `EP001_Open/OpenCtl.txt:11` | `XU24 Bypass` (`0x41001208`) | `0x00` | `XU24 not bypass` | `0x01` cache (`L2010`, BUG 2) | `0x01` cache (`L2010`, BUG 2) |
| **Stream Close** | `EP001_Open/CloseCtl.txt:3-6` | `PDE23/27/22/24` | `0x03` (PS3) | Power down playback + I/V sense | — | — |
| **Stream Close** | `EP001_Open/CloseCtl.txt:8` | **`0x0000C202`** | **`0x00`** | `disable BQ1, BQ2` | `0x00` | `0x00` |
| **Stream Close** | `EP001_Open/CloseCtl.txt:9` | **`0x0000C200`** | **`0xC0`** | `[7]=1, write "1" to send parameter update pluse` | — | — |

**Three Compounding Linux Kernel Driver Bugs (PROVEN):**
1. **BUG 3 (`0xC202` never written):** `dell_0b00_bq_params[]` (`sound_soc_sdw_utils_soc_sdw_rt_amp_coeff_tables.h:163-298`) omits `0xC202` completely (`contains_0xC202_enable = False`). Because `0xC202` is readable (`rt1316-sdw.c:150`, `0xc000 ... 0xc7b4`), non-volatile, has no `reg_default`, and is never written by Linux, both dumps (`0000c202: 00`) prove the hardware filters are bypassed on **both** amplifiers. Both sibling SKU `CODEC_Init_0BDA1028.txt` and `Aggr3Amp_SM/POST_Init_LL.txt` write `0xC202 = 0xC0` at init; RT1308's `dell_0a5d` table likewise includes its enable (`0xC540 = 0x0D`, `coeff_tables.h:95`).
2. **BUG 1 (Probe Race skips Amp 2):** `rt1316_sdw_component_probe()` (`rt1316-sdw.c:599-607`) only calls `rt1316_apply_bq_params()` if `rt1316->first_hw_init` is already true, and `rt1316_io_init()` (`rt1316-sdw.c:267-325`) never calls it. Live dump proof: Amp 1 matches `134/134` writes (`0xC203 = 0x00`, `0xC380 = 0x13`, `0xC200 = 0xC0`), whereas Amp 2 holds power-on identity coefficients (`0xC203/C217/C22B/C23F = 0x08 00 00 00`, `0xC380..C3A3 = 0x00`, `0xC200 = 0x40`). Already fixed in `module/patches/0001-bq-in-io-init.patch`.
3. **BUG 1b (`0xC200` `regcache_sync()` ordering hazard) + BUG 2 (`0x41001208` XU24 cache mismatch):**
   - `0xC200` is NOT in `rt1316_volatile_register()` (`rt1316-sdw.c:166-180`). On resume, `regcache_sync()` writes registers in ascending address order, replaying cached `0xC200 = 0xC0` **before** `0xC203..0xC252`! Marking `0xC200` volatile and pulsing it explicitly after coefficients + `0xC202` eliminates this race permanently.
   - `0x41001208` (`XU24 Bypass`) has `reg_default = 0x01` (`rt1316-sdw.c:64`) while `rt1316_blind_write[]` writes `0x00` (`rt1316-sdw.c:121`). Because it is unreadable (`rt1316-sdw.c:144-164`), ALSA restoring `1` matches `reg_default` and is silently skipped by `regcache_sync()` (`regcache.c:340-356`). Fixed in `module/patches/0002-xu24-truthful.patch` (`reg_default -> 0x00`).

### 1.2 Exact 134-Write (`402`-Byte) Coefficient Table Layout (`dell_0b00_bq_params[]`)

Format in `sound_soc_sdw_utils_soc_sdw_rt_amp_coeff_tables.h:163-298`: **134 register writes = 402 bytes** encoded as little-endian address triples `{reg_lo, reg_hi, data}` — verified **100% byte-identical** to both Windows `AggrLL/CODEC_Init.txt:101-234` and `AggrRR/CODEC_Init.txt:101-234`:

| Block | Register Range | Writes | Encoding & Function |
|---|---|---|---|
| **SPOL BQ1 (Woofer LPF)** | `0xC203..0xC216` | 20 | 5 × 4-byte big-endian signed **Q5.27** (`b0, b1, b2, a1, a2`; `1.0 = 0x08000000`) |
| **SPOL BQ2 (Subsonic HPF)** | `0xC217..0xC22A` | 20 | 5 × 4-byte BE signed Q5.27 (`b0, b1, b2, a1, a2`) |
| **SPOR BQ1 (Tweeter HPF)** | `0xC22B..0xC23E` | 20 | 5 × 4-byte BE signed Q5.27 (`b0, b1, b2, a1, a2`) |
| **SPOR BQ2 (Subsonic HPF)** | `0xC23F..0xC252` | 20 | 5 × 4-byte BE signed Q5.27 (`b0, b1, b2, a1, a2`) |
| **EQ Power Clock** | `0xC00B` | 1 | `0x30` (`enable EQ's Power Clock` + `pow_clk_silence_det`) |
| **BQ UI Metadata** | `0xC380..0xC3A3` | 36 | 4 × 9 bytes: `Fc(u16 BE)`, `BW(u16 BE)`, `Q(u16 BE)`, `Gain(s16 BE)`, `Type(u8)`. Write order: L-BQ1 (`0xC380..88`), L-BQ2 (`0xC392..9A`), R-BQ1 (`0xC389..91`), R-BQ2 (`0xC39B..A3`). Tuning-tool metadata only (not a limiter!). |
| **Pre/Post-Volume Gains** | `0xC261..0xC270` | 16 | Pre-L (`C261..64`), Pre-R (`C265..68`), Post-L (`C269..6C`), Post-R (`C26D..70`); all `0x08000000` (`1.0 = 0.0 dB`). |
| **Latch Update Pulse** | `0xC200` | 1 | `0xC0` (bit 7 = update pulse, bit 6 = reset default `1`). Status register `0xC201` reads `0x80` on both amps. |
| **TOTAL** | — | **134** | **402 bytes** (`0xC202` **MISSING**). *(Note: `0xD470..0xD497` is RT1320 `rt1320-sdw.c:479`, not RT1316.)* |

### 1.3 Decoded Biquad Topology, Bit-Exact Coefficients, and 44.1 kHz vs. 48 kHz Responses (PROVEN)

Difference equation: $y[n] = b_0 x[n] + b_1 x[n-1] + b_2 x[n-2] - a_1 y[n-1] - a_2 y[n-2]$.
All four filters match RBJ cookbook formulas at `fs = 44100 Hz` with **`max_lsb_error = 0` (`0 / 2^27`)** using `Q = Fc / BW` from `0xC380..0xC3A3` (`Type 1 = HPF`, `Type 2 = LPF`; `Gain = 0xFFEE = -18` is unused by HPF/LPF):

| Stage & Role | Coeff Range (UI Range) | UI Design (`Fc / BW -> Q`, Type) | Shipped Q5.27 Hex (`b0, b1, b2, a1, a2`) | Shipped Floats (`44.1 kHz` design running @ `48 kHz`) | Corrected `48 kHz` Q5.27 Hex (`b0, b1, b2, a1, a2`) |
|---|---|---|---|---|---|
| **SPOL BQ1** (Woofer LPF) | `0xC203..C216` (`0xC380..88`) | `5000 Hz / 6000 Hz -> Q=0.8333`, Type 2 (LPF) | `00b2e03a`, `0165c075`, `00b2e03a`, `f74d5be9`, `037e2501` | `0.087342`, `0.174683`, `0.087342`, `-1.087227`, `0.436594` | `009afe62`, `0135fcc3`, `009afe62`, `f6b3ce81`, `03b82b05` |
| **SPOL BQ2** (Subsonic HPF) | `0xC217..C22A` (`0xC392..9A`) | `20 Hz / 20 Hz -> Q=1.0000`, Type 1 (HPF) | `07fd1504`, `f005d5f7`, `07fd1504`, `f005d817`, `07fa2c29` | `0.998575`, `-1.997150`, `0.998575`, `-1.997146`, `0.997155` | `07fd51b6`, `f0055c95`, `07fd51b6`, `f0055e60`, `07faa537` |
| **SPOR BQ1** (Tweeter HPF) | `0xC22B..C23E` (`0xC389..91`) | `1000 Hz / 950 Hz -> Q=1.0526`, Type 1 (HPF) | `0774e033`, `f1163f9b`, `0774e033`, `f129b04d`, `06fd3118` | `0.932068`, `-1.864136`, `0.932068`, `-1.854644`, `0.873629` | `07803020`, `f0ff9fbf`, `07803020`, `f1101f3f`, `0710dfc1` |
| **SPOR BQ2** (Subsonic HPF) | `0xC23F..C252` (`0xC39B..A3`) | `20 Hz / 20 Hz -> Q=1.0000`, Type 1 (HPF) | Identical to SPOL BQ2 | Identical to SPOL BQ2 | Identical to SPOL BQ2 |

**Cascaded Channel Transfer Responses at `fs = 48 kHz` (Computed from Quantized Q5.27 Words):**

| Channel & Coefficient Set | `-3 dB` Corners | Max Passband Peak | DC (`0 Hz`) | `20 Hz` | `100 Hz` | `200 Hz` | `500 Hz` | `838 Hz` | `1000 Hz` | `1470 Hz` | `5000 Hz` | `6160 Hz` | `8000 Hz` | `12 kHz` | `20 kHz` | Nyquist (`24 kHz`) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **SPOL Woofer (Shipped 44.1k)** | `17.11 Hz` & `6164.0 Hz` | `+1.253 dB @ 30.8 Hz` (`+0.355 dB @ 2.97 kHz`) | `-60.73` | `-0.85` | `+0.20` | `+0.05` | `+0.03` | `+0.05` | `+0.07` | `+0.15` | `-0.89` | `-2.99` | `-7.36` | `-16.91` | `-40.03` | `-120.0` |
| **SPOR Tweeter (Shipped 44.1k)** | `838.4 Hz` | **`+1.557 dB @ 1466 Hz`** | `-180.73` | `-70.32` | `-41.26` | `-29.25` | `-12.63` | `-3.01` | `-0.43` | `+1.56` | `+0.21` | `+0.13` | `+0.07` | `+0.02` | `0.00` | `0.00` |
| **SPOL Woofer (Corrected 48k)** | `15.75 Hz` & `5674.1 Hz` | `+1.246 dB @ 28.3 Hz` (`+0.340 dB @ 2.97 kHz`) | `-59.27` | `-0.01` | `+0.17` | `+0.05` | `+0.03` | `+0.06` | `+0.09` | `+0.17` | `-1.58` | `-4.16` | `-8.89` | `-18.54` | `-41.63` | `-120.0` |
| **SPOR Tweeter (Corrected 48k)** | `770.2 Hz` | **`+1.557 dB @ 1349 Hz`** | `-179.27` | `-67.98` | `-39.81` | `-27.75` | `-11.02` | `-1.66` | `+0.45` | `+1.51` | `+0.18` | `+0.11` | `+0.06` | `+0.02` | `0.00` | `0.00` |

> **Critical Acoustic & Safety Consequence:** With `0xC202 = 0x00`, the tweeters currently lose **`29.25 dB` of attenuation at `200 Hz`** and **`12.63 dB` at `500 Hz`**, driving bass straight into the tweeter coils and diaphragms, while the woofers radiate uncontrolled breakup above `6.16 kHz`. Enabling `0xC202 = 0xC0` adds at most `+1.557 dB` local passband peaking (`1.47 kHz`) and is strictly safe as Step 0 before any loudness optimization.

---

## 2. Amp 2 IV-Sense Slot Non-Collision (`0x3004`/`0x3005`) & Complete LL vs. RR Diff

### 2.1 Exact LL (Amp 1) vs. RR (Amp 2) Differences in Windows Scripts (PROVEN)

Diffing `SDCA_06_Alc1316_AggrLL_SM_0AFE1028/` against `SDCA_06_Alc1316_AggrRR_SM_0AFE1028/` proves that **only three hardware items** differ between Amp 1 and Amp 2:

| Item | Register / Control | Linux Live Amp 1 (`sdw:0:1`) | Linux Live Amp 2 (`sdw:0:2`) | Windows LL (Amp 1) | Windows RR (Amp 2) | Evidence & Impact |
|---|---|---|---|---|---|---|
| **IV-Sense Left Tags** | **`0x3004`** | `0xA0` (`L5`, cache) | **`0xA0` (`L5`, COLLISION!)** | `0x82` early -> **`0xA0`** final (`L92, L243`) | `0xC6` early -> **`0xE4`** final (`L92, L243`) | Windows assigns Amp 2 distinct tags (`4/6` vs `0/2`) so aggregated capture frames do not collide. |
| **IV-Sense Right Tags** | **`0x3005`** | `0xB1` (`L6`, cache) | **`0xB1` (`L6`, COLLISION!)** | `0x93` early -> **`0xB1`** final (`L93, L244`) | `0xD7` early -> **`0xF5`** final (`L93, L244`) | Windows assigns Amp 2 distinct tags (`5/7` vs `1/3`). |
| **Channel Routing** | `0xC0B1` `[1:0]` vs `0x41080100` | `0xC0B1=0x00`, `UDMPU21=0x01` (L,L) | `0xC0B1=0x00`, `UDMPU21=0x05` (R,R) | `0xC0B1` RMW `OR 0x1` (L,L); `UDMPU21` untouched | `0xC0B1` RMW `OR 0x3` (R,R); `UDMPU21` untouched | `CODEC_Init.txt:237-241`. Both OSes achieve correct L,L / R,R routing via different muxes. |
| **DP2 Aggregation Mask** | `EndPoint002.txt:21,90` | IPC4 ALH link 1 = `0x3` (ch 0–1) | IPC4 ALH link 2 = `0xC` (ch 2–3) | `GroupPosition = 0xC`, `AggregationChannelMask = 0xC` | `GroupPosition = 0x3`, `AggregationChannelMask = 0x3` | Opposite channel ordering between Linux IPC4 ALH (`ipc4-topology.c:2329-2433`) and Windows (`EndPoint002.txt`). |

### 2.2 Remaining Sense-Path & Misc Register Differences (Both Amps)

| Register | Name | Linux Live (Both Amps) | Windows (Both Amps) | Evidence & Engineering Note |
|---|---|---|---|---|
| `0xC007` | I-V Sense Clock Divider | `0x11` (`rt1316-sdw.c:86,132`) | **`0x22`** (`CODEC_Init.txt:30,94`) | Vendor comment `"256 fs, PLL fout=12.288MHz (I-V=24KHz)"`. If IPC4 PCM 3 sense rate is wrong under `0x11`, parity target is `0x22`. |
| `0xC008` | I-V Sense Clock Divider | `0x11` (`rt1316-sdw.c:87,133`) | **`0x22`** (`CODEC_Init.txt:31,95`) | Companion divider to `0xC007`. |
| `0x2F0A` | SoundWire PHY Divider | `0x00` (`rt1316-sdw.c:90`) | **`0x01`** (`CODEC_Init.txt:54`) | `"Select v1.2 divider"`. |
| `0xC010` | DAC Volume Update Mode | `0xA5` (zero-cross + soft ramp) | `0x65` (zero-cross) | Linux soft ramp (`0xA5`) is **superior** for glitch-free P4 gain transitions (`rt1316-sdw.c:416-423`). Keep `0xA5`. |
| `0xC616` | Sense-ADC Bias Trim | `0x02` (`rt1316-sdw.c:99`) | Commented out in SM (`0x02` in NoSM) | Neighbours `0xC614/15/17` (`ADC Bias min power`). Off signal path; check if sense SNR changes. |
| `0x41001A08` | SDCA `PDE24` (I/V Sense Power) | `0x03` (PS3 off on IPC3; DAPM PS0 during PCM 3 capture) | `0x00` (PS0 on whenever `EP001` playback open) | Under IPC4, opening PCM 3 `"Amp feedback"` automatically powers `PDE24 -> PS0` via DAPM (`rt1316-sdw.c:374-397`). |
| `0xC174` | Silicon Revision (RO) | `0x02` / `0x02` (`L396`) | Read in PowerUp/PowerDown | Both chips are Rev `0x02` (not Rev `0x01`), so Windows' Rev-B `0xC60E` toggle branch never executes. |

> **Unresolved Nibble-Order Conflict (PROVEN contradiction — do NOT assume before capture):**
> - Windows comments read `"LV=2, LI=0"` for `0x3004 = 0xA0` (`0b1010_0000`) and `"LV=6, LI=4"` for `0xE4` (`0b1110_0100`), implying bits `[6:4] = V`, bits `[2:0] = I` (with bit 7 = enable).
> - Realtek-authored Linux `rt1316-sdw.c:435-438` defines `SOC_SINGLE("Left V Tag Select", 0x3004, 0, 7, 0)` (bits `[2:0] = V`) and `SOC_SINGLE("Left I Tag Select", 0x3004, 4, 7, 0)` (bits `[6:4] = I`) — **exact opposite nibble order**.
> - Therefore, whether the 4 IPC4 `"Amp feedback"` slots carry `(I, I)` or `(V, I)` interleaved at 24 kHz inside 48 kHz 24-in-32 frames is **UNKNOWN** until the first live faint-tone capture on IPC4 PCM 3.

---

## 3. Gain Staging (`0xC60F` / `0xD001..0xD004`) & P4 Deadman Gate Requirement

| Stage / Control | Register(s) | Stock Linux Live | Windows Basic (`Basic_cmd.txt:6-8`) | Windows Advance (`Advance_cmd.txt:6-8`) | `diff/` Inferred Delta vs. Linux | Conservative Safety Bound (`module/` & `sense/`) |
|---|---|---|---|---|---|---|
| **Class-D HV Stage Gain** | `0xC60F` | `0x01` (`L1575`, reset) | **`0x05`** (`"HV Gain=+9dB"`) | **`0x05`** (`"HV Gain=+9dB"`) | `+2.4 dB` (if linear `0.6 dB/code` from `0x00=+6 dB` to `0x05=+9 dB`) | `+3.0` to `+6.0 dB` (step linearity & boost rail voltage **UNKNOWN**) |
| **Digital Gain L / R** | `0xD001:0xD002` & `0xD003:0xD004` | `0x03:0xBF` (`0x3BF`) | **`0x03:0xA9`** (`0x3A9`) | **`0x03:0xCA`** (`0x3CA`) | `0x3BF = -6.00 dB`, `0x3A9 = -8.0625 dB` (**`-2.06 dB`**), `0x3CA = -4.9688 dB` (**`+1.03 dB`**) at `0.09375 dB/step` | **Direction UNPROVEN!** `Basic_cmd.txt:7` says `0xA9` `"0dB=2.8Vrms"` while `Advance_cmd.txt:7` says `0xCA` `"-6dB=2Vrms"` -> read literally, `0xA9` could be **up to `+4.0 dB` louder** than `0xBF`! |
| **Net Amp Stage Delta** | Combined | `0.0 dB` ref | `0xC60F=0x05`, `D002/4=0xA9` | `0xC60F=0x05`, `D002/4=0xCA` | Basic: **`+0.34 dB`** (`±0.5 dB`); Advance: **`+3.43 dB`** | **Basic: up to `+3` to `+9 dB`** until faint tone + I-sense measurement settles both scales! |
| **SDCA FU21 Volume** | `0x41000191/192` | Not exposed (reset UNKNOWN) | `-63..0 dB`, `0.75 dB` step, default **`-6.0 dB`** (`IntcSDW.inf:345-357`) | Same | — | Windows defaults endpoint volume to `-6 dB`; Linux relies on PipeWire/SOF `0 dB` max. |

### Why `0xC60F = 0x05` / `0xD002 = 0xA9` MUST Stay Behind the P4 250 ms Deadman Gate
1. **Unverified Electrical Gain & Supply Rail:** Neither the step law of `0xC60F` (and whether bitfields change Class-D boost rail voltage) nor the sign of `0xD002/0xD004` has been measured acoustically or via I-sense on this machine.
2. **Windows Thermal Guard Missing on Linux:** On Windows, `0AFF1028` runs `RtkIntU64.dll` (`RTSPKPT` thermal/excursion protection) continuously over DP2 I/V sense (`Basic` full scale `2.8 Vrms` into `4 Ω` = `1.96 W ≈ 2.0 W` rated average limit). Linux currently has zero thermal protection under IPC3.
3. **P4 Interlock Contract (`sense/design/DESIGN.md:243-260`, modeled on Asahi `macaudio.c:58-60,271-321`):**
   - Module defaults strictly to stock (`0xC60F = 0x01`, `0xD002/0xD004 = 0xBF`).
   - Elevated `Basic` profile (`0xC60F = 0x05`, `0xD002/0xD004 = 0xA9`) may ONLY be engaged while: (a) userspace speaker guard holding PCM 3 `"Amp feedback"` open writes magic heartbeat every `50 ms` (kernel delayed-work watchdog locks back to stock in **`≤ 250 ms`** on timeout/crash/suspend/DP2 stop), (b) hardware crossover readback proves `0xC202 == 0xC0` and non-identity coefficients on BOTH amps, and (c) IPC4 UCM DRC (`Post Mixer Speaker Playback DRC switch`, `+13.7 dB` makeup!) is fenced OFF.
   - Advance mode (`0xCA`) is **permanently forbidden**.

---

## 4. Forbidden Protection, Reset & Test Registers (NEVER WRITE)

Verified from `module/src/rt1316-lab-policy.h:76-78`, `diff/registers.md`, `alc1316_LL_0AFE1028_CODEC_Init.csv`, and live register dumps:

| Register | Name | Windows Value | Linux `blind_write` | Live Amp 1 (`amp1.registers.txt`) | Live Amp 2 (`amp2.registers.txt`) | Volatile? | Safety Classification & Rationale |
|---|---|---|---|---|---|---|---|
| **`0xC000`** | Software reset | `0x01` (`Init:15`) | `0x02` (`io_init:289`) | `0x00` (`L24`) | `0x00` (`L24`) | **Yes** | Resets chip domains; Linux & Windows use different reset bits (`0x02` vs `0x01`); owned exclusively by `rt1316_io_init()`. |
| **`0xC050`** | DC detection enable | `0x83` (`Init:79`) | `0x83` (`L116`) | `0x83` (`L104`) | `0x83` (`L104`) | No | Hardware DC-offset speaker protection. Clearing risks instant thermal coil destruction on DC fault. |
| **`0xC082`** | Sine generator control | `0x39` (`Init:248`) | Not written | `0x3B` (`L154`) | `0x3B` (`L154`) | No | Internal test-tone generator (`"sin gen auto clear enable"`). Accidental write can inject full-scale sine into drivers. |
| **`0xC093`** | Silence detection control | `0x80` init (`0xE0`/`0x60` runtime) | `0x80` (`L112`) | `0x87` (`L171`) | `0x87` (`L171`) | **Yes** | Silence-detect auto-mute & live status bits (`0x07`). |
| **`0xC09C`** | Software PDB / `K_R0` cal | `0x7B` (`K_R0` toggles `0x73`) | `0x7B` (`L106`) | `0x7B` (`L180`) | `0x7B` (`L180`) | No | Power-down / `K_R0` calibration mode; unsafe outside controlled calibration state machine. |
| **`0xC09D`** | Silence det & S3 hold | `0x80` (`Init:76`) | `0x80` (`L113`) | `0x87` (`L181`) | `0x87` (`L181`) | **Yes** | `"Enable silence detection, Disable hold S3"`; live status bits in low nibble. |
| **`0xC0A3`** | SoundWire clock detect | `0x71` (`Init:73`) | `0x71` (`L110`) | `0x71` (`L187`) | `0x71` (`L187`) | **Yes** | SoundWire clock-loss detector; disabling risks uncontrolled Class-D switching on bus clock stop. |
| **`0xC102`** | HV mute behaviour | `0x00` (`Init:67`) | `0x00` (`L103`) | `0x00` (`L282`) | `0x00` (`L282`) | No | `"[3]=0 HV_Mute=1 when Mute (Add for B版)"`. Enforces HV stage mute when muted. |
| **`0xC60E`** | `HV_POW_SPK_PRO` enable | `0x05` (`Init:66`) | `0x05` (`L102`) | `0x05` (`L1574`) | `0x05` (`L1574`) | No | Bit 0 enables high-voltage output-stage speaker power protection. Already active (`0x05`) on both amps; NEVER modify. |

---

## 5. Dell `RTSPKPT` Speaker-Protection Blob Analysis (`RTAIODAT` Entry 13)

Source: `/home/akshaybapat/project/xps17-speaker-stack/work/win/tables/rtspkpt_DELL_0AFE1028_float32_nonzero.csv` (`242` nonzero 32-bit words inside the `34,492`-byte `RTSPKPT` payload, consumed by Realtek endpoint APO `RtkIntU64.dll` which contains C++ classes `DCResistance`, `Excursion`, `Temperature`, `GainTemperature`, `SmartVolume`, `ADFImpedance` — `sense/protection-model/NOTES.md:36-38`).

### 5.1 Structural Proof (PROVEN)
- **3 byte-identical populated sections** at bases `0x0030`, `0x21D4`, `0x4378` (stride **`0x21A4` = 8,612 bytes**, `0` diffs across `0x00..0x158`), followed by a zeroed 4th slot and trailing build-date stamp **`20160414`** (`2016-04-14`) at `0x86B8`.
- Each section tail (`base + 0x2180`) contains `0.001, 0.001` followed by three **`0xCDCDCDCD`** words (MSVC debug-heap uninitialized fill — proving this blob is a raw C struct dump from Realtek's tuning tool).
- Inside each section sit **two `0xAC`-byte (`172`-byte) channel records**: **`RecA`** (`base + 0x00`, `38` nonzero words — woofer candidate) and **`RecB`** (`base + 0xAC`, `36` nonzero words — tweeter candidate).

### 5.2 Complete Parsed `RecA` vs. `RecB` Field Table & Physical Candidates

**Per-Driver Differences (ONLY 6 offsets differ between `RecA` and `RecB`):**

| Rel Offset | Block 0 Abs (`RecA` / `RecB`) | `RecA` Value | `RecB` Value | Ratio (`A/B`) | Status & Engineering Interpretation |
|---|---|---|---|---|---|
| `+0x18` | `0x0048` / `0x00F4` | **`0.00096`** (`f32`) | **`0.00039`** (`f32`) | `2.46×` | **UNKNOWN** — Strong per-driver physical asymmetry (e.g. woofer vs tweeter excursion `Xmax` in meters `0.96 mm` vs `0.39 mm`, compliance, or thermal/sense scale; unproven without `RtkIntU64.dll` disassembly). |
| `+0x24` | `0x0054` / `0x0100` | **`0.00016`** (`f32`) | **`0.00008`** (`f32`) | `2.00×` | **UNKNOWN** — Per-driver small constant (exact 2:1 ratio). |
| `+0x30` | `0x0060` / `0x010C` | **`0.39`** (`f32`) | **`0.10`** (`f32`) | `3.90×` | **UNKNOWN** — Per-driver dimensionless gain/damping/limit constant. |
| `+0x78` | `0x00A8` / `0x0154` | **`0.003`** (`f32`) | **`0.00328`** (`f32`) | `0.915×` | **INFERRED (low)** — Near copper temperature coefficient `α ≈ 0.00393 /K` (compare Asahi `0.0035–0.0038 /K`), or voice-coil inductance/step constant. |
| `+0xA0`, `+0xA4` | `0x00D0..D4` / `0x017C..80` | `u32(1)`, `u32(1)` | `0`, `0` | — | **UNKNOWN** — Feature enable flags set only on `RecA` (e.g. excursion guard enabled on woofer only!). |

**Shared Physical & Thermal Candidates (Identical in `RecA` and `RecB`):**

| Rel Offset(s) | Raw Value | Status | Candidate Meaning for Guard & Lead 2 Klippel Model |
|---|---|---|---|
| `+0x00`, `+0x04` | `u32 = 13163`, `u32 = 4` | INFERRED (low) | Codec/variant ID (`ALC1316` rev 3) & `4` speakers. |
| `+0x0C, +0x10, +0x14` | `100.0, 100.0, 100.0` | INFERRED (low) | Thermal ceiling candidate **`T_max = 100 °C`** (conservative vs Apple `130–140 °C`). |
| `+0x34` | **`4.0`** | INFERRED (low) | Nominal DC resistance **`Re0 = 4.0 Ω`** — cross-checks against Windows Basic `2.8 Vrms`: `2.8² / 4 = 1.96 W ≈ 2.0 W` Dell driver rating! |
| `+0x68` | **`2.0`** | INFERRED (low) | Matches `2.0 W` rated average power OR `2.0 s` fast voice-coil thermal time constant `τ_coil`. |
| `+0x80..+0x90` | **`25.0, 100.0, 80.0, 70.0, 60.0`** | INFERRED (low) | Ambient reference **`T0 = 25 °C`**, ceiling **`100 °C`**, and staged derating knees **`80 / 70 / 60 °C`**. |
| `+0x94..+0x9C` | `170.0, 340.0, 340.0` | UNKNOWN | Scaling/ceiling constants (`340 = 2 × 170`). |
| `+0x1C, +0x28, +0x38, +0x44, +0x48, +0x50, +0x54, +0x60` | `0.00025`, `0.375`, `0.0625`, `u32(900)`, `500.0`, `24.6`, `20.4`, `300.0` | UNKNOWN | Additional unlabelled filter/thermal/excursion struct fields (`24.6 + 20.4 = 45.0`; `900`, `500`, `300` plausible Hz or ms constants). |

> **Engineering Rule for Lead 1 & Lead 2:** Because `RTSPKPT` carries no field names and Waves' `SMARTAMP_ONOFF` / `IVOLUME_THERMAL_ON_OFF` / `IVOLUME_EXCURSION_ON_OFF` are all `0` (OFF with generic placeholders `F0=200 Hz, Qm=1.4, Re=1.0 Ω, R0=1.5 Ω, TC=2/40/200 s, Tmax=70 °C`), **never treat `RTSPKPT` floats as calibrated constants**. Use `T_limit ≤ 100 °C` as a safe conservative ceiling until live IPC4 I-sense heating fits (`Re0`, `Rth`, `τ`) and impedance-sweep `f0/Qm` identification are measured.

---

## 6. Waves `StradaleMLK` (`EFX` / `MFX` / `SFX`) Acoustic, Bass, Dynamics & Spatial Extraction

### 6.1 Exact BPAM Binary Decoding & Render Pipeline Architecture (PROVEN)

Our binary BPAM parser (`analyze_windows_tables.py`) decoded **100% of all three `.mps` files with `0` trailing bytes** and cross-checked them against the raw record CSVs (`EFX`: `3,548` rows / `40` blocks; `MFX`: `42,465` rows / `181` blocks; `SFX`: `92,014` rows / `520` blocks) and `waves_param_ids.csv` (`1,317` DLL-extracted `MA_DATA::PARAM_*` names).

The Windows render APO pipeline for `dev_id = 0` (**`Internal Speaker`**) runs three sequential Waves stages (followed by `RtkIntU64.dll` hardware protection):

```
App Stream ──► SFX (NxS Active / Nx Speakers On / N360 / UM228)
           ──► Windows Mixer
           ──► MFX (Default [0] + 6 volume-adaptive LINKLAWS, OR Master Off [1], OR NX MFX Subtype [163])
           ──► EFX (Speaker protection [0]: Asymmetric L/R 10-band chassis correction EQ + -1.0 dB trim)
           ──► Realtek RtkIntU64.dll (RTSPKPT) ──► ALC1316 x2 (HW Crossover 0xC202=0xC0)
```

**Flag Polarity Proof (INFERRED, very high confidence):**
- `MASTERBP`: `0 = active`, `1 = master bypassed` (e.g. `SFX Nx Off`: `MASTERBP = 1`; `EFX Headphones`: `MASTERBP = 1`).
- Legacy module flags (`PEQ_BYPASS`, `MAXX_BASS_BYPASS`, `MAXX_TREBLE_BYPASS`, `MAXX_STEREO_BYPASS`, `MAXX_VOLUME_BYPASS`): **`1 = ENABLED (active)`, `0 = DISABLED (bypassed)`** — proved by 5 independent block comparisons (`MFX[0] Default` has all five `= 1` alongside `MVBAND_ACTIVE = 1`, `LEVELLER_ACTIVE = 1`, `VOLUME_COMPRESSOR_ACTIVE = 1`, `CENTER_ACTIVE = 1`, `EXCITER_ACTIVE = 1`; `MFX[1] Master Off` flips ALL of them to `0` simultaneously; `EFX[0]` sets ONLY `PEQ_BYPASS = 1` & `MAXX_VOLUME_BYPASS = 1` while leaving untuned MaxxBass/Treble/Stereo `= 0`).

### 6.2 `EFX[0] Speaker protection` (`0x126`) — Asymmetric L/R Chassis Correction EQ (PROVEN)

Active endpoint correction right before hardware (`MASTERBP=0`, `PEQ_BYPASS=1`, `MAXX_VOLUME_BYPASS=1`, `VOLUMN_TRIM_OUTPUT = -1.0 dB`, `MVBAND_ACTIVE=0`):

| Band | **Left Channel (`PEQ L`)** (`State`, `Type`, `Freq`, `Gain`, `Q`) | **Right Channel (`PEQ R`)** (`State`, `Type`, `Freq`, `Gain`, `Q`) | Acoustic Role |
|---|---|---|---|
| **B1** | ON, High-Shelf (`T2`), **`16 Hz`**, **`+2.7 dB`**, `Q=1.414` | ON, Peak (`T0`), **`224 Hz`**, **`-4.1 dB`**, `Q=6.00` | L broadband `+2.7 dB` make-up/balance shelf; R enclosure cavity notch |
| **B2** | OFF (`state=0`), Peak, `62 Hz`, `-10.7 dB`, `Q=1.70` | ON, Peak (`T0`), **`290 Hz`**, **`-6.9 dB`**, `Q=6.00` | R low-mid cavity resonance |
| **B3** | ON, Peak (`T0`), **`344 Hz`**, **`-7.5 dB`**, `Q=5.98` | ON, Peak (`T0`), **`349 Hz`**, **`-8.4 dB`**, `Q=5.98` | **Primary shared chassis resonance (`~346 Hz`, `-7.5 / -8.4 dB`!)** |
| **B4** | ON, Peak (`T0`), **`449 Hz`**, **`-3.9 dB`**, `Q=3.63` | ON, Peak (`T0`), **`618 Hz`**, **`-6.4 dB`**, `Q=3.00` | Lower-mid boxiness |
| **B5** | ON, Peak (`T0`), **`778 Hz`**, **`-4.9 dB`**, `Q=3.00` | ON, Peak (`T0`), **`801 Hz`**, **`-5.0 dB`**, `Q=3.00` | **Tweeter crossover transition zone (`~790 Hz`)** |
| **B6** | ON, Peak (`T0`), **`1272 Hz`**, **`-2.6 dB`**, `Q=2.09` | ON, Peak (`T0`), **`1017 Hz`**, **`-3.2 dB`**, `Q=2.09` | Lower presence / shoutiness |
| **B7** | ON, Peak (`T0`), **`1734 Hz`**, **`-6.7 dB`**, `Q=3.00` | ON, Peak (`T0`), **`1878 Hz`**, **`-6.4 dB`**, `Q=3.00` | **Upper-mid glare peak (`~1.8 kHz`)** |
| **B8** | ON, Peak (`T0`), **`3135 Hz`**, **`-8.8 dB`**, `Q=3.00` | ON, Peak (`T0`), **`3112 Hz`**, **`-9.5 dB`**, `Q=3.00` | **Deepest hardware notch (`~3.12 kHz`, tweeter/grille peak!)** |
| **B9** | ON, High-Shelf (`T2`), **`17247 Hz`**, **`-4.9 dB`**, `Q=0.707` | ON, Peak (`T0`), **`1395 Hz`**, **`-5.2 dB`**, `Q=5.98` | L ultrasonic roll-off; R `1.4 kHz` shoutiness notch |
| **B10** | ON, Peak (`T0`), **`928 Hz`**, **`-4.8 dB`**, `Q=6.00` | ON, Peak (`T0`), **`915 Hz`**, **`-7.6 dB`**, `Q=6.00` | **Shared narrow `920 Hz` breakup notch (Q 6.0)** |

*(Note: `EFX[25] IS - secondary` uses virtually identical narrow cuts on both L and R with `MAXX_VOLUME_SLIDER = 4.6`.)*

### 6.3 `MFX[0] Default` (`0x11A`) & `MFX[163] NX MFX Subtype` (`0x6B011`) — Full Voicing, MaxxBass, Multiband & Volume-Adaptive `LINKLAWS` (PROVEN)

#### A. Static Parametric EQ (`PEQ`, both L & R identical)

| Band | `MFX[0] Default` (Normal Speakers) | `MFX[163] NX MFX Subtype` (When Nx Active; `TRIM = -1.1 dB`) | `MFX[1] Master Off` (Stored Fallback Curve) |
|---|---|---|---|
| **B1** | ON, Low-Shelf (`T1`), **`30 Hz`**, `-5.9 dB`, `Q=0.707` | ON, Low-Shelf (`T1`), **`39 Hz`**, `-5.9 dB`, `Q=0.707` | ON, HPF (`T4`), **`150 Hz`**, `0 dB`, `Q=0.40` |
| **B2** | ON, Peak, **`64 Hz`**, **`-4.5 dB`** *(LINKLAW: `-0.2 dB` ≤75% vol!)*, `Q=3.0` | ON, Peak, **`72 Hz`**, `-4.5 dB`, `Q=3.0` | ON, Peak, `348 Hz`, `-5.0 dB`, `Q=4.80` |
| **B3** | ON, Peak, **`102 Hz`**, **`+1.6 dB`**, `Q=3.0` | ON, Peak, **`102 Hz`**, **`+1.6 dB`**, `Q=3.0` | ON, Peak, `704 Hz`, `-5.5 dB`, `Q=4.46` |
| **B4** | OFF, `313 Hz`, `-6.9 dB`, `Q=3.0` | ON, Peak, **`313 Hz`**, **`-8.0 dB`**, `Q=3.0` | ON, Peak, `516 Hz`, `-4.5 dB`, `Q=3.18` |
| **B5** | ON, Peak, **`1397 Hz`**, **`-4.0 dB`**, `Q=3.0` | ON, Peak, **`1397 Hz`**, **`-5.0 dB`**, `Q=3.0` | ON, Peak, `1032 Hz`, `-6.0 dB`, `Q=4.55` |
| **B6** | OFF, `669 Hz`, `-4.5 dB`, `Q=3.0` | ON, Peak, **`669 Hz`**, **`-7.0 dB`**, `Q=3.0` | ON, Peak, `836 Hz`, `-7.5 dB`, `Q=4.20` |
| **B7** | ON, Peak, **`2715 Hz`**, **`-3.2 dB`**, `Q=6.0` | ON, Peak, **`2715 Hz`**, **`-3.2 dB`**, `Q=6.0` | ON, Peak, `3813 Hz`, `-7.5 dB`, `Q=4.20` |
| **B8** | ON, Peak, **`13206 Hz`**, **`-7.1 dB`**, `Q=4.95` | ON, Peak, **`13206 Hz`**, **`-7.1 dB`**, `Q=4.95` | ON, Peak, `6212 Hz`, `-8.0 dB`, `Q=3.57` |
| **B9** | ON, Peak, **`382 Hz`**, **`-6.5 dB`**, `Q=4.66` | ON, Peak, **`382 Hz`**, **`-6.5 dB`**, `Q=4.66` | ON, Peak, `2577 Hz`, `-6.5 dB`, `Q=3.86` |
| **B10** | OFF, `3062 Hz`, `-4.9 dB`, `Q=1.1` | ON, Peak, **`4029 Hz`**, **`-6.8 dB`**, `Q=1.1` | ON, Peak, `3247 Hz`, `-6.0 dB`, `Q=3.90` |

*(Plus `VARISLOPE_XOV_ACTIVE = 1` in `MFX[0]` and `MFX[163]`: `BAND0_ON = 1`, **`40 Hz`**, Type 2, Slope 3 — sub-40 Hz steep protective roll-off!)*

#### B. Psychoacoustic Bass (`MaxxBass`), Treble (`MaxxTreble` + `Exciter`), Stereo & Dynamics (`MFX[0]` vs `MFX[163]`)

| Subsystem | `MFX[0] Default` | `MFX[163] NX MFX Subtype` | `MFX[1] Master Off` (stored) | Engineering Meaning for Lead 2 & Lead 3 |
|---|---|---|---|---|
| **MaxxBass** | **`103.2 Hz`**, Intensity `21`, Amount `0.88`, `INPUT_HP_RATIO = 0.44` (`45.4 Hz`), `CUTOFF_RATIO = 0.34` (`35.1 Hz`), `INTENSITY_RATIO = 0.46`, `OUTPUT_LP_RATIO_A = 2.02` (`208.5 Hz`), `LP_RATIO_B = 0.36`, `COMP_INTENSITY = 14`, `WIDEBAND = 1` | **`88.0 Hz`**, Intensity `15`, Amount `0.88`, `INPUT_HP_RATIO = 0.44` (`38.7 Hz`), `CUTOFF_RATIO = 0.70` (`61.6 Hz`), `OUTPUT_LP_RATIO_A = 1.76` (`154.9 Hz`), `COMP_INTENSITY = 14` | `231.1 Hz`, Intensity `27`, Amount `0.85`, `HP_RATIO = 0.40` (`92.4 Hz`), `CUTOFF_RATIO = 0.55` (`127.1 Hz`), `LP_A = 1.3`, `LP_B = 1.5`, `ORIGINAL_BASS = 1` | Waves synthesizes bass harmonics cleanly starting around **`88–103 Hz`** with harmonics band-limited up to **`~208–314 Hz`** — **NOT** destroying `< 180 Hz` with a 48 dB/oct brickwall! |
| **MaxxTreble & Exciter** | `MaxxTreble = 2625.5 Hz`, Intensity `18`; **`EXCITER_ACTIVE = 1`, `EXCITER_WET_GAIN = -24.4 dB`** | `MaxxTreble = 1572.5 Hz`, Intensity `21`; `EXCITER_ACTIVE = 1`, `-24.4 dB` | `MaxxTreble = 8787.5 Hz`, Intensity `22`; `EXCITER_ACTIVE = 0` | Subtle harmonic sparkle (`-24.4 dB` wet) restores air without sharp high-Q treble peaking. |
| **Stereo & Center** | `MAXX_STEREO_INTENSITY = 14`, `STEREO_SPAN = 1`, **`CENTER_ACTIVE = 1`, `CENTER_GAIN_C = 50`, `CENTER_GAIN_LR = 65`** | Same (`14`, `CENTER 50/65`) | `MAXX_STEREO_INTENSITY = 23`, `CENTER_ACTIVE = 0` | Mild width + explicit Mid/Side center-anchoring (`C=50`, `LR=65`) preserves vocal solidity! |
| **Dynamics / Leveler** | `VOLUME_COMPRESSOR_ACTIVE = 1`: **`4.08:1` @ `-10.5 dB`**, atk `88 ms`, rel `800 ms`, makeup `+3.0 dB`, hard knee; `LEVELLER_ACTIVE = 1`: **`-8 dB`**, makeup **`+4.0 dB`**, atk `291 ms`, rel `2511 ms`; `VOLUME_LOW_GAIN = +7 dB`, `MAXX_VOLUME_SLIDER = 3`, `SV_COMPENSATION_LIMITER_ENABLE = 1` | Same except `COMPRESSOR_RELEASE = 1510 ms`, `VOLUMN_TRIM_OUTPUT = -1.1 dB` | `VOLUME_COMPRESSOR_ACTIVE = 0`, `LEVELLER_ACTIVE = 0` | Slow, musical leveling (`88/800 ms` comp + `291/2511 ms` leveler + `+7 dB` low-level gain) instead of a fast `20 ms` pumping limiter! |

#### C. Newly Decoded `MFX[0]` Volume-Adaptive `LINKLAWS` (`432`-Byte Binary Blob, PROVEN)

`LINKLAWS_ACTIVE = 1` maps `MASTER_PARAM_1` (Windows system volume `0..1000` = `0..100%`) linearly onto 6 bass/excursion parameters:

| LinkLaw # | Target Parameter (`ID`) | `0%` Vol | `45%` Vol | `75%` Vol | `100%` Vol | Physical / Psychoacoustic Role |
|---|---|---|---|---|---|---|
| **0** | **`BAND_2_GAIN` (`52`, `64 Hz` EQ)** | **`-0.20 dB`** | `-0.20 dB` | **`-0.20 dB`** | **`-4.50 dB`** | Full `64 Hz` physical bass up to 75% vol; smooth `-4.3 dB` excursion cut toward 100% vol! |
| **1** | **`MAXXBASS_COMP_INTENSITY` (`157`)** | **`47.0`** | **`47.0`** | **`35.0`** | **`14.0`** | Strong psychoacoustic bass compression/presence at quiet desk volume; backs off smoothly at high SPL. |
| **2** | **`MAXXBASS_INTENSITY_RATIO` (`159`)** | **`1.61`** | **`1.61`** | **`0.46`** | **`0.46`** | `3.5×` stronger upper-harmonic bass synthesis below 45% volume (ISO 226 equal-loudness bass compensation!). |
| **3** | **`MAXXBASS_OUTPUT_LP_RATIO_A` (`160`)** | **`2.02`** (`208 Hz`) | `2.63` (`272 Hz`) | **`3.04`** (`314 Hz`) | **`2.02`** (`208 Hz`) | Widens upper harmonic bandwidth around 75% volume where physical bass begins ducking, tightens at 100%. |
| **4** | **`MAXXBASS_OUTPUT_LP_RATIO_B` (`161`)** | `0.36` | `0.36` | `0.36` | `0.36` | Fixed lower harmonic roll-off ratio. |
| **5** | **`MAXXBASS_AMOUNT` (`163`)** | **`1.00`** | **`1.00`** | **`0.88`** | **`0.88`** | Full MaxxBass blend at quiet/mid volume; slight `-12%` backoff at high volume. |

#### D. Multiband Dynamic EQ / Compression (`MVBAND`, `MVBAND_ACTIVE = 1` in `MFX[0]` & `MFX[163]`)

| Band | `MFX[0] Default` (`Freq`, `Q`, `Makeup`, `Thr`, `Ratio`, `Atk/Rel`) | `MFX[163] NX MFX Subtype` | Purpose |
|---|---|---|---|
| **MV1** | ON: `746 Hz`, `Q=1.65`, `0 dB`, `-12.5 dB`, **`20.0:1`**, `1 / 40 ms` | ON: `746 Hz`, `Q=1.65`, `-18.5 dB`, `20.0:1`, `1 / 40 ms` | Fast dynamic ceiling at tweeter crossover lower knee (`746 Hz`) |
| **MV2** | ON: **`93 Hz`**, `Q=2.35`, **`-5.4 dB`**, **`-22.5 dB`**, **`14.84:1`**, `1 / 202 ms` | ON: **`82 Hz`**, `Q=2.35`, `0 dB`, **`-34.0 dB`**, `7.92:1`, `1 / 202 ms` | **Primary woofer excursion dynamic limiter at `82–93 Hz` (`1 ms` attack!)** |
| **MV3** | ON: **`217 Hz`**, `Q=1.65`, `0 dB`, `-10.0 dB`, `5.54:1`, `4 / 49 ms` | ON: `217 Hz`, `Q=1.65`, `-19.5 dB`, `5.54:1`, `4 / 49 ms` | Upper-bass / chest-warmth dynamic control |
| **MV4** | ON: **`356 Hz`**, `Q=0.35`, `0 dB`, `-4.0 dB`, `14.70:1`, `4 / 13 ms` | ON: `3436 Hz`, `Q=0.35`, `-3.5 dB`, `14.70:1`, `4 / 13 ms` | Chassis resonance / presence transient catch |
| **MV5** | ON: **`531 Hz`**, `Q=1.65`, `0 dB`, `-8.0 dB`, `3.16:1`, `4 / 507 ms` | ON: `531 Hz`, `Q=1.65`, `-9.5 dB`, `10.99:1`, `1 / 1072 ms` | Midrange loudness smoothing |
| **MV6** | OFF (`2048 Hz`) | ON: `2048 Hz`, `Q=1.65`, `-11.5 dB`, `4.69:1`, `4 / 49 ms` | Presence glare control under Nx |
| **MV7** | ON: **`3972 Hz`**, `Q=0.72`, `0 dB`, `-9.0 dB`, `16.66:1`, `4 / 372 ms` | ON: `91 Hz`, `Q=5.47`, `+1.0 dB`, `-1.0 dB`, `16.66:1`, `4 / 49 ms` | High-mid sibilance / tweeter peak protection |
| **MV8** | ON: **`15682 Hz`**, `Q=1.65`, `0 dB`, `-22.5 dB`, `12.29:1`, `4 / 49 ms` | ON: `8192 Hz`, `Q=1.65`, `-13.0 dB`, `12.29:1`, `4 / 49 ms` | Tweeter HF thermal/peak guard |

### 6.4 `SFX` Spatial & Crosstalk Cancellation Geometry (`Nx Speakers`, PROVEN)

From `SFX[4] NxS Active [Default]` (`0x8EB5`), `SFX[100] Nx Speakers On` (`0xC15B6`), and `SFX[105] NxS N360 On` (`0xC1BA1`):

| Parameter | Exact Value | Significance for Lead 2 & Lead 3 Spatial Engine |
|---|---|---|
| `XTC_ACTIVE` | `1` (in `SFX[100]`/`[105]`) | Crosstalk cancellation active in Waves Nx Speakers mode |
| **`XTC_SPEAKER_DISTANCE`** | **`34` cm** (`0.340 m`) | Exact physical flank-to-flank transducer span on XPS 17 9720! |
| **`XTC_SPEAKER_EAR_DISTANCE`** | **`72` cm** (`0.720 m`) | Nominal Waves listening distance (`±` nearfield sweet spot `0.55–0.72 m`) |
| **`XTC_LF_SPLIT_FREQ` / `Q`** | **`351 Hz` / `Q = 1.6`** | **Critical design cue:** Waves NEVER applies XTC below **`351 Hz`** — low frequencies stay strictly mono-anchored so XTC cancellation never steals bass headroom or causes bass pumping! |
| `XTC_CAM_TO_SPEAKERS_Y_OFS` | `-12` cm | Physical camera-to-speaker vertical offset |
| `XTC_HEAD_DIAMETER` / `EAR_SPAN` | `18 cm` / `180°` | Standard anthropometric head model |
| `AMBISONICSV2_STEREO_ANGLE` / `SURROUND_ANGLE` | **`75°`** / **`90°`** (N360: `100°`/`180°`) | Virtual stereo speaker angle (`±37.5°`, wider than physical `±13°–17°`!) |
| `AMBISONICSV2_GAIN` | `+1.5 dB` (`Nx Speakers On`) | Compensates spatialization level loss |
| `AMBISONICSV2_REMOVE_SPEAKER_DELAY` | `1` | Compensates physical propagation delay |
| `N360_BASS_CUT` / `N360_BASS_GAIN` | **`80 Hz`** / **`+3.0 dB`** | Spatial bass management floor (`80 Hz`, matching our `Physical Bass Hz = 82 Hz`!) |
| `SFX[105]` Spatial Compensation EQ | `+1.9 dB @ 20 Hz`, `+1.6 dB @ 79 Hz`, `-2.5 dB @ 686 Hz`, `-2.0 dB @ 1005 Hz`, `+1.8 dB @ 1421 Hz`, `+2.6 dB @ 5037 Hz` (high-shelf) | Mild tonal rebalancing (`+1.6 dB` bass, `+2.6 dB` >5 kHz air) to counteract XTC comb-filtering dullness |

---

## 7. Actionable Cross-Team Handoffs

### For Lead 1 (Kernel Driver, SoundWire & SOF IPC4)
1. **Immediate Safe Silicon Fixes (Zero Loudness Risk):**
   - Port `0001-bq-in-io-init.patch` + `0002-xu24-truthful.patch` (`XU24 reg_default = 0x00`).
   - Add **`0xC202 = 0xC0`** followed by explicit **`0xC200 = 0xC0`** update pulse in `rt1316_io_init()` (and/or DAPM playback widget event matching `OpenCtl.txt`/`CloseCtl.txt`), AND add **`0xC200` to `rt1316_volatile_register()`** so `regcache_sync()` can never replay a stale pulse before `0xC203..0xC252`.
   - Support optional 48 kHz-corrected BQ table (`analyze_windows_tables.py` emits exact Q5.27 hex for both shipped 44.1 kHz and corrected 48 kHz sets).
2. **IPC4 Sense-Path Enablement:**
   - Assign non-colliding Amp 2 (`sdw:0:2`) IV-sense tags **`0x3004 = 0xE4`, `0x3005 = 0xF5`** (vs Amp 1 `0xA0`/`0xB1`).
   - Expose/test `0xC007 = 0x22`, `0xC008 = 0x22`, `0x2F0A = 0x01` if PCM 3 sense rate needs 24 kHz parity.
   - Fence ALSA UCM2 **`Post Mixer Speaker Playback DRC switch = off`** (`sof-soundwire.conf:258` enables `+13.7 dB` makeup DRC on IPC4 init!).
3. **P4 250 ms Deadman Gate:**
   - Gate `0xC60F = 0x05` + `0xD002/0xD004 = 0xA9` strictly behind the 250 ms PID-bound heartbeat + verified `0xC202 == 0xC0` + active DP2 capture. Hard-block all 9 forbidden registers (`0xC000, 0xC050, 0xC082, 0xC093, 0xC09C, 0xC09D, 0xC0A3, 0xC102, 0xC60E`).

### For Lead 2 (Acoustics & Klippel Modeling)
1. **Hardware Crossover Integration:** Once `0xC202 = 0xC0` is active on both amps, tweeters gain `29 dB` protection at `200 Hz` and woofers roll off at `6.16 kHz` (or `5.67 kHz` at 48 kHz). Note the small `+1.56 dB @ 1.47 kHz` tweeter Q bump and `+1.25 dB @ 31 Hz` subsonic bump when designing the 4096-tap FIR inverse filters.
2. **Chassis Resonance Prior Map (L/R Asymmetric!):** Dell's `EFX[0]` reveals exact physical resonances to cross-check against the 455 Blue Yeti measurements: **`344–382 Hz` (`-7.5 / -8.4 dB`)**, **`778–928 Hz` (`-4.9 to -7.6 dB`)**, **`1.0–1.4 kHz` (`-3.2 to -5.2 dB`)**, **`1.73–1.88 kHz` (`-6.7 / -6.4 dB`)**, **`3.11–3.14 kHz` (`-8.8 / -9.5 dB`)**, plus R-only cavity modes at `224 Hz` & `290 Hz`.
3. **Volume-Adaptive Excursion & Bass Curve:** Dell's newly decoded `LINKLAWS` (`64 Hz`: `-0.2 dB` up to 75% vol -> `-4.5 dB` at 100% vol; `MV2` dynamic limiter at `82–93 Hz` with `1 ms` attack; `VARISLOPE` HPF at `40 Hz`; `N360_BASS_CUT = 80 Hz`) confirms that **physical bass down to `~80 Hz`** is viable on these woofers when paired with fast excursion limiting — validating our replacement of the broken static `180 Hz` 48 dB/oct brickwall HPF!
4. **Physical Model Bounds:** Nominal `Re0 ≈ 4.0 Ω` (`2.8 Vrms` Basic full scale = `1.96 W`), `T_limit ≤ 100 °C` conservative working ceiling, `RTSPKPT` `RecA/RecB` asymmetry ratios (`2.46×` at `+0x18`, `2.0×` at `+0x24`, `3.9×` at `+0x30`) — all marked provisional until measured via IPC4 I-sense.

### For Lead 3 (DSP / SIMD Engine)
1. **Psychoacoustic Bass Architecture:** Follow Waves' proven tuning cues from `MFX[0]` + `LINKLAWS`: harmonic generator centered around **`88–103 Hz`** (instead of choking at 180 Hz), upper-harmonic LPF around **`208–314 Hz`**, and **volume-adaptive harmonic intensity scaling** (`3.5×` higher harmonic weight below 45% volume, smoothly tapering above 75% volume where physical SPL takes over).
2. **Post-Dynamics Mono-Anchored XTC:** Respect Waves' exact `SFX[100]` XTC boundary: **`XTC_LF_SPLIT_FREQ = 351 Hz`** (`S = 0.340 m`, `D = 0.55–0.72 m`), plus center-channel anchoring (`CENTER_GAIN_C/LR = 50/65`) and running XTC **after** multiband dynamics so Side boost never pumps the bass or vocal core.
3. **Dynamics & Ceilings:** Mirror Waves' musical time constants (fast `1–4 ms` attack on the `82–93 Hz` woofer excursion band, gentle `88 ms / 800 ms` leveling compression, `-1.0` to `-1.1 dB` output ceiling trim) rather than a fast `20 ms` brickwall pumping the full band.

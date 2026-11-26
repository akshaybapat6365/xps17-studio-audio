#!/usr/bin/env python3
"""Read-only validation, benchmark README sync, and clean staging bundler
for the Dell XPS 17 9720 Studio Audio Engineering Stack.

SAFETY GUARANTEES:
  - Strictly offline/local filesystem operations only.
  - NEVER invokes `git push`, `gh`, or modifies live user files (`~/.config/*`).
  - Scans all staged text files for accidentally leaked secrets/tokens.
  - Validates exact benchmark table markers in README.md and structural YAML
    syntax of `.github/workflows/ci.yml`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

BENCH_START = "<!-- BENCHMARK_TABLE_START -->"
BENCH_END = "<!-- BENCHMARK_TABLE_END -->"

EXPECTED_PACKAGING_FILES = [
    "README.md",
    "ARCHITECTURE.md",
    "LICENSE",
    "bootstrap.sh",
    "PKGBUILD",
    "install.sh",
    "uninstall.sh",
    "sync_repo_bundle.py",
    ".github/workflows/ci.yml",
]

EXPECTED_CHARTS = [
    "01_frequency_response.png",
    "02_bass_and_body_comparison.png",
    "03_stereo_correlation_and_depth.png",
    "04_dynamics_loudness_thd.png",
]

SECRET_PATTERNS = [
    ("GitHub PAT", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Private Key Header", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("Provider Secret Token", re.compile(r"\b(?:sk-[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{20,})\b")),
]

IGNORE_DIR_NAMES = {
    ".git",
    "target",
    "__pycache__",
    ".pytest_cache",
    ".venv",
    ".runs",
    "node_modules",
}


def detect_roots(script_path: Path) -> tuple[Path, Path]:
    """Return (repo_root, packaging_dir) for both monorepo and published layouts."""
    script_dir = script_path.resolve().parent
    if (script_dir / "verify-integrate").is_dir() or (script_dir / "dsp-engine").is_dir():
        return script_dir, script_dir
    if script_dir.name == "packaging" and (script_dir.parent.parent / "verify-integrate").is_dir():
        return script_dir.parent.parent, script_dir
    return script_dir, script_dir


def scan_secrets(base_dir: Path) -> list[str]:
    """Return list of secret-pattern matches under base_dir."""
    hits: list[str] = []
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIR_NAMES]
        for fn in sorted(files):
            full = Path(root) / fn
            try:
                if full.is_symlink() or full.stat().st_size > 2 * 1024 * 1024:
                    continue
                text = full.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for label, pat in SECRET_PATTERNS:
                if pat.search(text):
                    hits.append(f"{full.relative_to(base_dir)}: matched {label}")
    return hits


def validate_yaml_file(yaml_path: Path) -> tuple[bool, str]:
    """Validate GitHub Actions CI YAML via PyYAML if installed."""
    text = yaml_path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore

        doc = yaml.safe_load(text)
    except ModuleNotFoundError:
        # Fallback minimal structural check if PyYAML is absent
        if "jobs:" not in text or "steps:" not in text:
            return False, "Missing 'jobs:' or 'steps:' keys (PyYAML unavailable)"
        return True, "Basic YAML structure OK (PyYAML not installed)"
    except Exception as exc:  # noqa: BLE001
        return False, f"YAML parse error: {exc}"

    if not isinstance(doc, dict):
        return False, "Top-level YAML must be a mapping"
    if "jobs" not in doc or not isinstance(doc["jobs"], dict) or not doc["jobs"]:
        return False, "CI workflow missing non-empty 'jobs' mapping"
    # PyYAML parses unquoted `on:` as boolean True in YAML 1.1
    if "on" not in doc and True not in doc:
        return False, "CI workflow missing 'on' trigger block"
    return True, f"PyYAML parsed cleanly ({len(doc['jobs'])} jobs: {', '.join(doc['jobs'].keys())})"


def check_bash_syntax(script_path: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        ["bash", "-n", str(script_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        return True, "bash -n OK"
    return False, (proc.stderr or proc.stdout).strip()


def run_check(repo_root: Path, pkg_dir: Path) -> int:
    print(f"=== Packaging & Repository Layout Audit ===")
    print(f"repo_root     : {repo_root}")
    print(f"packaging_dir : {pkg_dir}")

    errors: list[str] = []

    # 1. Required packaging files
    for rel in EXPECTED_PACKAGING_FILES:
        p = pkg_dir / rel
        if not p.is_file():
            errors.append(f"Missing required packaging file: {p}")
        else:
            print(f"  [OK] {rel} ({p.stat().st_size} bytes)")

    # 2. README benchmark markers & chart references
    readme_path = pkg_dir / "README.md"
    if readme_path.is_file():
        rtext = readme_path.read_text(encoding="utf-8")
        if BENCH_START not in rtext or BENCH_END not in rtext:
            errors.append("README.md missing BENCHMARK_TABLE_START / BENCHMARK_TABLE_END markers")
        elif rtext.index(BENCH_START) >= rtext.index(BENCH_END):
            errors.append("README.md benchmark markers are inverted")
        else:
            print("  [OK] README.md benchmark sync markers present")
        for chart in EXPECTED_CHARTS:
            if f"docs/charts/{chart}" not in rtext:
                errors.append(f"README.md missing relative chart link docs/charts/{chart}")

    # 3. Validate CI YAML
    ci_path = pkg_dir / ".github/workflows/ci.yml"
    if ci_path.is_file():
        ok, msg = validate_yaml_file(ci_path)
        if ok:
            print(f"  [OK] .github/workflows/ci.yml: {msg}")
        else:
            errors.append(f".github/workflows/ci.yml: {msg}")

    # 4. Validate shell syntax
    for sh_name in ("bootstrap.sh", "install.sh", "uninstall.sh"):
        sh_path = pkg_dir / sh_name
        if sh_path.is_file():
            ok, msg = check_bash_syntax(sh_path)
            if ok:
                print(f"  [OK] {sh_name}: {msg}")
            else:
                errors.append(f"{sh_name}: {msg}")

    # 5. Secret scan on packaging directory
    secret_hits = scan_secrets(pkg_dir)
    if secret_hits:
        for h in secret_hits:
            errors.append(f"Secret scan hit: {h}")
    else:
        print("  [OK] Secret scan: zero credentials/tokens found")

    # 6. Informational readiness inventory of sibling layers
    print("\n=== Sibling Layer Readiness (Informational) ===")
    probes = [
        ("kernel-driver/", repo_root / "kernel-driver"),
        ("acoustics-research/kernels/", repo_root / "acoustics-research/kernels"),
        ("dsp-engine/lib/libxps17_studio_dsp.so", repo_root / "dsp-engine/lib/libxps17_studio_dsp.so"),
        ("dsp-engine/bin/xps17-studio-cli", repo_root / "dsp-engine/bin/xps17-studio-cli"),
        ("verify-integrate/bench/benchmark_results.json", repo_root / "verify-integrate/bench/benchmark_results.json"),
        ("verify-integrate/bench/charts/", repo_root / "verify-integrate/bench/charts"),
        ("verify-integrate/staged/pipewire/", repo_root / "verify-integrate/staged/pipewire"),
    ]
    for label, path in probes:
        if path.is_file():
            status = f"READY (file, {path.stat().st_size} bytes)"
        elif path.is_dir():
            count = sum(1 for _ in path.rglob("*") if _.is_file())
            status = f"PRESENT ({count} files)" if count > 0 else "PENDING (0 files yet)"
        else:
            status = "PENDING (not built yet)"
        print(f"  - {label:<46} {status}")

    if errors:
        print("\nAUDIT FAILED:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print("\nPACKAGING AUDIT PASSED.")
    return 0


def _fmt_num(val: Any, suffix: str = "", digits: int = 2) -> str:
    if val is None or val == "":
        return "—"
    if isinstance(val, (int, float)):
        return f"`{val:.{digits}f}{suffix}`"
    return f"`{val}`"


def _pick(row: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in row and row[k] is not None:
            return row[k]
    return None


def render_benchmark_table(data: Any) -> str:
    """Render benchmark_results.json (chains or flat dict/list schema) into a clean GFM table."""
    if isinstance(data, dict) and isinstance(data.get("chains"), dict):
        chains = data["chains"]
        cpu_bench = data.get("cpu_benchmarks", {})
        new_modes = cpu_bench.get("new_studio_dsp", {}).get("modes", {})
        old_cpu = cpu_bench.get("old_spatial_fdtr", {}).get("fused_detail", {})
        stock_bass = (
            chains.get("stock_raw", {})
            .get("pink_noise_acoustic_bands", {})
            .get("physical_bass_70_180_db")
        )

        lines = [
            "| Pipeline / Profile | Physical Bass 70–180 Hz (dB rel. Stock) | Perceived Bass Index (dB) | Vocal+Bass Delta (dB) | Sub-180 Hz Mono `r` | Kick LUFS | True Peak (dBTP) | Max Excursion (% Xmax) | 1 kHz THD+N (%) | Block Time (µs) | E-Core CPU @ 48 kHz (%) |",
            "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
        ]
        mode_map = {
            "new_studio_deep": "deep",
            "new_studio_spatial": "spatial",
            "new_studio_reference": "reference",
            "new_studio_voice": "voice",
        }
        for cid, ch in chains.items():
            if not isinstance(ch, dict):
                continue
            label = str(ch.get("label") or cid)
            pb_abs = ch.get("pink_noise_acoustic_bands", {}).get("physical_bass_70_180_db")
            pb_rel = (pb_abs - stock_bass) if (isinstance(pb_abs, (int, float)) and isinstance(stock_bass, (int, float))) else None
            pbi = ch.get("bass_kick_psychoacoustics", {}).get("perceived_bass_index_db")
            vb_delta = ch.get("vocal_bass_preservation", {}).get("bass_preservation_delta_db")
            mono_r = (
                ch.get("stereo_uncorrelated", {})
                .get("bands", {})
                .get("sub180_hz", {})
                .get("correlation_r")
            )
            lufs = ch.get("bass_kick_dynamics", {}).get("integrated_lufs")
            tp = ch.get("bass_kick_dynamics", {}).get("true_peak_dbtp")
            exc_pct = ch.get("klippel_excursion", {}).get("peak_pct_xmax")
            thd_1k = ch.get("distortion", {}).get("thd_n_1khz", {}).get("thdn_percent")

            if cid in mode_map and mode_map[cid] in new_modes:
                m_cpu = new_modes[mode_map[cid]]
                us_blk = m_cpu.get("us_per_block")
                cpu_pct = m_cpu.get("cpu_percent")
            elif cid == "old_spatial_fdtr":
                us_blk = old_cpu.get("us_per_block")
                cpu_pct = old_cpu.get("cpu_percent")
            else:
                us_blk = 0.0 if cid == "stock_raw" else None
                cpu_pct = 0.0 if cid == "stock_raw" else None

            pb_str = f"`{pb_rel:+.2f} dB`" if isinstance(pb_rel, (int, float)) else "—"
            vb_str = f"`{vb_delta:+.2f} dB`" if isinstance(vb_delta, (int, float)) else "—"
            lines.append(
                f"| **{label}** | {pb_str} | {_fmt_num(pbi, ' dB', 2)} | {vb_str} | {_fmt_num(mono_r, '', 4)} | {_fmt_num(lufs, '', 1)} | {_fmt_num(tp, '', 2)} | {_fmt_num(exc_pct, '%', 1)} | {_fmt_num(thd_1k, '%', 3)} | {_fmt_num(us_blk, ' µs', 1)} | {_fmt_num(cpu_pct, '%', 2)} |"
            )
        return "\n".join(lines)

    rows: list[tuple[str, dict[str, Any]]] = []

    if isinstance(data, dict):
        if "profiles" in data and isinstance(data["profiles"], dict):
            rows = list(data["profiles"].items())
        elif "profiles" in data and isinstance(data["profiles"], list):
            for item in data["profiles"]:
                if isinstance(item, dict):
                    rows.append((str(item.get("name") or item.get("profile") or "unknown"), item))
        elif "results" in data and isinstance(data["results"], dict):
            rows = list(data["results"].items())
        elif "results" in data and isinstance(data["results"], list):
            for item in data["results"]:
                if isinstance(item, dict):
                    rows.append((str(item.get("name") or item.get("profile") or "unknown"), item))
        else:
            for k, v in data.items():
                if isinstance(v, dict):
                    rows.append((str(k), v))
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                rows.append((str(item.get("name") or item.get("profile") or "unknown"), item))

    if not rows:
        raise ValueError("Could not extract profile rows from benchmark_results.json")

    lines = [
        "| Pipeline / Profile | Target Error RMSE (dB) | Low-Bass Energy 60–180 Hz (dB rel. Stock) | Integrated Loudness (LUFS) | True Peak (dBTP) | Max Excursion (% Xmax) | Mono-Sum Ripple (dB) | THD @ 1 kHz (%) | DSP Block Latency p99 (µs) | CPU Load @ 48 kHz (%) |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ]
    for name, metrics in rows:
        rmse = _fmt_num(_pick(metrics, "target_rmse_db", "rmse_db", "target_error_rmse_db"), "", 2)
        bass = _fmt_num(_pick(metrics, "bass_energy_60_180_db", "low_bass_gain_db", "bass_60_180_rel_stock_db"), "", 2)
        lufs = _fmt_num(_pick(metrics, "integrated_lufs", "lufs", "loudness_lufs"), "", 1)
        tp = _fmt_num(_pick(metrics, "true_peak_dbtp", "true_peak_db", "max_true_peak_dbtp"), "", 2)
        exc = _fmt_num(_pick(metrics, "max_excursion_pct", "excursion_pct_xmax", "peak_excursion_pct"), "%", 1)
        mono = _fmt_num(_pick(metrics, "mono_sum_ripple_db", "mono_ripple_db"), "", 3)
        thd = _fmt_num(_pick(metrics, "thd_1khz_pct", "thd_pct"), "%", 3)
        lat = _fmt_num(_pick(metrics, "latency_p99_us", "dsp_latency_p99_us", "p99_us"), "", 1)
        cpu = _fmt_num(_pick(metrics, "cpu_load_pct", "cpu_pct_48k", "cpu_pct"), "%", 2)
        lines.append(
            f"| **{name}** | {rmse} | {bass} | {lufs} | {tp} | {exc} | {mono} | {thd} | {lat} | {cpu} |"
        )
    return "\n".join(lines)


def update_readme_benchmarks(readme_path: Path, bench_json: Path, dry_run: bool) -> int:
    if not bench_json.is_file():
        print(f"ERROR: benchmark JSON not found: {bench_json}", file=sys.stderr)
        return 1
    if not readme_path.is_file():
        print(f"ERROR: README.md not found: {readme_path}", file=sys.stderr)
        return 1

    raw = json.loads(bench_json.read_text(encoding="utf-8"))
    table_md = render_benchmark_table(raw)

    readme_text = readme_path.read_text(encoding="utf-8")
    if BENCH_START not in readme_text or BENCH_END not in readme_text:
        print("ERROR: README.md missing benchmark sync markers", file=sys.stderr)
        return 1

    start_idx = readme_text.index(BENCH_START) + len(BENCH_START)
    end_idx = readme_text.index(BENCH_END)
    updated = readme_text[:start_idx] + "\n" + table_md + "\n" + readme_text[end_idx:]

    if dry_run:
        print("=== Dry-run rendered benchmark table ===")
        print(table_md)
        return 0

    tmp_path = readme_path.with_suffix(".md.tmp")
    tmp_path.write_text(updated, encoding="utf-8")
    os.replace(tmp_path, readme_path)
    print(f"Updated benchmark table in {readme_path} from {bench_json}")
    return 0


def _copy_filtered_tree(src: Path, dst: Path) -> int:
    copied = 0
    if not src.exists():
        return 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIR_NAMES]
        rel_root = Path(root).relative_to(src)
        target_root = dst / rel_root
        target_root.mkdir(parents=True, exist_ok=True)
        for fn in sorted(files):
            if fn.endswith((".pyc", ".pyo", ".tmp")) or ".xps17tmp." in fn:
                continue
            sfile = Path(root) / fn
            if sfile.is_symlink():
                continue
            dfile = target_root / fn
            shutil.copy2(sfile, dfile)
            copied += 1
    return copied


def stage_release_bundle(repo_root: Path, pkg_dir: Path, stage_dir: Path, overwrite: bool) -> int:
    stage_dir = stage_dir.resolve()
    if str(stage_dir) in ("/", "/root") or str(stage_dir).startswith("/root/"):
        print(f"REFUSED: unsafe stage directory '{stage_dir}'", file=sys.stderr)
        return 2
    try:
        stage_dir.relative_to(repo_root)
        print(
            f"REFUSED: stage directory '{stage_dir}' must lie outside repo_root '{repo_root}' to prevent recursive copy.",
            file=sys.stderr,
        )
        return 2
    except ValueError:
        pass

    if stage_dir.exists():
        if not overwrite:
            print(f"ERROR: stage directory already exists: {stage_dir} (pass --overwrite to replace)", file=sys.stderr)
            return 1
        shutil.rmtree(stage_dir)

    stage_dir.mkdir(parents=True, exist_ok=True)

    # 1. Root packaging files
    for rel in EXPECTED_PACKAGING_FILES:
        src = pkg_dir / rel
        if src.is_file():
            dst = stage_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    for exe in ("bootstrap.sh", "install.sh", "uninstall.sh", "sync_repo_bundle.py"):
        p = stage_dir / exe
        if p.is_file():
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # 2. Clean .gitignore
    gitignore = "\n".join(
        [
            "# Build & runtime artifacts",
            "dsp-engine/target/",
            "**/__pycache__/",
            "**/*.py[cod]",
            "**/.pytest_cache/",
            "**/.venv/",
            "**/.runs/",
            "*.tmp",
            "*.xps17tmp.*",
            ".DS_Store",
            "",
        ]
    )
    (stage_dir / ".gitignore").write_text(gitignore, encoding="utf-8")

    # 3. Copy engineering layers
    counts: dict[str, int] = {}
    for layer in ("kernel-driver", "acoustics-research", "dsp-engine"):
        counts[layer] = _copy_filtered_tree(repo_root / layer, stage_dir / layer)

    # 4. Copy verify-integrate (excluding redundant packaging/ subdir)
    vi_src = repo_root / "verify-integrate"
    vi_copied = 0
    if vi_src.is_dir():
        for sub in sorted(vi_src.iterdir()):
            if sub.name == "packaging" or sub.name in IGNORE_DIR_NAMES:
                continue
            if sub.is_dir():
                vi_copied += _copy_filtered_tree(sub, stage_dir / "verify-integrate" / sub.name)
            elif sub.is_file():
                (stage_dir / "verify-integrate").mkdir(parents=True, exist_ok=True)
                shutil.copy2(sub, stage_dir / "verify-integrate" / sub.name)
                vi_copied += 1
    counts["verify-integrate"] = vi_copied

    # 5. Copy charts into docs/charts/
    charts_src = repo_root / "verify-integrate/bench/charts"
    charts_dst = stage_dir / "docs/charts"
    charts_dst.mkdir(parents=True, exist_ok=True)
    charts_copied = 0
    if charts_src.is_dir():
        for png in sorted(charts_src.glob("*.png")):
            shutil.copy2(png, charts_dst / png.name)
            charts_copied += 1
    counts["docs/charts"] = charts_copied

    # 6. Auto-sync benchmark table into staged README if benchmark_results.json exists
    bench_json = repo_root / "verify-integrate/bench/benchmark_results.json"
    if bench_json.is_file():
        update_readme_benchmarks(stage_dir / "README.md", bench_json, dry_run=False)

    # 7. Final secret scan on staged bundle
    hits = scan_secrets(stage_dir)
    if hits:
        print("SECRET SCAN FAILED ON STAGED BUNDLE:", file=sys.stderr)
        for h in hits:
            print(f"  - {h}", file=sys.stderr)
        return 1

    print(f"Staged publication bundle cleanly at: {stage_dir}")
    for k, v in counts.items():
        print(f"  {k:<20}: {v} files")
    print("Note: Local staging only. Review with `git status`/`git diff` before publishing manually.")
    return 0


def main() -> int:
    repo_root, pkg_dir = detect_roots(Path(__file__))
    parser = argparse.ArgumentParser(
        description="Validate, sync benchmark metrics, or assemble a clean GitHub release bundle (offline only; never pushes)."
    )
    parser.add_argument("--check", action="store_true", help="Validate packaging artifacts, CI YAML, shell scripts & secret scan")
    parser.add_argument("--update-readme", action="store_true", help="Update README.md benchmark table from benchmark_results.json")
    parser.add_argument("--bench-json", type=Path, default=repo_root / "verify-integrate/bench/benchmark_results.json", help="Path to benchmark_results.json")
    parser.add_argument("--readme", type=Path, default=pkg_dir / "README.md", help="Path to target README.md")
    parser.add_argument("--dry-run", action="store_true", help="Preview --update-readme output without modifying README.md")
    parser.add_argument("--stage-dir", type=Path, default=None, help="Assemble clean ready-to-publish repository directory")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing --stage-dir if present")

    args = parser.parse_args()
    if not (args.check or args.update_readme or args.stage_dir):
        args.check = True

    rc = 0
    if args.check:
        rc = run_check(repo_root, pkg_dir)
        if rc != 0:
            return rc

    if args.update_readme:
        rc = update_readme_benchmarks(args.readme, args.bench_json, dry_run=args.dry_run)
        if rc != 0:
            return rc

    if args.stage_dir is not None:
        rc = stage_release_bundle(repo_root, pkg_dir, args.stage_dir, overwrite=args.overwrite)
        if rc != 0:
            return rc

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

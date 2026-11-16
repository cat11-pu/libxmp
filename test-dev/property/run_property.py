#!/usr/bin/env python3
"""Build and run the malformed-module property test and OpenMPT comparison.

Malformed run: 200 cases, one process each, ASan + UBSan + LSan enabled.
Reference run: 50 valid modules compared per sample/channel/frame against the
checked-in OpenMPT traces.
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PROP = os.path.join(ROOT, "test-dev", "property")
BUILD = os.path.join(ROOT, "artifacts", "property", "build")
CORPUS = os.path.join(ROOT, "artifacts", "property", "corpus")
REPORT = os.path.join(ROOT, "artifacts", "property")
SAN_FLAGS = [
    "-O1", "-g", "-fno-omit-frame-pointer",
    "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
]


def run(cmd, cwd=ROOT, env=None, timeout=None):
    return subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout,
                          text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)


def build():
    os.makedirs(BUILD, exist_ok=True)
    cmake = run([
        "cmake", "-S", ROOT, "-B", BUILD,
        "-DBUILD_SHARED=OFF", "-DBUILD_STATIC=ON",
        "-DWITH_UNIT_TESTS=OFF",
        "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
        "-DCMAKE_C_FLAGS=" + " ".join(SAN_FLAGS),
    ])
    if cmake.returncode != 0:
        print(cmake.stdout)
        print(cmake.stderr, file=sys.stderr)
        raise SystemExit("cmake configure failed")
    make = run(["cmake", "--build", BUILD, "--target", "xmp_static", "-j2"], timeout=600)
    if make.returncode != 0:
        print(make.stdout)
        print(make.stderr, file=sys.stderr)
        raise SystemExit("library build failed")

    lib = os.path.join(BUILD, "libxmp.a")
    runner = os.path.join(BUILD, "property_runner")
    refcmp = os.path.join(BUILD, "ref_compare")
    common = ["cc", "-std=c99", "-I", os.path.join(ROOT, "include"),
              "-I", os.path.join(ROOT, "src")] + SAN_FLAGS
    for src, dst in ((os.path.join(PROP, "property_runner.c"), runner),
                     (os.path.join(PROP, "ref_compare.c"), refcmp)):
        cc = run(common + [src, lib, "-lm", "-o", dst])
        if cc.returncode != 0:
            print(cc.stdout)
            print(cc.stderr, file=sys.stderr)
            raise SystemExit("harness build failed: " + src)
    return runner, refcmp


def generate():
    gen = run([sys.executable, os.path.join(PROP, "generate_corpus.py"),
               "--root", ROOT, "--out", os.path.relpath(CORPUS, ROOT), "--count", "200"])
    if gen.returncode != 0:
        print(gen.stdout)
        print(gen.stderr, file=sys.stderr)
        raise SystemExit("corpus generation failed")
    print(gen.stdout.strip())


def sanitizer_env():
    env = dict(os.environ)
    env["ASAN_OPTIONS"] = ("detect_leaks=1:halt_on_error=1:abort_on_error=1:"
                           "allocator_may_return_null=1:detect_stack_use_after_return=1")
    env["UBSAN_OPTIONS"] = "halt_on_error=1:print_stacktrace=1"
    env["LSAN_OPTIONS"] = "exitcode=23:report_objects=1"
    return env


def classify(stderr):
    if "LeakSanitizer" in stderr or "detected memory leaks" in stderr:
        return "leak"
    if "AddressSanitizer" in stderr:
        return "out_of_bounds"
    if "runtime error:" in stderr:
        return "undefined_behavior"
    return "crash"


def malformed(runner):
    generate()
    manifest = []
    with open(os.path.join(CORPUS, "manifest.jsonl"), encoding="utf-8") as f:
        for line in f:
            manifest.append(json.loads(line))

    env = sanitizer_env()
    results = []
    failures = []
    for rec in manifest:
        path = os.path.join(ROOT, rec["file"])
        try:
            proc = run([runner, path], env=env, timeout=10)
            stderr = proc.stderr
            if proc.returncode == 0:
                status = "pass"
            elif "INVARIANT" in proc.stdout:
                status = "invariant"
            else:
                status = classify(stderr)
        except subprocess.TimeoutExpired as e:
            proc = None
            stderr = (e.stderr or "") if isinstance(e.stderr, str) else ""
            status = "timeout"

        row = dict(rec)
        row["status"] = status
        if proc is not None and proc.returncode == 0:
            m = re.search(r"RESULT .*", proc.stdout)
            row["result"] = m.group(0) if m else proc.stdout.strip()
        else:
            lines = [ln for ln in (stderr + "\n" + proc.stdout).splitlines()
                     if "Sanitizer" in ln or "runtime error" in ln or "SUMMARY" in ln
                     or "ERROR" in ln or "INVARIANT" in ln]
            row["diagnostic"] = "\n".join(lines[:8])
            failures.append(row)
        results.append(row)

    summary = {
        "total": len(results),
        "pass": sum(r["status"] == "pass" for r in results),
        "fail": len(failures),
        "by_class": {},
        "failures": failures,
    }
    for cls in ("truncate", "bad_pattern", "huge_counts", "cycle_refs"):
        rows = [r for r in results if r["class"] == cls]
        summary["by_class"][cls] = {
            "total": len(rows),
            "pass": sum(r["status"] == "pass" for r in rows),
            "fail": sum(r["status"] != "pass" for r in rows),
        }

    with open(os.path.join(REPORT, "malformed_results.jsonl"), "w", encoding="utf-8") as f:
        for row in results:
            f.write(json.dumps(row, sort_keys=True) + "\n")
    with open(os.path.join(REPORT, "malformed_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, sort_keys=True)
    return summary


def discover_reference_pairs():
    pairs = []
    pat = re.compile(r'(compare_mixer_data(?:_loops|_no_rv|_player_mode)?)\s*'
                     r'\(\s*"([^"]+)"\s*,\s*"([^"]+)"', re.S)
    for path in sorted(glob.glob(os.path.join(ROOT, "test-dev", "test_openmpt_*.c"))):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for fn, mod, data in pat.findall(text):
            mod_path = os.path.join(ROOT, "test-dev", mod)
            data_path = os.path.join(ROOT, "test-dev", data)
            if os.path.isfile(mod_path) and os.path.isfile(data_path):
                mode = 1 if fn == "compare_mixer_data_player_mode" else 0
                ignore_rv = 1 if fn == "compare_mixer_data_no_rv" else 0
                pairs.append((mod, data, mode, ignore_rv))
    return pairs


def choose_reference_pairs():
    pairs = discover_reference_pairs()
    by_fmt = {}
    for mod, data, mode, ignore_rv in pairs:
        parts = mod.split("/")
        fmt = parts[1] if len(parts) > 1 else parts[0]
        by_fmt.setdefault(fmt, []).append((mod, data, mode, ignore_rv))
    # 50 balanced samples: all MOD/S3M, then IT and XM round-robin.
    chosen = []
    chosen += by_fmt.get("mod", [])
    chosen += by_fmt.get("s3m", [])
    rest = []
    it = list(by_fmt.get("it", []))
    xm = list(by_fmt.get("xm", []))
    for i in range(max(len(it), len(xm))):
        if i < len(it):
            rest.append(it[i])
        if i < len(xm):
            rest.append(xm[i])
    chosen += rest[:50 - len(chosen)]
    return chosen[:50]


def reference(refcmp):
    pairs = choose_reference_pairs()
    if len(pairs) != 50:
        raise SystemExit(f"expected 50 reference pairs, found {len(pairs)}")

    env = sanitizer_env()
    results = []
    failures = []
    for mod, data, mode, ignore_rv in pairs:
        proc = run([refcmp, os.path.join(ROOT, "test-dev", mod),
                    os.path.join(ROOT, "test-dev", data),
                    str(mode), str(ignore_rv)], env=env, timeout=30)
        ok = proc.returncode == 0 and "MISMATCH" not in proc.stdout
        row = {"module": mod, "trace": data, "mode": mode,
               "ignore_rv": ignore_rv, "status": "pass" if ok else "fail",
               "output": proc.stdout.strip(), "stderr": proc.stderr.strip()}
        if not ok:
            failures.append(row)
        results.append(row)

    summary = {
        "total": len(results),
        "pass": sum(r["status"] == "pass" for r in results),
        "fail": len(failures),
        "mismatches": failures,
    }
    with open(os.path.join(REPORT, "reference_results.jsonl"), "w", encoding="utf-8") as f:
        for row in results:
            f.write(json.dumps(row, sort_keys=True) + "\n")
    with open(os.path.join(REPORT, "reference_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, sort_keys=True)
    return results, summary


def write_summary(ms, rs):
    with open(os.path.join(REPORT, "malformed_results.jsonl"), encoding="utf-8") as f:
        malformed_rows = [json.loads(line) for line in f]
    with open(os.path.join(REPORT, "reference_results.jsonl"), encoding="utf-8") as f:
        reference_rows = [json.loads(line) for line in f]

    load_status = {}
    played = 0
    frames = 0
    for row in malformed_rows:
        m = re.search(r"test=(-?\d+) load=(-?\d+) play=(-?\d+) frames=(\d+)",
                      row.get("result", ""))
        if not m:
            continue
        load_status[m.group(2)] = load_status.get(m.group(2), 0) + 1
        n = int(m.group(4))
        frames += n
        played += n > 0

    fmt_counts = {}
    for row in reference_rows:
        parts = row["module"].split("/")
        fmt = parts[1] if len(parts) > 1 else parts[0]
        fmt_counts[fmt] = fmt_counts.get(fmt, 0) + 1

    lines = [
        "# libxmp module property test",
        "",
        "## Malformed corpus",
        "",
        f"- Cases: {ms['pass']}/{ms['total']} passed",
        f"- Crashes/OOB/leaks/timeouts: {ms['fail']}",
    ]
    for cls in ("truncate", "bad_pattern", "huge_counts", "cycle_refs"):
        stat = ms["by_class"][cls]
        lines.append(f"- {cls}: {stat['pass']}/{stat['total']} passed")
    lines += [
        f"- Cases that loaded and played: {played} ({frames} frames total)",
        "- Load result counts: " + ", ".join(
            f"{k}: {v}" for k, v in sorted(load_status.items())),
        "",
        "## OpenMPT reference comparison",
        "",
        f"- Valid modules: {rs['pass']}/{rs['total']} passed",
        "- Format mix: " + ", ".join(
            f"{k}: {v}" for k, v in sorted(fmt_counts.items())),
        "- Comparison unit: each active sample/channel on every replay frame",
        "",
        "## Counterexamples",
        "",
    ]
    if ms["failures"] or rs["mismatches"]:
        for row in ms["failures"]:
            lines.append(
                f"- malformed id={row['id']} class={row['class']} "
                f"offset={row['offset']} status={row['status']} "
                f"file={row['file']}")
        for row in rs["mismatches"]:
            lines.append(f"- reference {row['module']} <=> {row['trace']}: "
                         f"{row['output']}")
    else:
        lines.append("None. Zero crashes, zero sanitizer findings, zero leaks, "
                     "zero reference mismatches.")
    lines.append("")

    with open(os.path.join(REPORT, "SUMMARY.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--only", choices=("malformed", "reference", "all"), default="all")
    args = ap.parse_args()

    os.makedirs(REPORT, exist_ok=True)
    if args.skip_build:
        runner = os.path.join(BUILD, "property_runner")
        refcmp = os.path.join(BUILD, "ref_compare")
    else:
        runner, refcmp = build()

    ms = rs = None
    if args.only in ("malformed", "all"):
        ms = malformed(runner)
        print(f"malformed: {ms['pass']}/{ms['total']} passed, {ms['fail']} failed")
        for cls, stat in ms["by_class"].items():
            print(f"  {cls}: {stat['pass']}/{stat['total']}")
        for row in ms["failures"][:10]:
            print(f"  FAIL id={row['id']} class={row['class']} offset={row['offset']} "
                  f"status={row['status']} file={row['file']}")
            if row.get("diagnostic"):
                print("    " + row["diagnostic"].replace("\n", "\n    "))

    if args.only in ("reference", "all"):
        results, rs = reference(refcmp)
        print(f"reference: {rs['pass']}/{rs['total']} passed, {rs['fail']} failed")
        for row in rs["mismatches"][:10]:
            print(f"  FAIL {row['module']} <=> {row['trace']}")
            print("    " + row["output"].replace("\n", "\n    "))

    if args.only == "all":
        write_summary(ms, rs)
        if ms["fail"] == 0 and rs["fail"] == 0:
            print("PROPERTY PASS")
            return 0
        print("PROPERTY FAIL")
        return 1
    target = ms if args.only == "malformed" else rs
    return 0 if target["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

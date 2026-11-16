#!/usr/bin/env python3
"""Generate 200 deterministic malformed modules for the libxmp property test.

The corpus deliberately alternates four corruption classes:
truncation, illegal pattern/order metadata, huge sample/instrument counts,
and cyclic order/sample-loop references. Every case records the byte offset
that was changed (or the truncation point) in manifest.jsonl.
"""

import argparse
import hashlib
import json
import os
import random
import struct


SEED = 0x3902_2026
CLASSES = ("truncate", "bad_pattern", "huge_counts", "cycle_refs")
FORMATS = ("mod", "xm", "s3m", "it")
BASES = {
    "mod": ("test-dev/openmpt/mod/PatternJump.mod",
            "test-dev/openmpt/mod/PortaTarget.mod"),
    "xm": ("test-dev/openmpt/xm/delay1.xm",
           "test-dev/openmpt/xm/3xxins.xm"),
    "s3m": ("test-dev/openmpt/s3m/ParamMemory.s3m",
            "test-dev/openmpt/s3m/PeriodLimit.s3m"),
    "it": ("test-dev/openmpt/it/Bidi-Loops.it",
           "test-dev/openmpt/it/noteoff2.it"),
}


def u16le(buf, off, value):
    buf[off:off + 2] = struct.pack("<H", value & 0xffff)


def u16be(buf, off, value):
    buf[off:off + 2] = struct.pack(">H", value & 0xffff)


def u32le(buf, off, value):
    buf[off:off + 4] = struct.pack("<I", value & 0xffffffff)


def truncate_case(data, rng):
    # Keep a small file so header-only and short-pattern paths are both hit.
    if len(data) < 8:
        return data[:1], 1

    special = min(len(data) - 1, rng.choice((1, 20, 64, 96, 1080, 0x60, 0x80)))
    off = special if rng.randrange(3) == 0 else rng.randrange(1, len(data))
    return data[:off], off


def bad_pattern_case(data, fmt, rng, rel):
    out = bytearray(data)
    if fmt == "mod":
        # MOD has no pattern count: make the order table point at absurd
        # pattern numbers and simultaneously corrupt the song length.
        off = rel("order_table")
        out[off - 2] = rng.choice((0, 1, 127, 255))
        for i in range(min(16, len(out) - off)):
            out[off + i] = rng.choice((0x7f, 0x80, 0xfe, 0xff))
        return out, off
    if fmt == "xm":
        out[rel("xm_header_size"):rel("xm_header_size") + 4] = struct.pack("<I", rng.choice((0, 4, 0xfffffff0)))
        u16le(out, rel("xm_patterns"), rng.choice((0, 1, 0x7fff, 0xffff)))
        for i in range(8):
            out[rel("order_table") + i] = rng.choice((0, 1, 2, 0x7f, 0xff))
        return out, rel("xm_patterns")
    if fmt == "s3m":
        u16le(out, rel("s3m_orders"), rng.choice((0, 1, 0x7fff, 0xffff)))
        u16le(out, rel("s3m_patterns"), rng.choice((0, 1, 0x7fff, 0xffff)))
        for i in range(8):
            out[rel("order_table") + i] = rng.choice((0, 1, 0x7f, 0xfe, 0xff))
        return out, rel("s3m_patterns")
    if fmt == "it":
        u16le(out, rel("it_patterns"), rng.choice((0, 1, 0x7fff, 0xffff)))
        for i in range(8):
            out[rel("order_table") + i] = rng.choice((0, 1, 2, 0x7f, 0xfe, 0xff))
        # IT pattern parapointers live immediately after the 0x60-byte header.
        base = 0xc0
        if base + 4 <= len(out):
            u32le(out, base, rng.choice((0, 1, len(out) - 1, 0xffffff00)))
        return out, rel("it_patterns")
    return data, 0


def huge_counts_case(data, fmt, rng, rel):
    out = bytearray(data)
    if fmt == "mod":
        # 31 sample headers: length in words, loop start/end in words.
        for i in range(31):
            off = 20 + i * 30
            if off + 30 > len(out):
                break
            u16be(out, off + 22, rng.choice((0x7fff, 0xffff)))
            u16be(out, off + 26, rng.choice((0x7fff, 0xffff)))
            u16be(out, off + 28, rng.choice((0x7fff, 0xffff)))
        return out, 20 + 22
    if fmt == "xm":
        u16le(out, rel("xm_instruments"), rng.choice((0x7fff, 0xffff)))
        u16le(out, rel("xm_patterns"), rng.choice((0x7fff, 0xffff)))
        return out, rel("xm_instruments")
    if fmt == "s3m":
        u16le(out, rel("s3m_ins"), rng.choice((0x7fff, 0xffff)))
        u16le(out, rel("s3m_patterns"), rng.choice((0x7fff, 0xffff)))
        return out, rel("s3m_ins")
    if fmt == "it":
        u16le(out, rel("it_ins"), rng.choice((0x7fff, 0xffff)))
        u16le(out, rel("it_samples"), rng.choice((0x7fff, 0xffff)))
        u16le(out, rel("it_patterns"), rng.choice((0x7fff, 0xffff)))
        return out, rel("it_samples")
    return data, 0


def cycle_refs_case(data, fmt, rng, rel):
    out = bytearray(data)
    # All four formats have a byte order table at 0x60 except MOD (952).
    order_off = rel("order_table")
    cycle_len = min(64, len(out) - order_off)
    if cycle_len > 0:
        for i in range(cycle_len):
            out[order_off + i] = i % 3

    if fmt == "mod":
        out[950] = 128
        out[951] = 0
        # Set loop start beyond loop end for each MOD sample.
        for i in range(31):
            off = 20 + i * 30
            if off + 30 > len(out):
                break
            u16be(out, off + 26, 0x7fff)
            u16be(out, off + 28, 0x0001)
        return out, order_off
    if fmt == "xm":
        u16le(out, rel("order_len"), 64)
        u16le(out, rel("restart"), 0)
        return out, order_off
    if fmt == "s3m":
        u16le(out, rel("s3m_orders"), 64)
        return out, order_off
    if fmt == "it":
        u16le(out, rel("order_len"), 64)
        u16le(out, rel("restart"), 0)
        return out, order_off
    return data, order_off


def locate(fmt, data):
    off = {"order_table": 0x60}
    if fmt == "mod":
        off["order_table"] = 952
    elif fmt == "xm":
        off.update({
            "order_table": 100,
            "order_len": 84,
            "restart": 86,
            "xm_header_size": 80,
            "xm_patterns": 90,
            "xm_instruments": 92,
        })
    elif fmt == "s3m":
        off.update({
            "order_table": 0x60,
            "s3m_orders": 0x20,
            "s3m_ins": 0x22,
            "s3m_patterns": 0x24,
        })
    elif fmt == "it":
        off.update({
            "order_table": 0x60,
            "order_len": 0x20,
            "restart": 0x3e,
            "it_ins": 0x22,
            "it_samples": 0x24,
            "it_patterns": 0x26,
        })
    return lambda name: off[name]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--out", default="artifacts/property/corpus")
    ap.add_argument("--count", type=int, default=200)
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    out_dir = os.path.join(root, args.out)
    os.makedirs(out_dir, exist_ok=True)

    rng = random.Random(SEED)
    cache = {}
    manifest = []

    for i in range(args.count):
        cls = CLASSES[i % len(CLASSES)]
        fmt = FORMATS[(i + i // len(CLASSES)) % len(FORMATS)]
        rel = locate(fmt, b"")
        base_path = os.path.join(root, BASES[fmt][(i // len(CLASSES)) % len(BASES[fmt])])
        if base_path not in cache:
            with open(base_path, "rb") as f:
                cache[base_path] = f.read()
        data = cache[base_path]

        if cls == "truncate":
            mutated, offset = truncate_case(data, rng)
        elif cls == "bad_pattern":
            mutated, offset = bad_pattern_case(data, fmt, rng, rel)
        elif cls == "huge_counts":
            mutated, offset = huge_counts_case(data, fmt, rng, rel)
        else:
            mutated, offset = cycle_refs_case(data, fmt, rng, rel)

        name = f"{i:03d}_{cls}_{fmt}.bin"
        path = os.path.join(out_dir, name)
        with open(path, "wb") as f:
            f.write(mutated)

        rec = {
            "id": i,
            "file": os.path.relpath(path, root),
            "class": cls,
            "format": fmt,
            "source": os.path.relpath(base_path, root),
            "source_size": len(data),
            "size": len(mutated),
            "offset": offset,
            "sha256": hashlib.sha256(mutated).hexdigest(),
        }
        manifest.append(rec)

    manifest_path = os.path.join(out_dir, "manifest.jsonl")
    with open(manifest_path, "w", encoding="utf-8") as f:
        for rec in manifest:
            f.write(json.dumps(rec, sort_keys=True) + "\n")

    print(f"generated {len(manifest)} cases in {out_dir}")
    for cls in CLASSES:
        print(f"  {cls}: {sum(r['class'] == cls for r in manifest)}")


if __name__ == "__main__":
    main()

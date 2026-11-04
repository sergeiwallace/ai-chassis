#!/usr/bin/env python3
"""Deduplicate video frames by perceptual hash distance.

Keep a frame only when it differs from the last kept frame by at least a threshold. This
filters a grid sampled at a fixed interval (e.g. 10 seconds) down to frames that represent
meaningful screen changes.

The 2026-07-30 corpus demonstrated that phash distances during continuous console work are
unimodal (median 68 of a possible 256 at hash_size=16). There is no bimodal signal to
threshold against, so pixel-based deduplication cannot reliably detect "the screen changed"
during that kind of work. Frame selection anchored on transcript screen-share cues is more
reliable. This tool remains useful for static presentations where transitions are clean.

Usage:
    python frame_dedup.py GRID_DIR THRESHOLD [--interval 10] [--crop-fraction 0.865] [--hash-size 16]

Where GRID_DIR holds frame files named g_*.jpg sampled at a fixed interval.
"""

import argparse
import pathlib


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("grid_dir", help="directory of frames named g_*.jpg")
    ap.add_argument("threshold", type=int, help="minimum phash distance to keep a frame")
    ap.add_argument(
        "--interval",
        type=int,
        default=10,
        help="seconds between sampled frames (default: 10). Used only for timestamp derivation.",
    )
    ap.add_argument(
        "--crop-fraction",
        type=float,
        default=0.865,
        help="keep this fraction of the frame width, cropping from the right (default: 0.865). "
        "The 2026-07-30 corpus had webcam in the right 13.5 percent whose movement dominated "
        "the hash; cropping it out made deduplication work.",
    )
    ap.add_argument(
        "--hash-size",
        type=int,
        default=16,
        help="perceptual hash grid dimension (default: 16). Max distance is hash_size squared.",
    )
    args = ap.parse_args()

    # Imported here so --help works without the dependency installed.
    import imagehash
    from PIL import Image

    grid = pathlib.Path(args.grid_dir)
    if not grid.is_dir():
        ap.error(f"grid_dir does not exist or is not a directory: {grid}")

    files = sorted(grid.glob("g_*.jpg"))
    if not files:
        print(f"No g_*.jpg files found in {grid}")
        return

    kept = []
    prev_hash = None
    for i, frame_path in enumerate(files):
        timestamp = i * args.interval
        im = Image.open(frame_path)
        width, height = im.size
        im = im.crop((0, 0, int(width * args.crop_fraction), height))
        current_hash = imagehash.phash(im, hash_size=args.hash_size)
        dist = None if prev_hash is None else current_hash - prev_hash
        if prev_hash is None or dist >= args.threshold:
            kept.append((timestamp, frame_path.name, dist))
            prev_hash = current_hash

    print(f"candidates={len(files)} kept={len(kept)} thr={args.threshold}")
    for t, name, dist in kept:
        print(f"{t // 60:02d}:{t % 60:02d}\t{t}\t{name}\tdist={dist}")


if __name__ == "__main__":
    main()

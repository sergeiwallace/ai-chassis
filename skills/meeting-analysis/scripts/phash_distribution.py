#!/usr/bin/env python3
"""Compute perceptual hash distance distribution to calibrate frame deduplication thresholds.

This is a calibration tool, not a production step. It compares hash sizes 8 and 16, prints
the distribution of consecutive-frame distances (min, median, max, deciles), and for a set
of candidate thresholds shows how many frames each would keep. Its purpose is to let a human
pick a threshold for frame_dedup.py.

The 2026-07-30 corpus finding: distances at hash_size=16 were unimodal (median 68 of a
possible 256), meaning there is no bimodal "the screen changed" signal to threshold against
during continuous console work. Pixel-based frame selection cannot work there. That negative
result is why the workflow anchors frame selection on transcript screen-share cues instead.

Usage:
    python phash_distribution.py GRID_DIR [--crop-fraction 0.865] [--thresholds 40,50,60,70,80]
"""

import argparse
import pathlib
import statistics


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("grid_dir", help="directory of frames named g_*.jpg")
    ap.add_argument(
        "--crop-fraction",
        type=float,
        default=0.865,
        help="keep this fraction of width, cropping from the right (default: 0.865)",
    )
    ap.add_argument(
        "--thresholds-8",
        type=lambda s: [int(x) for x in s.split(",")],
        default=[6, 8, 10, 12, 14],
        help="comma-separated threshold values to test at hash_size=8 (default: 6,8,10,12,14)",
    )
    ap.add_argument(
        "--thresholds-16",
        type=lambda s: [int(x) for x in s.split(",")],
        default=[40, 50, 60, 70, 80],
        help="comma-separated threshold values to test at hash_size=16 (default: 40,50,60,70,80)",
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

    for hash_size in (8, 16):
        distances = []
        prev_hash = None
        for frame_path in files:
            im = Image.open(frame_path)
            width, height = im.size
            cropped = im.crop((0, 0, int(width * args.crop_fraction), height))
            current_hash = imagehash.phash(cropped, hash_size=hash_size)
            if prev_hash is not None:
                distances.append(current_hash - prev_hash)
            prev_hash = current_hash

        if not distances:
            print(f"hash_size={hash_size}: insufficient frames for distance computation")
            continue

        max_dist = hash_size * hash_size
        deciles = statistics.quantiles(distances, n=10)
        print(
            f"hash_size={hash_size} maxdist={max_dist} min={min(distances)} "
            f"med={statistics.median(distances)} max={max(distances)}"
        )
        print("  deciles:", [round(q, 1) for q in deciles])

        thresholds = args.thresholds_8 if hash_size == 8 else args.thresholds_16
        for thr in thresholds:
            kept_count = sum(1 for d in distances if d >= thr) + 1
            print(f"    thr={thr} -> would keep ~{kept_count}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Show vendor and local-ASR text side by side wherever a suspect term appears.

Follow-up to transcript_diff.py. That script tells you WHICH terms are suspect; this one shows
them in context from both sources at once, which is what actually settles a mishearing. Reading
"quill mart" next to the ASR's "quill mark layer" in the same 24-second window is the evidence.

The terms are supplied per meeting, because they come out of the previous step. Deduplicates to
one hit per term per 30-second bucket, so a term repeated in a single exchange does not flood the
output.

Usage:
    python probe_terms.py TRANSCRIPT_VTT ASR_JSONL --terms "example product"
    python probe_terms.py TRANSCRIPT_VTT ASR_JSONL --terms "example product"

Terms are matched case-insensitively as substrings, so a trailing space is meaningful:
"cla " matches the acronym without matching "class".
"""

import argparse
import json

from transcript_diff import parse_vtt


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("vtt", help="vendor WebVTT transcript")
    ap.add_argument("asr_jsonl", help="local ASR output from local_asr.py")
    ap.add_argument("--terms", nargs="*", default=[], help="suspect terms to locate")
    ap.add_argument("--terms-file", help="file with one suspect term per line")
    ap.add_argument(
        "--window",
        type=int,
        default=12,
        help="seconds either side of the cue to pull ASR text from (default: 12). The ASR "
        "segments its own way, so a window is needed rather than an exact match.",
    )
    ap.add_argument(
        "--dedup-bucket",
        type=int,
        default=30,
        help="report each term at most once per this many seconds (default: 30)",
    )
    ap.add_argument("--context-chars", type=int, default=300, help="ASR excerpt length")
    args = ap.parse_args()

    terms = list(args.terms)
    if args.terms_file:
        with open(args.terms_file, encoding="utf-8") as fh:
            terms += [line.strip() for line in fh if line.strip()]
    if not terms:
        ap.error("supply at least one term via --terms or --terms-file")

    cues = parse_vtt(args.vtt)
    with open(args.asr_jsonl, encoding="utf-8") as fh:
        asr = [json.loads(line) for line in fh]

    def asr_at(when: float) -> str:
        """ASR text overlapping a window around `when`."""
        return " ".join(
            rec["text"] for rec in asr if rec["end"] >= when - args.window and rec["start"] <= when + args.window
        )

    seen: set[tuple[str, int]] = set()
    hits = 0
    for start, speaker, body in cues:
        lowered = body.lower()
        for term in terms:
            if term.lower() not in lowered:
                continue
            key = (term, int(start) // args.dedup_bucket)
            if key in seen:
                break
            seen.add(key)
            hits += 1
            print(f"[{int(start) // 60:02d}:{int(start) % 60:02d}] TERM={term!r} spk={speaker}")
            print(f"   VENDOR: {body}")
            print(f"   ASR   : {asr_at(start)[: args.context_chars]}")
            print()
            break

    print(f"{hits} deduplicated hit(s) across {len(terms)} term(s)")


if __name__ == "__main__":
    main()

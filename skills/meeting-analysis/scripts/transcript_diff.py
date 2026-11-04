#!/usr/bin/env python3
"""Diff a vendor VTT transcript against a local ASR pass to find mangled terms.

This is the step that earns the ASR pass. A vendor transcript is accurate on ordinary speech
and unreliable on product names, so the useful signal is not "which transcript is better" but
"which words does exactly one source contain." Those are the candidate mishearings.

Worked example from the 2026-07-30 corpus: the vendor rendered a product called Quillmark as
"quill mart", "kill mark", "quill park", and "will mark" across 20+ occurrences, none of which
reads as a product name. The local pass heard "quill mark", which identified the tool behind
every variant. Only
four ASR-only terms cleared frequency 3 on that corpus, and one of them was worth the whole run.

Three reports, in decreasing usefulness:
  1. ASR-only terms   - candidate vendor mishearings. Start here.
  2. vendor-only terms - usually correct vendor wins; occasionally an ASR miss.
  3. low-confidence ASR words - spots where BOTH sources are unreliable, so a frame or the
     recording is the only way to settle them.

Usage:
    python transcript_diff.py TRANSCRIPT_VTT ASR_JSONL [--min-freq 3] [--top 45]
"""

import argparse
import collections
import json
import re

CUE_TIME = re.compile(r"(\d\d):(\d\d):(\d\d)\.(\d+) --> ")
SPEAKER = re.compile(r"<v ([^>]+)>")
VOICE_TAG = re.compile(r"</?v[^>]*>")
WORD = re.compile(r"[a-z0-9']+")


def parse_vtt(path: str) -> list[tuple[float, str, str]]:
    """Return [(start_seconds, speaker, text)] from a WebVTT file.

    Cues are separated by blank lines. The speaker is a <v Name> tag when present. The body is
    everything after the timing line with voice tags stripped and newlines flattened, so a
    multi-line cue becomes one utterance.
    """
    with open(path, encoding="utf-8") as fh:
        blocks = fh.read().split("\n\n")

    cues: list[tuple[float, str, str]] = []
    for block in blocks:
        match = CUE_TIME.search(block)
        if not match:
            continue
        hours, minutes, seconds, millis = match.groups()
        start = int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000
        speaker_match = SPEAKER.search(block)
        body = VOICE_TAG.sub("", block[block.index("-->") :])
        body = re.sub(r"^[^\n]*\n", "", body).replace("\n", " ").strip()
        if body:
            cues.append((start, speaker_match.group(1) if speaker_match else "?", body))
    return cues


def words(text: str) -> list[str]:
    """Lowercase word tokens, apostrophes kept so contractions stay one token."""
    return WORD.findall(text.lower())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("vtt", help="vendor WebVTT transcript")
    ap.add_argument("asr_jsonl", help="local ASR output from local_asr.py")
    ap.add_argument(
        "--min-freq",
        type=int,
        default=3,
        help="only report terms appearing at least this often (default: 3). Lower it to see "
        "more candidates at the cost of noise from one-off mishearings.",
    )
    ap.add_argument("--top", type=int, default=45, help="max terms per report (default: 45)")
    ap.add_argument(
        "--confidence-floor",
        type=float,
        default=0.35,
        help="ASR word probability below which a word is reported as unreliable (default: 0.35)",
    )
    args = ap.parse_args()

    cues = parse_vtt(args.vtt)
    with open(args.asr_jsonl, encoding="utf-8") as fh:
        asr = [json.loads(line) for line in fh]

    vendor_vocab = collections.Counter(w for _, _, body in cues for w in words(body))
    asr_vocab = collections.Counter(w for rec in asr for w in words(rec["text"]))

    print(f"vendor cues={len(cues)} asr segments={len(asr)}")
    print()

    print(f"=== ASR-only terms (freq>={args.min_freq}), candidate vendor mishearings ===")
    asr_only = [(w, c) for w, c in asr_vocab.items() if c >= args.min_freq and vendor_vocab.get(w, 0) == 0]
    print(sorted(asr_only, key=lambda x: -x[1])[: args.top])
    print()

    print(f"=== vendor-only terms (freq>={args.min_freq}) ===")
    vendor_only = [(w, c) for w, c in vendor_vocab.items() if c >= args.min_freq and asr_vocab.get(w, 0) == 0]
    print(sorted(vendor_only, key=lambda x: -x[1])[: args.top])
    print()

    # Low ASR confidence marks where BOTH sources are shaky: the vendor may also be wrong there,
    # and no amount of text comparison settles it. These are the timestamps to sample a frame at.
    low = [
        (w["s"], w["w"].strip(), w["p"])
        for rec in asr
        for w in rec["words"]
        if w["p"] < args.confidence_floor and len(w["w"].strip()) > 3
    ]
    print(f"=== ASR low-confidence words (p<{args.confidence_floor}): {len(low)} ===")
    for start, word, prob in low[: args.top]:
        print(f"  {int(start) // 60:02d}:{int(start) % 60:02d} {word!r} p={prob}")


if __name__ == "__main__":
    main()

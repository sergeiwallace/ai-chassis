#!/usr/bin/env python3
"""Run a local ASR pass over meeting audio, emitting word-level JSONL.

This exists to CHECK a vendor transcript, not to replace one. Vendor transcripts are
substantially accurate on ordinary speech and mangle product names; diffing an independent
pass against them is what finds the mangled terms (see transcript_diff.py).

Measured on a 49-minute recording: about 2.7x realtime on 4 CPU threads with small.en/int8,
941 segments. Output is flushed per segment so a long run can be watched, and so a crash
leaves partial results usable.

Usage:
    python local_asr.py AUDIO_WAV OUT_JSONL [--model small.en] [--threads 4]

Extract the audio first (16kHz mono is plenty for ASR and far smaller than the source):
    ffmpeg -i recording.mp4 -ac 1 -ar 16000 -vn audio.wav
"""

import argparse
import json
import time


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("audio", help="input audio file (wav)")
    ap.add_argument("out", help="output JSONL path")
    ap.add_argument("--model", default="small.en", help="faster-whisper model (default: small.en)")
    ap.add_argument("--threads", type=int, default=4, help="CPU threads (default: 4)")
    ap.add_argument("--progress-every", type=int, default=50, help="log every N segments")
    args = ap.parse_args()

    # Imported here so --help works without the heavy dependency installed.
    from faster_whisper import WhisperModel

    started = time.time()
    model = WhisperModel(args.model, device="cpu", compute_type="int8", cpu_threads=args.threads)

    # condition_on_previous_text=False keeps one bad segment from poisoning those after it,
    # which matters when the audio has crosstalk. vad_filter drops silence.
    segments, _info = model.transcribe(
        args.audio,
        beam_size=1,
        vad_filter=True,
        word_timestamps=True,
        condition_on_previous_text=False,
    )

    count = 0
    with open(args.out, "w", encoding="utf-8") as fh:
        for seg in segments:
            record = {
                "start": round(seg.start, 2),
                "end": round(seg.end, 2),
                "text": seg.text.strip(),
                "avg_logprob": round(seg.avg_logprob, 3),
                "no_speech_prob": round(seg.no_speech_prob, 3),
                "words": [
                    {
                        "w": w.word,
                        "s": round(w.start, 2),
                        "e": round(w.end, 2),
                        "p": round(w.probability, 3),
                    }
                    for w in (seg.words or [])
                ],
            }
            fh.write(json.dumps(record) + "\n")
            fh.flush()
            count += 1
            if count % args.progress_every == 0:
                print(
                    f"{count} segs, t={record['end']:.0f}s, elapsed={time.time() - started:.0f}s",
                    flush=True,
                )

    print(f"DONE segs={count} elapsed={time.time() - started:.0f}s")


if __name__ == "__main__":
    main()

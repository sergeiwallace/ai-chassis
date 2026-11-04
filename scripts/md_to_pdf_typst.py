#!/usr/bin/env python3
"""Convert a Markdown doc to PDF via pandoc -> Typst -> pikepdf, with no system PDF engine.

Route (why this shape): pandoc has no PDF engine on this host and conda-forge is
unreachable through the corporate TLS interception, but pandoc's Typst writer plus the
`typst` PyPI package (typst-py, which bundles the compiler) covers the whole path, and
pikepdf recompresses the result under common large-file commit gates.

Canonical invocation (deps are ephemeral; nothing needs a persistent install):

    uv run --with typst --with pikepdf python scripts/md_to_pdf_typst.py \
        docs/research/claude-desktop-app-frontend-and-cloud-hosting.md \
        docs/research/claude-desktop-app-frontend-and-cloud-hosting.pdf

Steps, all automated (nothing about the shipped research PDF was hand-tuned):
  1. `pandoc <in.md> -f gfm -t typst -s -o <tmp.typ>`
  2. Drop the body-level `= <title>` heading line: pandoc emits the doc title twice
     (template title block + the Markdown H1), which double-prints on page 1.
  3. Compile with typst-py.
  4. Recompress with pikepdf (object streams + flate) -- roughly a 60% size cut.

Output is REGENERABLE BUT NOT BYTE-IDENTICAL: Typst embeds a creation timestamp and
pikepdf writes a fresh document ID, so regeneration produces an equivalent PDF (same
page count and content), not the same bytes.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


def convert(md_path: Path, pdf_path: Path) -> int:
    import pikepdf
    import typst

    with tempfile.TemporaryDirectory() as tmp:
        typ_path = Path(tmp) / "doc.typ"
        subprocess.run(
            [
                "pandoc",
                str(md_path),
                "-f",
                "gfm",
                "-t",
                "typst",
                "-s",
                "-o",
                str(typ_path),
            ],
            check=True,
        )

        # Drop the duplicated body-level H1 (see module docstring, step 2).
        lines = typ_path.read_text().split("\n")
        typ_path.write_text("\n".join(ln for ln in lines if not ln.startswith("= ")))

        raw_pdf = Path(tmp) / "doc-raw.pdf"
        typst.compile(str(typ_path), output=str(raw_pdf))

        with pikepdf.open(raw_pdf) as pdf:
            n_pages = len(pdf.pages)
            pdf.save(
                str(pdf_path),
                compress_streams=True,
                recompress_flate=True,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
            )
    return n_pages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("markdown", type=Path, help="input Markdown file")
    parser.add_argument("pdf", type=Path, help="output PDF path")
    args = parser.parse_args()

    if not args.markdown.is_file():
        print(f"ERROR: no such file: {args.markdown}", file=sys.stderr)
        return 1
    n_pages = convert(args.markdown, args.pdf)
    size = args.pdf.stat().st_size
    print(f"Wrote {args.pdf} ({n_pages} pages, {size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

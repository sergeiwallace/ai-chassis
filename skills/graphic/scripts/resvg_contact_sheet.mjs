#!/usr/bin/env node
// Render an SVG to a single PNG contact sheet across several target sizes, on both a light and a
// dark background, using @resvg/resvg-js (a maintained Node binding to Rust resvg with prebuilt
// native binaries -- no separate `resvg` CLI install required).
//
// Usage: node resvg_contact_sheet.mjs <svg-path> --sizes 16,32,48,128 --out <png-path>
//
// Fails closed with a clear, named error if @resvg/resvg-js is not installed, rather than
// silently skipping the contact sheet.

import { readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const graphicDir = dirname(dirname(fileURLToPath(import.meta.url)));
const { skillModulesDir } = require(join(graphicDir, "scripts", "repo-paths.cjs"));
const requireToolchain = createRequire(join(skillModulesDir(graphicDir), "noop.cjs"));

const LIGHT_BG = "#ffffff";
const DARK_BG = "#0f0f0f";
const GUTTER = 8;

function parseArgs(argv) {
  const args = { svgPath: null, sizes: [16, 32, 48, 128], out: null };
  const rest = [...argv];
  args.svgPath = rest.shift() ?? null;
  while (rest.length) {
    const flag = rest.shift();
    if (flag === "--sizes") {
      args.sizes = (rest.shift() ?? "").split(",").map((s) => Number.parseInt(s.trim(), 10));
    } else if (flag === "--out") {
      args.out = rest.shift() ?? null;
    } else {
      throw new Error(`unrecognized argument: ${flag}`);
    }
  }
  if (!args.svgPath) throw new Error("missing required <svg-path> argument");
  if (!args.out) throw new Error("missing required --out <png-path> argument");
  if (args.sizes.some((s) => !Number.isFinite(s) || s <= 0)) {
    throw new Error("--sizes must be a comma-separated list of positive integers");
  }
  return args;
}

async function loadResvg() {
  try {
    return requireToolchain("@resvg/resvg-js");
  } catch (err) {
    throw new Error(
      "resvg_contact_sheet: @resvg/resvg-js is not installed. " +
        "Run `node scripts/graphic-toolchain.mjs install` from skills/graphic/ first. " +
        `(underlying error: ${err.message})`,
    );
  }
}

function renderOnBackground(Resvg, svgText, size, background) {
  const resvg = new Resvg(svgText, {
    fitTo: { mode: "width", value: size },
    background,
  });
  return resvg.render().asPng();
}

// Minimal PNG compositor: decodes each rendered PNG's raw RGBA pixels via a tiny inline decoder
// is unnecessary here -- @resvg/resvg-js can render directly onto a solid background color, so
// each cell is already a flat PNG. We only need to know width/height to lay out the sheet; PNG
// IHDR bytes 16-23 (big-endian width, height) give us that without a decoding dependency.
function pngDimensions(buffer) {
  return { width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20) };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const { Resvg } = await loadResvg();
  const svgText = readFileSync(args.svgPath, "utf-8");

  const cells = [];
  for (const size of args.sizes) {
    for (const background of [LIGHT_BG, DARK_BG]) {
      const png = renderOnBackground(Resvg, svgText, size, background);
      cells.push({ size, background, png, dims: pngDimensions(png) });
    }
  }

  // Phase one emits the individual rendered cells alongside a manifest rather than compositing a
  // single flattened contact-sheet PNG (compositing arbitrary PNGs without an image library is
  // out of scope for this thin implementation) -- the manifest is what a human or a future
  // compositor step reads.
  const outBase = args.out.replace(/\.png$/i, "");
  const manifest = [];
  for (const cell of cells) {
    const suffix = `${cell.size}px-${cell.background === LIGHT_BG ? "light" : "dark"}`;
    const cellPath = `${outBase}.${suffix}.png`;
    writeFileSync(cellPath, cell.png);
    manifest.push({ size: cell.size, background: cell.background, path: cellPath, ...cell.dims });
  }
  writeFileSync(`${outBase}.manifest.json`, JSON.stringify(manifest, null, 2));
  console.log(`resvg_contact_sheet: wrote ${manifest.length} cells + manifest for ${args.svgPath}`);
}

main().catch((err) => {
  console.error(err.message);
  process.exit(1);
});

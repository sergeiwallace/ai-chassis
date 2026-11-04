// Render what macOS actually DRAWS for a path (NSWorkspace.icon(forFile:)) to a PNG.
//
//   swift render_app_icon.swift <path> <out.png> [<size-pt>]      size defaults to 512
//
// The size argument exists so a TILE-SIZE render is one command: fine detail is illegible at Dock
// size and hue is the only reliable signal there, so judging an icon at 512 and shipping it for a
// 64px tile is how an indistinguishable tile gets approved.
//
// THE SIZE IS IN POINTS, NOT PIXELS. lockFocus draws into a backing store at the display's scale,
// so on a Retina screen the written PNG is 2x the number given: `... out.png 64` produced a
// 128x128 file (measured, example-client-host-mac 2026-10-02). That is harmless for a hue measurement and for
// generate_app_icon.py (which resizes to its own 1024 tile either way), but do not read the
// output's pixel dimensions back as a confirmation of what you asked for.
// `qlmanage` cannot thumbnail .app bundles, which is why this exists at all.
import Cocoa
let size = CGFloat(CommandLine.arguments.count > 3 ? (Double(CommandLine.arguments[3]) ?? 512) : 512)
let path = CommandLine.arguments[1]
let out = CommandLine.arguments[2]
let icon = NSWorkspace.shared.icon(forFile: path)
let target = NSImage(size: NSSize(width: size, height: size))
target.lockFocus()
NSColor.clear.set()
NSRect(x: 0, y: 0, width: size, height: size).fill()
icon.draw(in: NSRect(x: 0, y: 0, width: size, height: size), from: .zero, operation: .sourceOver, fraction: 1.0)
target.unlockFocus()
if let tiff = target.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
   let png = rep.representation(using: .png, properties: [:]) {
    try? png.write(to: URL(fileURLWithPath: out))
    print("wrote", out)
}

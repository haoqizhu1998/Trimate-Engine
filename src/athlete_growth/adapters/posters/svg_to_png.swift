import AppKit
import Foundation

guard CommandLine.arguments.count == 3,
      let image = NSImage(contentsOfFile: CommandLine.arguments[1]),
      let bitmap = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: 1024,
        pixelsHigh: 1536,
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .deviceRGB,
        bytesPerRow: 0,
        bitsPerPixel: 0
      ),
      let context = NSGraphicsContext(bitmapImageRep: bitmap) else {
    fputs("SVG render input is invalid\n", stderr)
    exit(2)
}

NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = context
context.imageInterpolation = .high
image.draw(
    in: NSRect(x: 0, y: 0, width: 1024, height: 1536),
    from: .zero,
    operation: .copy,
    fraction: 1.0
)
context.flushGraphics()
NSGraphicsContext.restoreGraphicsState()

guard let png = bitmap.representation(using: .png, properties: [:]) else {
    fputs("PNG encoding failed\n", stderr)
    exit(3)
}
do {
    try png.write(to: URL(fileURLWithPath: CommandLine.arguments[2]))
} catch {
    fputs("PNG write failed\n", stderr)
    exit(4)
}

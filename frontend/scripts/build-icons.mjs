import { readFile, writeFile } from "node:fs/promises";
import { deflateSync } from "node:zlib";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(fileURLToPath(new URL("../..", import.meta.url)));
const brandDir = resolve(root, "src/primary_signal/web/public/static/brand");
const source = await readFile(resolve(brandDir, "favicon-light.svg"), "utf8");
const colours = [...source.matchAll(/<rect\b[^>]*fill="(#[0-9A-Fa-f]{6})"/g)]
  .map(([, hex]) => [1, 3, 5].map((index) => Number.parseInt(hex.slice(index, index + 2), 16)));
if (colours.length !== 9) throw new Error("The favicon SVG must contain nine coloured pixels.");

const layouts = new Map([
  [16, { cell: 4, gap: 1, margin: 1 }],
  [32, { cell: 8, gap: 3, margin: 1 }],
  [48, { cell: 13, gap: 4, margin: 0 }],
  [180, { cell: 50, gap: 15, margin: 0 }],
]);

function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function chunk(name, data) {
  const type = Buffer.from(name);
  const header = Buffer.alloc(4);
  header.writeUInt32BE(data.length);
  const checksum = Buffer.alloc(4);
  checksum.writeUInt32BE(crc32(Buffer.concat([type, data])));
  return Buffer.concat([header, type, data, checksum]);
}

function render(size, { cell, gap, margin }) {
  const scanlines = Buffer.alloc(size * (1 + size * 4));
  // A solid paper ground keeps the fallback legible on dark browser chrome.
  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      const offset = y * (1 + size * 4) + 1 + x * 4;
      scanlines.set([247, 248, 246, 255], offset);
    }
  }
  for (let row = 0; row < 3; row += 1) {
    for (let col = 0; col < 3; col += 1) {
      const [red, green, blue] = colours[row * 3 + col];
      for (let y = margin + row * (cell + gap); y < margin + row * (cell + gap) + cell; y += 1) {
        for (let x = margin + col * (cell + gap); x < margin + col * (cell + gap) + cell; x += 1) {
          const offset = y * (1 + size * 4) + 1 + x * 4;
          scanlines[offset] = red;
          scanlines[offset + 1] = green;
          scanlines[offset + 2] = blue;
          scanlines[offset + 3] = 255;
        }
      }
    }
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; // RGBA, 8 bits per channel.
  ihdr[9] = 6;
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(scanlines, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

for (const [size, geometry] of layouts) {
  const name = size === 180 ? "apple-touch-icon.png" : `favicon-${size}.png`;
  await writeFile(resolve(brandDir, name), render(size, geometry));
}

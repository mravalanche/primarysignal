import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import { inflateSync } from "node:zlib";

const layout = await readFile("src/primary_signal/web/public/templates/layouts/public.html", "utf8");
const brand = await readFile("src/primary_signal/web/public/templates/components/brand.html", "utf8");
const styles = await readFile("frontend/styles/public.css", "utf8");
const entranceScript = layout.match(/<script>\s*([\s\S]*?primary-signal-brand-last-seen[\s\S]*?)<\/script>/)?.[1];

function visit(storage, now, options = {}) {
  const { reducedMotion = false, storageFails = false, sessionFails = false, session = new Map(), path = "/" } = options;
  const classes = new Set();
  let clickListener;
  let readyListener;
  const localStorage = {
    getItem(key) {
      if (storageFails) throw new Error("storage unavailable");
      return storage.get(key) ?? null;
    },
    setItem(key, value) {
      if (storageFails) throw new Error("storage unavailable");
      storage.set(key, value);
    },
  };
  const sessionStorage = {
    getItem(key) {
      if (sessionFails) throw new Error("session storage unavailable");
      return session.get(key) ?? null;
    },
    setItem(key, value) {
      if (sessionFails) throw new Error("session storage unavailable");
      session.set(key, value);
    },
    removeItem(key) {
      if (sessionFails) throw new Error("session storage unavailable");
      session.delete(key);
    },
  };
  runInNewContext(entranceScript, {
    Date: { now: () => now },
    document: {
      documentElement: { classList: { add: (name) => classes.add(name) } },
      addEventListener: (name, callback) => { if (name === "DOMContentLoaded") readyListener = callback; },
      querySelector: (name) => name === ".ps-brand"
        ? { addEventListener: (event, callback) => { if (event === "click") clickListener = callback; } }
        : null,
    },
    localStorage,
    sessionStorage,
    location: { pathname: path },
    matchMedia: () => ({ matches: !reducedMotion }),
  });
  readyListener?.();
  return {
    animated: classes.has("ps-brand-entrance"),
    click: (event = {}) => clickListener?.({ button: 0, currentTarget: { pathname: "/" }, ...event }),
  };
}

test("the mark enters once per 30-minute browsing visit across tabs", () => {
  assert.ok(entranceScript);
  const storage = new Map();
  assert.equal(visit(storage, 1_000_000).animated, true);
  assert.equal(visit(storage, 1_010_000).animated, false);
  assert.equal(visit(storage, 1_010_000 + 29 * 60_000).animated, false);
  assert.equal(visit(storage, 1_010_000 + 60 * 60_000).animated, true);
});

test("a plain masthead logo click replays once on home arrival", () => {
  const storage = new Map();
  const session = new Map();
  visit(storage, 1_000_000, { session });
  const current = visit(storage, 1_010_000, { session });
  assert.equal(current.animated, false);
  current.click();
  assert.equal(visit(storage, 1_010_100, { session }).animated, true);
  assert.equal(visit(storage, 1_010_200, { session }).animated, false);

  const modified = visit(storage, 1_020_000, { session });
  modified.click({ ctrlKey: true });
  assert.equal(visit(storage, 1_020_100, { session }).animated, false);

  current.click();
  assert.equal(visit(storage, 1_020_200, { session, path: "/progress" }).animated, false);
  assert.equal(visit(storage, 1_020_300, { session }).animated, false);

  const prefixed = visit(storage, 1_030_000, { session, path: "/primarysignal/latest" });
  prefixed.click({ currentTarget: { pathname: "/primarysignal/" } });
  assert.equal(visit(storage, 1_030_100, { session, path: "/primarysignal/" }).animated, true);
  assert.equal(visit(storage, 1_030_200, { session, path: "/primarysignal/" }).animated, false);
});

test("the complete mark stays static when motion or storage is unavailable", () => {
  assert.equal(visit(new Map(), 1_000_000, { reducedMotion: true }).animated, false);
  assert.equal(visit(new Map(), 1_000_000, { storageFails: true }).animated, false);
  assert.equal(visit(new Map(), 1_000_000, { sessionFails: true }).animated, false);
  assert.match(styles, /\.ps-brand-entrance \.ps-brand-mark--entrance \.ps-pixel/);
});

test("all nine pixels have equal square geometry at native display sizes", () => {
  const rects = [...brand.matchAll(/<rect\b[^>]+>/g)].map(([tag]) => tag);
  assert.equal(rects.length, 9);
  for (const rect of rects) {
    assert.match(rect, /width="10" height="10"/);
  }
  assert.match(brand, /viewBox="0 0 36 36"/);
  assert.match(styles, /\.ps-brand-mark\s*\{[^}]*width: 36px;[^}]*height: 36px;[^}]*shape-rendering: crispEdges;/);
  assert.match(styles, /\.ps-source-loading \.ps-brand-mark \{ width: 36px; height: 36px; \}/);
});

test("PNG favicons preserve the nine equally sized pixels", async () => {
  const layouts = new Map([[16, [4, 1, 1]], [32, [8, 3, 1]], [48, [13, 4, 0]], [180, [50, 15, 0]]]);
  const expected = ["c8d3d4", "142b37", "126a85", "c8d3d4", "142b37", "126a85", "c8d3d4", "142b37", "c8d3d4"];
  for (const [size, [cell, gap, margin]] of layouts) {
    const name = size === 180 ? "apple-touch-icon.png" : `favicon-${size}.png`;
    const bytes = await readFile(`src/primary_signal/web/public/static/brand/${name}`);
    assert.deepEqual([...bytes.subarray(0, 8)], [137, 80, 78, 71, 13, 10, 26, 10]);
    assert.equal(bytes.readUInt32BE(16), size);
    assert.equal(bytes.readUInt32BE(20), size);
    // Generated files have a single IDAT chunk after the 25-byte IHDR chunk.
    const compressedSize = bytes.readUInt32BE(33);
    const pixels = inflateSync(bytes.subarray(41, 41 + compressedSize));
    const gapPixel = pixels.subarray(1 + (margin + cell) * 4, 1 + (margin + cell) * 4 + 4);
    assert.deepEqual([...gapPixel], [247, 248, 246, 255]);
    for (let row = 0; row < 3; row += 1) {
      for (let col = 0; col < 3; col += 1) {
        const colour = expected[row * 3 + col];
        for (let y = margin + row * (cell + gap); y < margin + row * (cell + gap) + cell; y += 1) {
          for (let x = margin + col * (cell + gap); x < margin + col * (cell + gap) + cell; x += 1) {
            const pixel = pixels.subarray(y * (1 + size * 4) + 1 + x * 4, y * (1 + size * 4) + 5 + x * 4);
            assert.equal(pixel.subarray(0, 3).toString("hex"), colour);
            assert.equal(pixel[3], 255);
          }
        }
      }
    }
  }
  for (const size of [16, 32, 48]) {
    assert.match(layout, new RegExp(`sizes="${size}x${size}"[^>]*favicon-${size}\\.png`));
  }
  assert.match(layout, /rel="apple-touch-icon" sizes="180x180"/);
});

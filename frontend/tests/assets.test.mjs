import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const generated = [
  "src/primary_signal/web/public/static/public.css",
  "src/primary_signal/web/admin/static/admin.css",
  "src/primary_signal/web/common/static/htmx.min.js",
];

test("generated assets are present and self-hosted", async () => {
  for (const path of generated) {
    const content = await readFile(path, "utf8");
    assert.ok(content.length > 500, `${path} is unexpectedly small`);
    assert.doesNotMatch(
      content,
      /(?:url\(|src=|href=)["']?https?:\/\//,
      `${path} contains a remote runtime asset URL`,
    );
  }
});

test("the public stylesheet contains the Primary Signal themes", async () => {
  const css = await readFile(generated[0], "utf8");
  assert.match(css, /primarysignal-light/);
  assert.match(css, /primarysignal-dark/);
  assert.match(css, /--ps-reading-measure/);
});

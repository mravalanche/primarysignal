import { readFile } from "node:fs/promises";

const generated = [
  "src/primary_signal/web/public/static/public.css",
  "src/primary_signal/web/admin/static/admin.css",
  "src/primary_signal/web/common/static/htmx.min.js",
];
const before = await Promise.all(generated.map((path) => readFile(path)));

await import("./build-assets.mjs");

const after = await Promise.all(generated.map((path) => readFile(path)));
if (before.some((content, index) => !content.equals(after[index]))) {
  throw new Error("Generated assets were stale. Run `npm run build` and stage the results.");
}

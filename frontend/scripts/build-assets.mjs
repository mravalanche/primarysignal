import { cp, mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import postcss from "postcss";
import tailwind from "@tailwindcss/postcss";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const htmx = resolve(root, "node_modules/htmx.org/dist/htmx.min.js");

const builds = [
  ["frontend/styles/public.css", "src/primary_signal/web/public/static/public.css"],
  ["frontend/styles/admin.css", "src/primary_signal/web/admin/static/admin.css"],
];

for (const [input, output] of builds) {
  const inputPath = resolve(root, input);
  const outputPath = resolve(root, output);
  await mkdir(dirname(outputPath), { recursive: true });
  const source = await readFile(inputPath, "utf8");
  const result = await postcss([tailwind({ base: root, optimize: { minify: true } })]).process(
    source,
    { from: inputPath, to: outputPath, map: false },
  );
  await writeFile(outputPath, result.css.replace(/\n$/, ""));
}

const commonStatic = resolve(root, "src/primary_signal/web/common/static");
await mkdir(commonStatic, { recursive: true });
await cp(htmx, resolve(commonStatic, "htmx.min.js"));

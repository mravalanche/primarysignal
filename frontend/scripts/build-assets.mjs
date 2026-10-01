import { cp, mkdir } from "node:fs/promises";
import { spawn } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const tailwind = resolve(root, "node_modules/@tailwindcss/cli/dist/index.mjs");
const htmx = resolve(root, "node_modules/htmx.org/dist/htmx.min.js");

const builds = [
  ["frontend/styles/public.css", "src/primary_signal/web/public/static/public.css"],
  ["frontend/styles/admin.css", "src/primary_signal/web/admin/static/admin.css"],
];

function run(command, args) {
  return new Promise((resolveRun, reject) => {
    const process = spawn(command, args, { cwd: root, stdio: "inherit" });
    process.on("error", reject);
    process.on("exit", (code) => {
      if (code === 0) resolveRun();
      else reject(new Error(`${command} exited with status ${code}`));
    });
  });
}

for (const [input, output] of builds) {
  const outputPath = resolve(root, output);
  await mkdir(dirname(outputPath), { recursive: true });
  await run(process.execPath, [tailwind, "-i", resolve(root, input), "-o", outputPath, "--minify"]);
}

const commonStatic = resolve(root, "src/primary_signal/web/common/static");
await mkdir(commonStatic, { recursive: true });
await cp(htmx, resolve(commonStatic, "htmx.min.js"));

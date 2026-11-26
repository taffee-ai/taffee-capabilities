// Fixture implementation (mechanism test only). Reads {"n": N} on stdin, prints a JSON result.
import { readFileSync } from "node:fs";

const data: { n: number } = JSON.parse(readFileSync(0, "utf8"));
console.log(`{"ok": true, "lang": "typescript", "n": ${data.n}, "double": ${data.n * 2}}`);

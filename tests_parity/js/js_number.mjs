// How JavaScript, and so Nightscout's own plugins, read treatment amounts.
//
// Input (stdin JSON): { raw: "<exact /api/v1/treatments.json response text>", fields: [...] }
// Output: [{ id, <field>: { truthy, number } }], with NaN and +/-Infinity reported as strings.
import { readFileSync } from "node:fs";

const input = JSON.parse(readFileSync(0, "utf8"));
const rows = JSON.parse(input.raw);
const encode = (n) => (Number.isFinite(n) ? n : String(n));
const out = rows.map((row) => {
  const result = { id: row._id };
  for (const field of input.fields) {
    const value = row[field];
    result[field] = { truthy: !!value, number: encode(Number(value)) };
  }
  return result;
});
process.stdout.write(JSON.stringify(out));

// Parses every service's AsyncAPI document with the pinned AsyncAPI parser:
// the document, every $ref it reaches (schemas and examples), and each
// payload schema must resolve and validate. Warnings fail too.
//
//   node validate.mjs                  every service
//   node validate.mjs graph_engine     one service
import { existsSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Parser, fromFile } from "@asyncapi/parser";

const contracts = dirname(dirname(fileURLToPath(import.meta.url)));
const only = process.argv[2];
const services = readdirSync(contracts)
  .filter((name) => existsSync(join(contracts, name, "v1", "asyncapi.yaml")))
  .filter((name) => !only || name === only)
  .sort();
const severity = ["error", "warning", "info", "hint"];
const parser = new Parser();

let failed = only && services.length === 0;
for (const service of services) {
  const path = join(contracts, service, "v1", "asyncapi.yaml");
  const { document, diagnostics } = await fromFile(parser, path).parse();
  const problems = diagnostics.filter((diagnostic) => diagnostic.severity <= 1);
  for (const diagnostic of diagnostics) {
    console.log(`${service}: ${severity[diagnostic.severity]} ${diagnostic.code}: ${diagnostic.message} at ${diagnostic.path.join(".")}`);
  }
  if (!document || problems.length) failed = true;
  console.log(`${service}: ${!document || problems.length ? "invalid" : "valid"}`);
}
process.exit(failed ? 1 : 0);

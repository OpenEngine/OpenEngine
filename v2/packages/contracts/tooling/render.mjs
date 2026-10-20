// Runs the pinned AsyncAPI CLI over every service contract.
//
//   node render.mjs validate   parse, resolve every $ref and lint each document
//   node render.mjs bundle     write each document with its references inlined to ../dist/{service}/asyncapi.yaml
//   node render.mjs docs       render each document to ../dist/{service}/index.html
//   node render.mjs preview    serve one live-reloading document: node render.mjs preview graph_engine
//
// Telemetry is off: the CLI reads its analytics settings from the checked-in
// asyncapi-analytics.json instead of creating one in the home directory.
import { spawnSync, spawn } from "node:child_process";
import { existsSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const tooling = dirname(fileURLToPath(import.meta.url));
const contracts = dirname(tooling);
const services = readdirSync(contracts).filter((name) => existsSync(join(contracts, name, "v1", "asyncapi.yaml"))).sort();
const cli = join(tooling, "node_modules", ".bin", "asyncapi");
const template = join(tooling, "node_modules", "@asyncapi", "html-template");
const env = {
  ...process.env,
  ASYNCAPI_METRICS_CONFIG_PATH: join(tooling, "asyncapi-analytics.json"),
  SUPPRESS_NO_CONFIG_WARNING: "true",
};

const documentOf = (service) => join(contracts, service, "v1", "asyncapi.yaml");
const outputOf = (service) => join(contracts, "dist", service);

function run(args) {
  const { status } = spawnSync(cli, args, { stdio: "inherit", env, cwd: tooling });
  return status === 0;
}

const commands = {
  validate: (service) => run(["validate", documentOf(service), "--fail-severity", "warn"]),
  bundle: (service) => run(["bundle", documentOf(service), "--output", join(outputOf(service), "asyncapi.yaml")]),
  docs: (service) =>
    run([
      "generate", "fromTemplate", documentOf(service), template,
      "--output", outputOf(service), "--force-write", "--param", "singleFile=true",
    ]),
};

const [command, only] = process.argv.slice(2);
if (command === "preview") {
  const service = only ?? "connection_bus";
  spawn(cli, ["start", "preview", documentOf(service)], { stdio: "inherit", env, cwd: tooling });
} else if (commands[command]) {
  const failed = services.filter((service) => (!only || only === service) && !commands[command](service));
  if (failed.length) {
    console.error(`${command} failed for: ${failed.join(", ")}`);
    process.exit(1);
  }
} else {
  console.error("usage: node render.mjs validate|bundle|docs|preview [service]");
  process.exit(2);
}

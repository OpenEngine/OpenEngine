import assert from "node:assert/strict";
import test from "node:test";
import { accountTokenChannels } from "./tenant-addresses.mjs";

const document = (...addresses) => ({
  channels: () => addresses.map((address) => ({ address: () => address })),
});

for (const service of ["connection_bus", "graph_engine", "tool_service", "virtual_control_plane"]) {
  test(`${service}: accepts tenant addresses`, () => {
    assert.deepEqual(accountTokenChannels(service, document("oe.v1.ge.query.graphs.list", null)), []);
  });
  for (const address of ["oe.v1.{account}.ge.query.graphs.list", "oe.v1.ge.events.{account}"]) {
    test(`${service}: rejects ${address}`, () => {
      const rejected = accountTokenChannels(service, document(address));
      assert.equal(rejected.length, 1);
      assert.equal(rejected[0].address(), address);
    });
  }
}

test("internal runtime is outside tenant-facing validation", () => {
  assert.deepEqual(accountTokenChannels("internal/workspace_runtime", document("oe.internal.v1.{account}")), []);
});

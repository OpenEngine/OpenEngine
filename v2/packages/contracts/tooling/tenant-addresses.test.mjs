import assert from "node:assert/strict";
import { test } from "node:test";
import { tenantAddressProblems } from "./tenant-addresses.mjs";

const channel = (address) => ({ id: () => "request", address: () => address });

for (const service of ["connection_bus", "graph_engine", "tool_service", "virtual_control_plane"]) {
  test(`${service} rejects a tenant-facing account token`, () => {
    assert.deepEqual(tenantAddressProblems(service, [channel("oe.v1.{account}.svc.query.get")]), [
      `${service}: error account-token: channel request address oe.v1.{account}.svc.query.get contains {account}`,
    ]);
  });

  test(`${service} preserves tenant-facing addresses`, () => {
    assert.deepEqual(tenantAddressProblems(service, [
      channel("oe.v1.svc.query.resources.{resource_id}.get"),
      channel(null),
    ]), []);
  });
}

test("internal workspace runtime is outside the tenant-facing rule", () => {
  assert.deepEqual(tenantAddressProblems("internal/workspace_runtime", [
    channel("oe.internal.v1.{account}.request"),
  ]), []);
});

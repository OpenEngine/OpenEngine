import assert from "node:assert/strict";
import test from "node:test";
import { hasAccountToken } from "./tenant-address.mjs";

for (const address of [
  "{account}",
  "{account}.oe.v1.cxn.control.connections.apply",
  "oe.v1.{account}.cxn.control.connections.apply",
  "oe.v1.cxn.control.{account}",
]) {
  test(`rejects account token: ${address}`, () => {
    assert.equal(hasAccountToken(address), true);
  });
}

for (const address of [
  null,
  undefined,
  "",
  "oe.v1.cxn.control.connections.apply",
  "oe.v1.tools.invoke.{namespace}.{name}",
  "oe.v1.cxn.query.{account_id}",
  "oe.v1.cxn.query.prefix{account}",
  "oe.v1.cxn.query.{account}suffix",
]) {
  test(`allows address without exact account token: ${address}`, () => {
    assert.equal(hasAccountToken(address), false);
  });
}

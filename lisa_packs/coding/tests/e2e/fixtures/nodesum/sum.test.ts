import test from "node:test";
import assert from "node:assert";
import { total } from "./sum.ts";

test("total adds the configured base value", () => {
    assert.equal(total(1), 42);
});

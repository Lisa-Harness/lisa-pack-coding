import test from "node:test";
import assert from "node:assert";
import { applyDiscount } from "./sum.ts";

test("acceptance: applies the 15% discount rounded to cents", () => {
    assert.equal(applyDiscount(100), 85);
    assert.equal(applyDiscount(19.99), 16.99);
    assert.equal(applyDiscount(0), 0);
});

test("acceptance: rejects negative amounts with a RangeError", () => {
    assert.throws(() => applyDiscount(-0.01), RangeError);
});

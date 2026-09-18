import assert from "node:assert/strict";
import test from "node:test";
import { csvAmountNotice, isLiquidBitcoin, liquidBitcoinAsset, outputValue } from "../src/scripts/amounts.ts";

test("L-BTC outputs use eight exact decimal places, including zero and one satoshi", () => {
  for (const [value, expected] of [
    [0, "0.00000000"], [1, "0.00000001"], [100000000, "1.00000000"],
    [123456789, "1.23456789"],
  ]) {
    assert.equal(outputValue({ value, asset: liquidBitcoinAsset }), expected);
  }
  assert.equal(isLiquidBitcoin(liquidBitcoinAsset.toUpperCase()), true);
  assert.equal(outputValue({ value: 1, asset: liquidBitcoinAsset.toUpperCase() }), "0.00000001");
});

test("exact text takes precedence over rounded JavaScript numbers", () => {
  const rounded = Number.MAX_SAFE_INTEGER + 1;
  assert.equal(outputValue({ value_text: "9007199254740993", value: rounded,
    asset: liquidBitcoinAsset }), "90071992.54740993");
  assert.equal(outputValue({ value: "9223372036854775807", asset: liquidBitcoinAsset }),
    "92233720368.54775807");
  assert.equal(outputValue({ value: rounded, asset: liquidBitcoinAsset }), "??");
});

test("other or confidential assets retain exact base units without being called L-BTC", () => {
  for (const asset of [undefined, "ab".repeat(32), "L-BTC"]) {
    assert.equal(outputValue({ value_text: "9223372036854775807", asset }),
      "9223372036854775807 base units");
    assert.equal(outputValue({ value: 0, asset }), "0 base units");
    assert.equal(isLiquidBitcoin(asset), false);
  }
});

test("unavailable or invalid amounts remain unknown", () => {
  for (const value of [undefined, null, -1, 1.5, Infinity, NaN, "-1", "1.5", "1e8", ""]) {
    assert.equal(outputValue({ value, asset: liquidBitcoinAsset }), "??");
  }
  assert.equal(outputValue({ value_text: "??", value: 1, asset: liquidBitcoinAsset }), "??");
});

test("saved legacy CSV exports are not labeled as converted amounts", () => {
  assert.match(csvAmountNotice("asset_dependent"), /whole units with 8 decimals/);
  for (const version of [undefined, "base_units", "unknown"]) {
    assert.match(csvAmountNotice(version), /Saved amounts are exact base units/);
    assert.match(csvAmountNotice(version), /create a new export/);
  }
  assert.match(csvAmountNotice("base_units", "plot starter connections again"), /plot starter connections again/);
});

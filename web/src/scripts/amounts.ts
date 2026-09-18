/** Format presentation amounts without rounding the stored integer evidence. */
export const liquidBitcoinAsset =
  "6f0279e9ed041c3d710a9f57d0c02928416460c4b722ae3457a11eec381c526d";

export const isLiquidBitcoin = (asset?: string): boolean =>
  asset?.toLowerCase() === liquidBitcoinAsset;

type AmountOutput = {
  value?: number | string | null;
  value_text?: string | null;
  asset?: string;
};

export function outputValue(output: AmountOutput): string {
  // The server supplies value_text because JSON numbers can exceed JS precision.
  const value = typeof output.value_text === "string"
    ? output.value_text
    : typeof output.value === "string"
      ? output.value
      : typeof output.value === "number" && Number.isSafeInteger(output.value)
        ? String(output.value)
        : "";
  if (!/^\d+$/.test(value)) return "??";
  if (!isLiquidBitcoin(output.asset)) return `${value} base units`;
  // Decimal placement uses only strings, including values larger than 2^53.
  const digits = value.replace(/^0+(?=\d)/, "").padStart(9, "0");
  return `${digits.slice(0, -8)}.${digits.slice(-8)}`;
}

export const csvAmountNotice = (valueUnits?: string, regenerate = "create a new export"): string =>
  valueUnits === "asset_dependent"
    ? "L-BTC and BTC amounts use whole units with 8 decimals; other or unidentified assets use base units"
    : `Saved amounts are exact base units; ${regenerate} to use L-BTC and BTC units`;

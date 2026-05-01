"use client";

import { useEffect, useState } from "react";

interface CurrencyInfo {
  code: string;
  symbol: string;
}

const DEFAULT: CurrencyInfo = { code: "EUR", symbol: "€" };

export function useCurrency(): CurrencyInfo {
  const [currency, setCurrency] = useState<CurrencyInfo>(DEFAULT);

  useEffect(() => {
    fetch("/api/currency")
      .then((r) => (r.ok ? r.json() : DEFAULT))
      .then(setCurrency)
      .catch(() => {});
  }, []);

  return currency;
}

/** Format a price-per-kWh value as "<symbol>X.Xc" (cents). */
export function formatPriceCents(
  pricePerKwh: number | null | undefined,
  symbol: string,
): string {
  if (pricePerKwh == null) return "—";
  return `${symbol}${(pricePerKwh * 100).toFixed(1)}c`;
}

/** Format an absolute cost as "<symbol>X.XX". */
export function formatCost(
  cost: number | null | undefined,
  symbol: string,
): string {
  if (cost == null) return `${symbol}0.00`;
  return `${symbol}${cost.toFixed(2)}`;
}

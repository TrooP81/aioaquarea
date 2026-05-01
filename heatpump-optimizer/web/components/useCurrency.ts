"use client";

import { useEffect, useState } from "react";

export interface CurrencyInfo {
  /** Currency code, e.g. "EUR", "SEK". */
  code: string;
  /** Placed before the number, e.g. "€", "£". May be empty. */
  prefix: string;
  /** Placed after the number, e.g. " kr", " zł". May be empty. */
  suffix: string;
  /**
   * Server-decided multiplier for per-kWh prices.
   * 100 for EUR/GBP/USD (show cents), 1 for SEK/NOK/DKK (show main unit).
   * The frontend NEVER decides this — the server does.
   */
  multiplier: number;
  /** Ready-to-use chart axis label, e.g. "€c/kWh" or "SEK/kWh". */
  priceLabel: string;
  /** True once the API response has been received. */
  loaded: boolean;
}

/**
 * Safe default: multiplier=1 so prices are NEVER accidentally ×100.
 * Before the API responds, formatters return "—".
 */
const LOADING: CurrencyInfo = {
  code: "",
  prefix: "",
  suffix: "",
  multiplier: 1,
  priceLabel: "/kWh",
  loaded: false,
};

function parseCurrencyResponse(data: any): CurrencyInfo {
  return {
    code: data.code ?? "EUR",
    prefix: data.prefix ?? "",
    suffix: data.suffix ?? "",
    multiplier: data.multiplier ?? 1,
    priceLabel: data.price_label ?? "/kWh",
    loaded: true,
  };
}

/**
 * Fetches currency config from the API on every mount.
 *
 * No module-level caching — this guarantees the component always
 * reflects the current setting, even right after changing currency
 * in the settings page and navigating back to the dashboard.
 */
export function useCurrency(): CurrencyInfo {
  const [currency, setCurrency] = useState<CurrencyInfo>(LOADING);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/currency")
      .then((r) => {
        if (!r.ok) throw new Error("not ok");
        return r.json();
      })
      .then((data) => {
        if (!cancelled) setCurrency(parseCurrencyResponse(data));
      })
      .catch(() => {
        // Fallback: multiplier stays 1 (safe — never inflates prices)
        if (!cancelled) setCurrency({ ...LOADING, loaded: true });
      });
    return () => { cancelled = true; };
  }, []);

  return currency;
}

/**
 * Format a per-kWh price for display.
 * Uses the server-provided multiplier — no client-side ×100 guessing.
 *
 * Examples:  "€5.2c" (EUR, m=100)  |  "0.52 kr" (SEK, m=1)
 */
export function formatPricePerKwh(
  pricePerKwh: number | null | undefined,
  c: CurrencyInfo,
): string {
  if (pricePerKwh == null || !c.loaded) return "—";
  const v = pricePerKwh * c.multiplier;
  const decimals = c.multiplier >= 100 ? 1 : 2;
  return `${c.prefix}${v.toFixed(decimals)}${c.suffix}`;
}

/** Format an absolute cost, e.g. "€12.50" or "125.00 kr". */
export function formatCost(
  cost: number | null | undefined,
  c: CurrencyInfo,
): string {
  if (!c.loaded) return "—";
  const v = cost ?? 0;
  return `${c.prefix}${v.toFixed(2)}${c.suffix}`;
}

/** Chart axis label — pre-built by the server. */
export function priceAxisLabel(c: CurrencyInfo): string {
  return c.priceLabel;
}

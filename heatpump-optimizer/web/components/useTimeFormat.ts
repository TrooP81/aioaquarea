"use client";

import { useEffect, useState } from "react";

export interface TimeFormatInfo {
  /** Whether to use 12-hour clock. */
  hour12: boolean;
  /** True once the API response has been received. */
  loaded: boolean;
}

const LOADING: TimeFormatInfo = { hour12: false, loaded: false };

export function useTimeFormat(): TimeFormatInfo {
  const [info, setInfo] = useState<TimeFormatInfo>(LOADING);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/time-format")
      .then((r) => {
        if (!r.ok) throw new Error("not ok");
        return r.json();
      })
      .then((data) => {
        if (!cancelled) setInfo({ hour12: !!data.hour12, loaded: true });
      })
      .catch(() => {
        if (!cancelled) setInfo({ hour12: false, loaded: true });
      });
    return () => { cancelled = true; };
  }, []);

  return info;
}

/**
 * Format a Date to a time string respecting the user's time format preference.
 */
export function formatTime(
  date: Date,
  hour12: boolean,
  opts?: { seconds?: boolean },
): string {
  return date.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    ...(opts?.seconds ? { second: "2-digit" } : {}),
    hour12,
  });
}

/**
 * Format an hour number (0-23) to a display string.
 * 24h: "07", "22"
 * 12h: "7 AM", "10 PM"
 */
export function formatHourLabel(h: number, hour12: boolean): string {
  if (!hour12) {
    return h.toString().padStart(2, "0");
  }
  const suffix = h >= 12 ? "PM" : "AM";
  const display = h === 0 ? 12 : h > 12 ? h - 12 : h;
  return `${display}${suffix}`;
}

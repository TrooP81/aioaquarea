import packageInfo from "../package.json";

/** The version compiled into the dashboard that the user is currently viewing. */
export const APP_VERSION = packageInfo.version;

export interface ReleaseNote {
  version: string;
  released: string;
  title: string;
  changes: readonly string[];
}

/**
 * User-facing release notes. Update this together with the package version for
 * every release so the Settings page remains an accurate deployment history.
 */
export const RELEASE_HISTORY: readonly ReleaseNote[] = [
  {
    version: APP_VERSION,
    released: "2026-07-11",
    title: "More accurate heating forecasts",
    changes: [
      "COP learning now uses the hot-water electricity counter that matches the measured tank heat, excluding simultaneous space-heating energy.",
      "The indoor-temperature forecast and heat-curve selection now use forecast wind, solar irradiance, and rainfall for every hour.",
      "Comfort-model training now uses only measurements known at the forecast time, avoiding future-data leakage in its accuracy validation.",
      "Thermal calibration now pairs each reading with the closest valid earlier sample, preserving real heating and cooling rates through mode changes.",
      "Observed temperatures of exactly 0°C are now retained throughout forecasts instead of being mistaken for missing data.",
      "The corrected COP model is retrained automatically after deployment instead of reusing an incompatible older checkpoint.",
      "Charts now open with one shared indoor-comfort view that relates temperature, weather, planned control changes, and electricity price; raw and hot-water details are optional.",
    ],
  },
  {
    version: "0.2.0",
    released: "2026-07-10",
    title: "Weather-aware ML and clearer plan visibility",
    changes: [
      "Rainfall from the selected weather provider is stored and used by the ML forecasts.",
      "The weather forecast chart now shows hourly rainfall alongside temperature and wind.",
      "The MILP optimizer now reserves enough heating capacity for forecast space-heating demand.",
      "Recent Activity separates completed, failed, and skipped actions from the stream of plan revisions.",
      "Plan actions stay attached to the selected plan when switching between plans quickly.",
      "Dashboard and Settings now group information into focused tabs instead of one long scrolling page.",
    ],
  },
  {
    version: "0.1.0",
    released: "Initial release",
    title: "Heat pump monitoring and optimisation",
    changes: [
      "Dashboard for device status, energy use, prices, weather, and planned actions.",
      "Rules-based and MILP scheduling with configurable comfort and hot-water limits.",
      "Data collection, ML training, and editable runtime settings.",
    ],
  },
];

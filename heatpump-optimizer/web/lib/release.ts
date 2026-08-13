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
    released: "2026-07-28",
    title: "Weather-based outdoor temperature compensation",
    changes: [
      "Planning, forecasts, and new ML training now use the configured weather report as the effective outdoor temperature instead of a sun-heated Aquarea sensor.",
      "The physical heat-pump sensor is preserved separately, and Overview shows the live difference and applied compensation.",
      "Settings provides source selection, a local weather offset, and a freshness timeout with automatic pump-sensor fallback."
    ],
  },
  {
    version: "0.11.1",
    released: "2026-07-28",
    title: "Comfort-gap guidance and consistent forecasts",
    changes: [
      "Comfort risks now explain the predicted shortfall, controller limitation, and whether a planned action can actually add room heat.",
      "Blocked comfort gaps provide a manual-only, bounded Värme AV candidate and require measured verification before another adjustment.",
      "Rules, forecast charts, and weather inputs now use the same no-space-heat baseline; quiet-mode transitions and forecast provenance are clearer."
    ],
  },
  {
    version: "0.10.0",
    released: "2026-07-17",
    title: "Lead-time-safe forecasts and clearer evidence",
    changes: [
      "Panasonic's -5°C weather-curve sentinel is now normalised before it reaches indoor-temperature learning and is shown as a weather-compensated curve rather than a false temperature in the UI.",
      "The comfort model trains independent 1, 3, 6 and 12-hour forecasts. Forecast quality now validates each lead time separately, so a measured poor longer horizon safely falls back to rules without hiding behind a good short-horizon MAE.",
      "Models now show seasonal readiness progress, weather-regime coverage, and a transparent published-price horizon. Price publication is checked every 15 minutes and queues a single re-plan when the horizon extends.",
      "Sensor diagnostics add an observation-only shadow view of freshness, cadence and room spread. It never changes the chosen comfort sensor or sends a heat-pump command automatically."
    ],
  },
  {
    version: "0.9.0",
    released: "2026-07-16",
    title: "Evidence-led indoor forecasts",
    changes: [
      "Room-heating data now requires an explicit active-heating report from the pump. A PUMP direction while the unit is off is no longer mistaken for delivered heat.",
      "The comfort model now learns hour-aligned, causal inputs including the heat-curve target, confirmed heating duration, recent heating, the selected room sensor or robust median, and indoor temperature trend.",
      "Retraining keeps the previous compatible comfort model when chronological validation MAE gets worse. Models now show their MAE, persistence baseline, heating-evidence count, horizon, and sensor strategy.",
      "Heat-pump status explicitly shows whether space heating is confirmed. Manual heat-curve trials pause in warm weather, defrost, hot-water operation, or cooling and never send commands automatically."
    ],
  },
  {
    version: "0.8.1",
    released: "2026-07-15",
    title: "Forecast-gated room control",
    changes: [
      "Comfort-hour and cheap-price mode changes now use the passive indoor forecast. When stored heat already keeps the home above target, the optimizer keeps the lower-demand mode instead of restoring Normal mode.",
      "Pre-heating now requires a forecasted comfort risk and aims at the configured comfort target rather than adding a fixed 2°C to the current indoor temperature.",
      "Plan history explains when the predicted indoor temperature already exceeds the target, making intentional heat avoidance visible. Explicit Optimize Now requests replace later pending actions too, rather than being limited to the near-term stability window."
    ],
  },
  {
    version: "0.8.0",
    released: "2026-07-15",
    title: "Weather-aware learning and safer comfort control",
    changes: [
      "Comfort advice now explicitly identifies warm weather above the heat-curve cutoff as not heat-pump controllable, preventing misleading curve suggestions outside the heating season.",
      "The dashboard can use one trusted SmartThings room sensor as the comfort reference and shows the sensor spread instead of hiding disagreement between rooms.",
      "Outcome reporting adds a clearly labelled, non-causal comparison with earlier windows of similar outdoor weather; optional manual-review trials remain command-free and bounded by the existing verification loop.",
      "Comfort, demand, and COP models now share humidity and cloud-cover features. Their persisted feature schemas are versioned, so retraining safely replaces older models.",
      "Saved plans now explain hourly price volatility and the stability policy for near-term versus later actions. CI runs browser tests in Chromium, Firefox, and mobile Chromium with failure artifacts."
    ],
  },
  {
    version: "0.7.0",
    released: "2026-07-15",
    title: "Condition-aware forecasts and resilient operations",
    changes: [
      "Forecast validation now scores rain, cold, and mild weather separately. Unobserved rain or cold adds a conservative comfort reserve, while a failing regime only falls back to rules when that weather is forecast.",
      "Recent forecast bias is applied as a bounded, evidence-based correction to the saved indoor forecast and MILP target calculation.",
      "Seasonal calibration is an explicit observe-only option: it can pause commands during detected heating weather to collect natural demand and indoor-heating evidence, but never changes pump settings by itself.",
      "Plans clearly retain the published price horizon. When newly published prices complete tomorrow's horizon, the poller queues one safe refresh through the optimizer service.",
      "The API and database now bind to localhost by default. Optional backup replication encrypts archives, writes a checksum, and exposes backup freshness to readiness checks."
    ],
  },
  {
    version: "0.5.0",
    released: "2026-07-14",
    title: "Production-safe control inputs and accountable forecasts",
    changes: [
      "The dashboard now uses a server-side API gateway, so production bearer authentication protects the API without breaking the web UI; Docker health checks stay available without a token.",
      "Daily cost never fills price gaps with a guessed rate: it shows priced coverage and marks incomplete amounts as awaiting price data.",
      "Every new plan records its price source, currency, source-data freshness, model evidence, and the immutable hourly forecast inputs used to create it.",
      "Forecast accuracy now reports bias and P90 error in addition to MAE. After enough saved forecasts, poor quality automatically keeps scheduling on the safe rules layer.",
      "Validated comfort forecasts add a small bounded planning reserve based on their measured error; the visible comfort target itself is unchanged.",
      "Automatic database backups, bounded archive retention, and an isolated restore-verification command are included for local operations."
    ],
  },
  {
    version: "0.4.3",
    released: "2026-07-14",
    title: "Safer plans and measurable forecast quality",
    changes: [
      "Hot-water forcing is skipped when the thermal model estimates only a negligible top-up, and the live tank target is checked again just before a command is sent.",
      "Room-water target commands now stop safely when the pump reports a heat-curve sentinel rather than a real water setpoint.",
      "New plans begin at the next full price hour, while equivalent near-term plans are retained instead of constantly replacing scheduled actions.",
      "Price amounts retain their provider, currency, and plan-time currency so SEK is never silently labelled as EUR.",
      "Thermal standby calibration rejects unsafe extrapolations and the Models view explains when heating-season data is still needed.",
      "Plan history now explains command changes and execution timing; Models adds a scorecard that compares saved forecasts with later indoor readings."
    ],
  },
  {
    version: "0.4.2",
    released: "2026-07-13",
    title: "Trustworthy plans, model data, and runtime checks",
    changes: [
      "The active-plan progress count now comes from the real plan actions, and 24-hour plan windows show both dates.",
      "Replaced plans are condensed into one history event instead of flooding Recent Activity with individual cancellations.",
      "Demand-model readiness now shows usable energy intervals, rejected implausible rates, and weather matches instead of counting plan actions.",
      "Indoor thermal confidence is separated from tank calibration; unlearned room-heating rates cannot be presented as learned or drive Comfort forecasts.",
      "Automatic commands pause when live heat-pump status is stale, and Models reports the age of the latest device status.",
      "CI now uses the production TimescaleDB image, applies migrations, runs Chromium E2E coverage, and Docker builds pin runtime images."
    ],
  },
  {
    version: "0.4.1",
    released: "2026-07-13",
    title: "Clear model validation status",
    changes: [
      "A comfort model that has trained but has not passed validation is now labelled ‘Trained · observe only’ in amber instead of appearing ready.",
      "Training feedback states whether the model was approved for room-temperature control or remains observation-only, with the validation reason.",
      "Automatic optimization now explains that its current decision engine is the safest available layer."
    ],
  },
  {
    version: "0.4.0",
    released: "2026-07-13",
    title: "Safer control and explainable plan revisions",
    changes: [
      "Only one plan can be active; a replacement atomically cancels the older plan's pending actions and records the revision link.",
      "Manual re-plans are queued to the optimizer service, avoiding competing plans from the dashboard API.",
      "Room control now uses fresh selected sensors, a robust median, outlier protection, and pauses room-heating automation when no trusted reading exists.",
      "Comfort-model predictions need validated quality before they can influence control; otherwise the thermal fallback remains in charge.",
      "Weather records, plan snapshots, and forecast APIs retain the selected provider and forecast issue time."
    ],
  },
  {
    version: "0.3.6",
    released: "2026-07-12",
    title: "Local-time hot-water deadlines",
    changes: [
      "Hot-water ready-by deadlines now use the configured local timezone instead of treating local comfort hours as UTC.",
      "Plans that cross midnight now use the following calendar day's weekday or weekend comfort schedule when choosing the next hot-water deadline.",
      "The action explanation now matches the local deadline it was optimized to meet.",
    ],
  },
  {
    version: "0.3.5",
    released: "2026-07-12",
    title: "Measured heat-curve verification",
    changes: [
      "Saving a controller heat-curve change now starts a verification window with the indoor and outdoor readings at the time of the change.",
      "New heat-curve recommendations stay locked for at least 24 hours, six new indoor readings, and three cooler-weather readings below the controller cut-off.",
      "The Settings page reports the observed comfort-distance change before it unlocks the next manual recommendation.",
    ],
  },
  {
    version: "0.3.4",
    released: "2026-07-12",
    title: "Panasonic heat-curve comfort planning",
    changes: [
      "Settings now record the Panasonic outdoor-to-water curve, heating-off outdoor temperature, and ΔT shown on the controller.",
      "Rules and MILP planners use the recorded curve hour by hour, including the controller's heating-off cutoff, instead of projecting a single live water reading.",
      "Settings provides a bounded manual curve recommendation based on the latest indoor temperature and comfort target; it is a draft only and never controls the heat pump automatically.",
    ],
  },
  {
    version: "0.3.3",
    released: "2026-07-12",
    title: "Explicit heating control forecasts",
    changes: [
      "Rule-based plans now add indoor heat only during explicit zone-temperature actions; NORMAL, ECO, and Quiet modes no longer imply full-hour room heating.",
      "A plan with no room-heating action now matches its no-heating comparison, making the chart's causal difference clear.",
      "The chart tooltip shows each hour's planned space-heating share.",
    ],
  },
  {
    version: "0.3.2",
    released: "2026-07-12",
    title: "Plan-based indoor forecast",
    changes: [
      "Comfort forecast now displays the temperature trajectory saved when the active plan was solved, rather than a generic schedule estimate.",
      "No heating is the same plan's weather-aware counterfactual, so it shows what the optimizer expected without space heating.",
      "Weather, rain, electricity price, comfort target, and planned control level are frozen together with the plan and remain aligned in the chart.",
    ],
  },
  {
    version: "0.3.1",
    released: "2026-07-12",
    title: "Unified forecast API",
    changes: [
      "The indoor forecast now returns the timestamped temperature, wind, sun, and rain data used for every prediction.",
      "The shared comfort chart reads weather, plan, temperatures, and electricity price from one aligned forecast timeline instead of mixing API responses.",
      "The API contract is shown on Settings so a live dashboard can be verified against its running forecast service.",
    ],
  },
  {
    version: "0.3.0",
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

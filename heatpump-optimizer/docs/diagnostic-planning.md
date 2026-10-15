# Conditional diagnostic planning

The represented strategy is **DHW timing plus unchanged native space heating**.
It is not an actuator policy, a physical comfort guarantee, or a globally optimal
household-cost claim. Ordinary actions remain empty when published by this
observe-only build; the modeled commands are retained as
`diagnostic_strategy_actions`.

## Admissible assumptions

`native_sh_trace_v1` requires more than a basis label. Only an explicit
conditional assumption is currently supported. A historical label is **unproven**:
there is no implemented historical reader that establishes complete,
selected-device, native-only duty provenance. ECO, quiet-mode and temperature
targets are not converted into compressor duty.

The producer's observation context must contain:

- Actual device identity, aware observation/capture timestamps, the existing
  freshness allowance, an observed positive tank target, and inactive force-DHW.
- The observed hardware tank minimum (`tank_hardware_floor_c`); the caller also
  supplies the observed hardware ceiling. Effective hourly floors are the maximum
  of the hardware minimum and the unchanged operational floor; the ceiling is the
  minimum of the hardware and unchanged operational ceilings.
- Explicit numeric `auxiliary_power_kw: 0`. Missing, nonfinite, boolean, negative
  or nonzero auxiliary load is unsupported. No auxiliary electricity is silently
  omitted or inferred to be zero.
- `native_sh_provenance`, containing:
  - `kind: "explicit_conditional_assumption"`
  - `method: "unchanged_hourly_load_over_declared_max_electrical_power"`
  - Matching `device_id`, aware `captured_at`, `horizon_start`, `horizon_end`
  - Positive `maximum_electrical_power_kw`
  - A complete `hourly_electrical_kw` list, including genuine declared zeros.

The declaration timestamp must lie between the observation and planning capture
clocks, within the existing observation freshness policy. Its horizon must exactly
match the contiguous, fully priced hourly trace. Each declared load must equal the
row's fraction times maximum electrical power. Loads must be within that maximum;
blocked SH requires zero load, and unknown or missing gates are refused.

The real cached-observation producer only copies these explicit declarations from
`native_policy_provenance.diagnostic_assumptions` (keys
`native_sh_provenance`, `auxiliary_power_kw`). It does **not** create them from
weather, tank targets, compressor flags, historical labels, or missing fields.
This is an input contract, not a new setting or a mechanism for granting control.
No collector, database backfill, schema migration, or settings change is included.
The current product collector supplies **no `diagnostic_assumptions`**. Consequently
normal current product inputs cannot produce an admissible represented diagnostic.
Only explicit synthetic/offline caller declarations currently exercise this path.
This approved restriction does not grant control permission. MILP still estimates
demand before the validator conservatively requires it to match the declaration;
this change adds no producer or configuration route for those declarations.

## Replay and accounting

Actual initial tank and room temperatures are retained without clamping. Initial
violations are disclosed; feasible modeled recovery does not erase them.
Replay checks hourly endpoints and serialized pulse boundaries against tank and
indoor bounds, and checks deadlines through the final priced hour. Commands require
exact integer durations and linked restores inside the complete priced horizon.
Observed tank-target suppression/termination makes a commanded pulse unproven.

The final priced endpoint is included as a ready-by deadline, but the horizon
start is not a future deadline. Both producers limit deadline extraction to the
actual priced end, including shortened windows. A tank initially above its ceiling
can passively recover by the first endpoint without an invented pulse peak;
active pulse peaks and every endpoint still obey the original ceiling.

Ready-by temperatures are explicit: `deadline_tank_floors_c` is aligned one-to-one
with `deadline_hours` in the serialized trace. Both producers supply the existing
configured comfort tank minimum for each deadline; the builder takes the maximum
with the observed hardware minimum. This requirement is independent of slot-start
hourly floors. For example, a 06:00–12:00 priced window with comfort only at 12:00
can have six 40 C hourly off-peak floors but still require 48 C at the deadline.
Neither solver nor replay substitutes the maximum in-window floor. Missing,
nonfinite, incomplete, duplicate or hardware-inconsistent deadline requirements
are unproven, not silently defaulted. Serialized DTO validation uses the same
explicit requirements.

During DHW, native SH is diverted completely. Otherwise its declared load is
unchanged. Tank heating is a **net** temperature rate; loss is applied separately
only during non-DHW time, not subtracted twice during heating. Electrical DHW power
is gross tank heat divided by assumed COP. Each segment contributes
`electrical kW × elapsed hours × price currency/kWh` to cost, including negative
prices and price-boundary crossings.

Before persistence, the orchestrator reconstructs replay from the serialized
diagnostic commands and checks the stored states, energy, cost, horizon and
forecast against it. A mismatched DTO is refused before opening a publication
session. Publication remains diagnostic-only, with
`modeled_conditional_not_physical_or_global_optimality` assurance.
The check also binds the fully passive counterfactual, price timestamps/values,
currency accounting and supplied MILP compatibility trajectories/fractions to the
same trace. Rules publish the represented counterfactual and prices, not a second
legacy projection with potentially different passive rates.

Offline acceptance anchors: `tests/test_s4_diagnostic_contract.py`,
`tests/test_remaining_scope.py`, `tests/test_milp.py`, and the represented-policy
controls in `tests/test_rules_engine_snapshot.py`. These use synthetic
declarations and mocked I/O, not measurements of an installation.

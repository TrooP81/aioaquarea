import type { ResetHistory } from "@/lib/api-types";

export function ResetHistoryNotice({ history }: { history?: ResetHistory | null }) {
  if (!history) return null;
  if (!history.available) return <p role="status">Reset history is unavailable; physical safety is not established.</p>;
  if (!history.data_reset_at && !history.historical_retirement) return null;
  return (
    <div role="status" data-testid="reset-history">
      <div>{history.data_reset_at ? `Fresh data collection reset: ${history.data_reset_at}` : "Data reset not committed; historical retirement only."}</div>
      <div>Retained campaign timing is a preference history, not surviving training evidence.</div>
      {history.historical_retirement && (
        <div>Historical obligation administratively retired (operator_invalidated / reset_superseded).
          No device command was sent; physical OFF was not verified. This is not a pending restore.</div>
      )}
    </div>
  );
}

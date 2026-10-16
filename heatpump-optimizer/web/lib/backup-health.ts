/** Public projection only: no archive identifiers, paths, checksums or source identity. */
export interface BackupHealth {
  ready: boolean;
  status: "healthy" | "warning" | "degraded";
  reason_codes: string[];
  last_success: string | null;
  last_attempt: {
    started_at: string | null;
    finished_at: string | null;
    status: string;
  };
  last_verification: {
    started_at: string | null;
    finished_at: string | null;
    status: string;
  };
  last_verified: string | null;
  success_age_seconds: number | null;
  stale_after_seconds: number;
  next_due_at: string | null;
  restore_qualified: boolean;
}

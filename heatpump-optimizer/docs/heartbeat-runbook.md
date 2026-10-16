# External heartbeat runbook

1. In Healthchecks.io, create a separate **house reporting/data heartbeat** check
   with a **60-second period** and **300-second grace**. Configure notifications
   independently of this application.
2. Set `EXTERNAL_HEARTBEAT_ALLOWED_AUTHORITIES=hc-ping.com:443` (or the exact
   provider authority approved for the deployment) in protected deployment
   configuration. Set the ping URL in the protected environment file or the
   Settings page; the Settings API fully redacts it. Do not commit a real URL.
3. Keep the URL secret and enable with `EXTERNAL_HEARTBEAT_ENABLED=true` or the
   runtime setting `external_heartbeat_enabled=true`. Set the interval to 60
   seconds unless the provider check period is deliberately changed. Restart the
   poller after changing deployment-only environment variables.
4. Verify a success in the provider dashboard only after poller and optimizer
   heartbeats, Redis, API `/health`, and fresh persisted device observations are
   all healthy. Confirm an invalid or disabled configuration sends no request.
   Do not test outage notifications by stopping production services; schedule that
   check in an owner-approved maintenance window with a separate test check.

This check detects missing successful reports, not necessarily a hung container.
A container that is unhealthy but remains running is alert-only; restart policies
recover exited containers, not unhealthy ones. Windows Docker Desktop currently
starts on user login, so unattended reboot recovery remains an operational blocker
until startup behavior is deliberately changed and tested.

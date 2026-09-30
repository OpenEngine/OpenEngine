# Autonomous projects

Open **Projects** in the sidebar and choose **New project**. Select a repository
and workflow, write the instructions to give the agent on each run, and configure
working days, start/end times, and an IANA timezone such as `America/Denver`.
Enable autonomous scheduling and save. Projects are paused by default.

The daily budget is a maximum number of WorkOrders started per local calendar
day (1–100), not a currency or token limit. The allowance resets at local
midnight. Overnight working hours belong to the day they start. Only one
WorkOrder per project runs at a time; a WorkOrder waiting for review or approval
also occupies that slot. Runs use the selected workflow's default inputs and
the service's existing approval settings.

The running web service checks projects on startup and every minute. It stores
the daily allowance before starting work, so an interrupted start cannot be
repeated automatically after restart. Failed or interrupted starts pause the
project and show an error. Review the latest WorkOrder before enabling it again.
The reserved allowance is kept even if starting fails.

Edit a project to change its instructions or schedule, or uncheck **Enable
autonomous scheduling** to pause it. Pausing and changing hours prevent new
starts; they do not stop work already running. The sidebar shows today's usage
and links to the latest WorkOrder. Project settings and usage persist in SQLite.

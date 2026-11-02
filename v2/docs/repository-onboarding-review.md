# Local repository onboarding review

Run from `v2/`. `uv sync` printed:

```text
Resolved 54 packages in 10ms
Checked 53 packages in 6ms
```

Exact service walkthrough output:

```text
$ uv run oe down
OpenEngine stopped.

$ uv run oe up
  ___  ___ ___ _  _ ___ _  _  ___ ___ _  _ ___
 / _ \| _ \ __| \| | __| \| |/ __|_ _| \| | __|
| (_) |  _/ _|| .` | _|| .` | (_ || || .` | _|
 \___/|_| |___|_|\_|___|_|\_|\___|___|_|\_|___|

NAME        PID    STATUS   ADDRESS
supervisor  57456  ready
nats        57460  ready    nats://127.0.0.1:4222
cxn         57462  running
ge          57463  ready    http://127.0.0.1:52928/healthz
tools       57464  running
vcp         57465  ready    http://127.0.0.1:52926/healthz

$ uv run oe repo add .. --project alpha --base-ref main --workspace-policy worktree --no-trust
CHECKOUT                                        PROJECT  BASE REF  WORKSPACE POLICY  TRUST
/private/tmp/engine-workspaces/ws-cc4581bae439  alpha    main      worktree          approval-required

$ uv run oe repo add ../v2 --project beta --base-ref HEAD --workspace-policy worktree --trust
CHECKOUT                                        PROJECT  BASE REF  WORKSPACE POLICY  TRUST
/private/tmp/engine-workspaces/ws-cc4581bae439  beta     HEAD      worktree          trusted

$ uv run oe repo list --project alpha
CHECKOUT                                        PROJECT  BASE REF  WORKSPACE POLICY  TRUST
/private/tmp/engine-workspaces/ws-cc4581bae439  alpha    main      worktree          approval-required

$ uv run oe repo get --project beta
CHECKOUT                                        PROJECT  BASE REF  WORKSPACE POLICY  TRUST
/private/tmp/engine-workspaces/ws-cc4581bae439  beta     HEAD      worktree          trusted

$ uv run oe down
OpenEngine stopped.

$ uv run oe up
  ___  ___ ___ _  _ ___ _  _  ___ ___ _  _ ___
 / _ \| _ \ __| \| | __| \| |/ __|_ _| \| | __|
| (_) |  _/ _|| .` | _|| .` | (_ || || .` | _|
 \___/|_| |___|_|\_|___|_|\_|\___|___|_|\_|___|

NAME        PID    STATUS   ADDRESS
supervisor  57602  ready
nats        57607  ready    nats://127.0.0.1:4222
cxn         57608  running
ge          57609  ready    http://127.0.0.1:52952/healthz
tools       57610  running
vcp         57612  ready    http://127.0.0.1:52954/healthz

$ uv run oe repo list --project alpha
CHECKOUT                                        PROJECT  BASE REF  WORKSPACE POLICY  TRUST
/private/tmp/engine-workspaces/ws-cc4581bae439  alpha    main      worktree          approval-required

$ uv run oe repo get --project beta
CHECKOUT                                        PROJECT  BASE REF  WORKSPACE POLICY  TRUST
/private/tmp/engine-workspaces/ws-cc4581bae439  beta     HEAD      worktree          trusted

```

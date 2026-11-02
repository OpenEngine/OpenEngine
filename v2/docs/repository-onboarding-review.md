# Local repository onboarding review

Run from `v2/`. `uv sync` printed:

```text
Resolved 54 packages in 4ms
Checked 53 packages in 7ms
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

NAME        PID   STATUS   ADDRESS
supervisor  3675  ready
nats        3678  ready    nats://127.0.0.1:4222
cxn         3680  running
ge          3681  ready    http://127.0.0.1:56773/healthz
tools       3682  running
vcp         3684  ready    http://127.0.0.1:56775/healthz

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

NAME        PID   STATUS   ADDRESS
supervisor  3859  ready
nats        3863  ready    nats://127.0.0.1:4222
cxn         3864  running
ge          3865  ready    http://127.0.0.1:56809/healthz
tools       3866  running
vcp         3867  ready    http://127.0.0.1:56810/healthz

$ uv run oe repo list --project alpha
CHECKOUT                                        PROJECT  BASE REF  WORKSPACE POLICY  TRUST
/private/tmp/engine-workspaces/ws-cc4581bae439  alpha    main      worktree          approval-required

$ uv run oe repo get --project beta
CHECKOUT                                        PROJECT  BASE REF  WORKSPACE POLICY  TRUST
/private/tmp/engine-workspaces/ws-cc4581bae439  beta     HEAD      worktree          trusted
```

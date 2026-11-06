# Local repository onboarding review

Run from `v2/`. Exact command output:

```text
$ uv sync
Resolved 54 packages in 3ms
Checked 53 packages in 0.97ms

$ uv run oe down
OpenEngine is not running.

$ uv run oe up
  ___  ___ ___ _  _ ___ _  _  ___ ___ _  _ ___
 / _ \| _ \ __| \| | __| \| |/ __|_ _| \| | __|
| (_) |  _/ _|| .` | _|| .` | (_ || || .` | _|
 \___/|_| |___|_|\_|___|_|\_|\___|___|_|\_|___|

NAME        PID    STATUS   ADDRESS
supervisor  39569  ready
nats        39572  ready    nats://127.0.0.1:4222
cxn         39575  running
ge          39576  ready    http://127.0.0.1:61463/healthz
tools       39577  running
vcp         39578  ready    http://127.0.0.1:61465/healthz

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
supervisor  39721  ready
nats        39726  ready    nats://127.0.0.1:4222
cxn         39727  running
ge          39728  ready    http://127.0.0.1:61495/healthz
tools       39729  running
vcp         39730  ready    http://127.0.0.1:61498/healthz

$ uv run oe repo list --project alpha
CHECKOUT                                        PROJECT  BASE REF  WORKSPACE POLICY  TRUST
/private/tmp/engine-workspaces/ws-cc4581bae439  alpha    main      worktree          approval-required

$ uv run oe repo get --project beta
CHECKOUT                                        PROJECT  BASE REF  WORKSPACE POLICY  TRUST
/private/tmp/engine-workspaces/ws-cc4581bae439  beta     HEAD      worktree          trusted

```

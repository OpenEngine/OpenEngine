# Approval policy

A daemon sets the approval ceiling in `engine.toml`. A repository can check in
`.openengine.toml` at its root to set defaults beneath that ceiling. A run can
only tighten those defaults. Fresh installations still use `auto_approve = false`.

```toml
# engine.toml: owned by the daemon operator
[approvals]
auto_approve = true
allow = ["read", "edit"]
repositories = ["acme/*"]

[approvals.bash]
allow = ["uv run pytest **"]
deny = ["git push **"]
```

| Setting | Meaning |
| --- | --- |
| `auto_approve` | Permit blanket automatic approval of tool requests. Human questions and plan approvals still require a person. Defaults to `false`. |
| `allow` | Capabilities eligible for preapproval: `read`, `edit`, `bash`, `web`, `mcp`. Defaults to `["read"]`. |
| `bash.allow` | Shell command glob patterns eligible for preapproval. Defaults to `[]`. |
| `bash.ask` | Shell patterns requiring a person, even with blanket approval. Defaults to `[]`. |
| `bash.deny` | Shell patterns always refused. Deny beats ask and allow. Defaults to `[]`. |
| `repositories` | Backend-only glob patterns matching configured repository names or checkout paths that may supply defaults. Defaults to `["*"]`; `[]` disables delegation. |

`auto_approve = true` retains the backend's historical blanket consent behavior;
its bash deny and ask rules still apply. Explicit repository or run allow lists
disable blanket consent so their narrower grants actually restrict requests.
Repository/run capability lists are intersected with the backend `allow` list.
A run cannot combine an explicit allow restriction with `auto_approve = true`.
Spend and PR caps remain backend-owned; repository and run approval tables cannot set them.

A repository might commit:

```toml
# .openengine.toml
[approvals]
auto_approve = false
allow = ["read", "edit"]

[approvals.bash]
allow = ["uv run pytest **"]
deny = ["rm -rf **"]
```

Missing settings inherit from the enclosing layer. With no repository file,
the backend's own settings are the defaults. Repo requests exceeding the
ceiling are ignored with warnings on the run. Allow patterns must be identical
to a ceiling pattern, narrow a literal prefix followed by a wildcard, be covered
by a universal ceiling pattern, or be a literal command matching a ceiling pattern; uncertain glob inclusion is clamped.
Deny and ask patterns accumulate. Run overrides attempting to add grants or
remove inherited deny/ask patterns are refused with a clear error.

The file is read from a base commit, never from working-tree contents.
Provisioning retains its normal remote-ref refresh; the actual checkout's
committed base supplies the final policy before downstream agent nodes run.
Subsequent reprovisioning uses that pinned commit. Uncommitted files,
later agent edits, and resumed runs cannot replace the stored policy. The run
JSON includes `approvalPolicy`, provenance for each setting, warnings, and the
base revision. Node auto-approve toggles are bounded by that stored policy.

`[trusted_repos]` remains a deprecated delegation alias. It allows a named
repository to set defaults, including auto-approval **only when permitted by
the backend ceiling**. It no longer overrides `auto_approve = false`.

```bash
engine policy show                       # current checkout, selected backend
engine policy show --repo acme/api --backend shared
engine go --no-auto-approve "Fix the parser"
engine graph run default "Fix the parser" --allow read --allow edit
engine run get RUN_ID --pretty
```

`policy show` previews the backend's default base; a graph with an explicit
base-ref input can resolve a different committed repository policy. The policy
recorded on a run is authoritative. API submissions use an optional `approvals`
object with the repository table's shape; for example
`{"approvals":{"auto_approve":false,"allow":["read"]}}`.

Unattended project callers use the same resolver with
`reject_instead_of_ask=True`. Anything that would ask is refused, including
human-only questions, so no unattended run waits for a person. This mode grants
no additional permissions. The project scheduler described in the ongoing
projects plan remains a separate feature.

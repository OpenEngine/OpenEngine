# Tool Service - tools
Keeps the tools agents may call, who may call them, and a durable record of
every call. Its interface is the [tool service contract](../../packages/contracts/tool_service/v1/asyncapi.yaml);
only the contract, the database tables and the authorization module exist so far.

Data Model
Tool | An invocable capability bound to one connection, with its adapter, input and output schemas and recovery behaviour.
ToolGrant | Lets a workspace, or one step in it, call one tool, under a Cedar `when` condition and until it expires.
Policy | A Cedar `forbid` an operator applies as a ceiling: a call is allowed only if some grant permits it and no policy forbids it.
ToolInvocation | The durable record of one call, with the tool definition it resolved and each attempt.

Tables (`tools` database): `tools`, `tool_grants`, `tool_invocations`, `tool_invocation_attempts`, `policies`.

Authorization
A grant and the operator's policies are Cedar, evaluated together in one policy
set, so a policy always wins. A grant is a `permit` for one subject to invoke
one tool, narrowed by its `when` (checked at apply against the tool's
`inputSchema`) and its expiry; a policy is a `forbid`. The Cedar engine is
[cedar-python](https://pypi.org/project/cedar-python/), a Python binding over the
Rust `cedar-policy` crate, and `authorization.py` holds the schema, the
condition and ceiling checks, and the decision.

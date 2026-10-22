# Tool Service - tools
Keeps the tools agents may call, who may call them, and a durable record of
every call. Its interface is the [tool service contract](../../packages/contracts/tool_service/v1/asyncapi.yaml);
only the contract and the database tables exist so far.

Data Model
Tool | An invocable capability bound to one connection, with its adapter, input and output schemas and recovery behaviour.
ToolGrant | Lets a workspace, or one step in it, call one tool, within constraints and until it expires.
ToolInvocation | The durable record of one call, with the tool definition it resolved and each attempt.

Tables (`tools` database): `tools`, `tool_grants`, `tool_invocations`, `tool_invocation_attempts`.

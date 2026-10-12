// Messages between the terminal page and the bridge server, one JSON object
// per WebSocket message. A session starts with the client's `start`.

export type StartMessage = {
  type: "start"
  /** Defaults to a clean interactive bash. */
  command?: string
  args?: string[]
  cwd?: string
  /** Added to, and overriding, the fixed base environment. */
  env?: Record<string, string>
  cols?: number
  rows?: number
}

export type ClientMessage =
  | StartMessage
  | { type: "input"; data: string }
  | { type: "resize"; cols: number; rows: number }

export type ServerMessage =
  | { type: "started"; pid: number }
  | { type: "output"; data: string }
  | { type: "exit"; exitCode: number; signal?: number }
  | { type: "error"; message: string }

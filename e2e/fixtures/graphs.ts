// Graphs the specs save, kept here so a change to the graph schema is one edit.

/** A one-step graph any agent can finish quickly, and nothing it does can be wrong. */
export const HELLO = `apiVersion: openengine.cc/v1
kind: Graph
name: hello
description: Greet the reader.
implementation:
  greet:
    agent: claude
    prompt: Reply with one short, friendly sentence greeting the reader. Do not run commands or edit files.
`

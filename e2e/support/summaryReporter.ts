import type { FullResult, Reporter, TestCase } from "@playwright/test/reporter"

// Playwright's own annotations, which say how a test is run and are not facts about the run.
const OWN = new Set(["skip", "fixme", "fail", "slow", "issue", "tag"])

/**
 * At the end of a run, print what the benchmark noted about it: the OpenEngine commit it tested, how long the
 * install took, whether anything had to be retried. Tests record these with `testInfo.annotations.push(...)`,
 * which no other reporter shows. The same notes are in the JSON file the config writes.
 */
export default class SummaryReporter implements Reporter {
  private notes: { test: string; type: string; description: string }[] = []

  onTestEnd(test: TestCase) {
    for (const { type, description } of test.annotations) {
      if (!OWN.has(type) && description !== undefined) this.notes.push({ test: test.title, type, description })
    }
  }

  onEnd(_result: FullResult) {
    if (this.notes.length === 0) return
    const width = Math.max(...this.notes.map(({ type }) => type.length)) + 2
    console.log("\nRun notes")
    for (const { type, description } of this.notes) console.log(`  ${`${type}:`.padEnd(width)}${description}`)
  }
}

import { gh, GhError, inContainer } from "./benchmark.ts"

/** The most a run, and the whole benchmark, may spend or take. A run over its cap is cancelled and recorded as a failure. */
export type Budget = { perRunUsd: number; totalUsd: number; perRunMinutes: number }

export type PendingApproval = { approvalId: string; node: string; kind: string }

/** What the daemon says about a run (the parts the driver uses). */
export type Run = {
  runId: string
  status: string
  terminal: boolean
  pendingApprovals: PendingApproval[]
  usage?: { costUsd: number | null } & Record<string, unknown>
  prUrl?: string | null
  pullRequests?: { repository: string; number: number }[]
  results?: Record<string, unknown>
  failure?: { error?: string; node?: string | null } | null
}

/** What the driver recorded about one run, for the report. */
export type RunRecord = { runId: string; status: string; costUsd: number; usage?: unknown; pullRequest?: number; impact?: unknown; note?: string }

/** The surface the `oe` command itself uses. */
const DAEMON = "http://127.0.0.1:4364/api/v1"

/** Ask the daemon from inside the container, where it listens: its port is not published to this computer. */
async function daemon<T>(path: string, body?: unknown): Promise<T> {
  // The daemon answers only to its service token, which the guide saved in `.env` beside `engine.toml`.
  const args = [
    "sh", "-c", 'set -a; . "$HOME/.config/openengine/.env"; exec curl -sS --fail-with-body -H "Authorization: Bearer $ENGINE_SERVICE_TOKEN" "$@"', "curl",
    ...(body === undefined ? [] : ["-X", "POST", "-H", "content-type: application/json", "-d", JSON.stringify(body)]), `${DAEMON}${path}`,
  ]
  try {
    return JSON.parse((await inContainer(...args)) || "{}") as T
  } catch (error) {
    // curl exits non-zero on an error answer; the answer says what was wrong.
    const { stdout = "" } = error as { stdout?: string }
    throw new Error(`${body === undefined ? "GET" : "POST"} ${path} failed: ${stdout.trim() || (error as Error).message}`)
  }
}

export const getRun = (runId: string) => daemon<Run>(`/runs/${runId}`)
export const listRuns = async (graph: string) => (await daemon<{ runs: Run[] }>(`/runs?graph=${graph}`)).runs
const approve = (run: Run, approval: PendingApproval) => daemon(`/runs/${run.runId}/approvals/${approval.approvalId}`, { decision: "accept" })
const cancel = (runId: string) => daemon(`/runs/${runId}/cancel`, {})

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

/**
 * `gh` for reading. GitHub answers a busy moment with a 5xx (a 504 stopped a half-hour run), so wait and ask again. Writes
 * don't use this: after a 504 a merge may have happened, and doing it twice is not the same as doing it once.
 */
async function ghRead(...args: string[]): Promise<string> {
  for (let attempt = 1; ; attempt++) {
    try {
      return await gh(...args)
    } catch (error) {
      const transient = error instanceof GhError && (error.status === undefined || error.status >= 500)
      if (!transient || attempt === 5) throw error
      await sleep(attempt * 10_000)
    }
  }
}

type PullRequest = { number: number; title: string; state: string; mergeStateStatus: string; statusCheckRollup: { name?: string; conclusion: string | null; status: string }[] }

async function pullRequest(repo: string, number: number): Promise<PullRequest> {
  return JSON.parse(await ghRead("pr", "view", String(number), "--repo", repo, "--json", "number,title,state,mergeStateStatus,statusCheckRollup")) as PullRequest
}

/** Checks all finished and none failed. A pull request with no checks counts as green. */
const green = (pr: PullRequest) => pr.statusCheckRollup.every((check) => check.status === "COMPLETED" && ["SUCCESS", "SKIPPED", "NEUTRAL"].includes(check.conclusion ?? ""))

/** Squash-merge, as the account `gh` is signed in as, whatever the pull request's impact rating. */
async function merge(repo: string, number: number) {
  await gh("pr", "merge", String(number), "--repo", repo, "--squash")
}

export type Driven = { runs: RunRecord[]; totalUsd: number; failures: string[] }

/**
 * Work the milestone until none of its issues is open: approve each run's human review, merge its pull request
 * once CI is green, merge release pull requests the same way, cancel any run over its budget and close an issue
 * that is still open after its pull request merged. Every run is recorded.
 *
 * `until` is when to stop: once the first run has finished and its pull request merged (so the loop that takes
 * over from it can't pick the issue it is still working on), or once no issue in the milestone is open.
 *
 * The approval comes before the merge: this account merges as the one OpenEngine uses, and OpenEngine ignores a
 * merge by its own login, so that merge would not release the review.
 */
export async function driveMilestone({ repo, milestone, graph, budget, until, onProgress }: { repo: string; milestone: string; graph: string; budget: Budget; until: "first-run-done" | "milestone-done"; onProgress?: (line: string) => void }): Promise<Driven> {
  const records = new Map<string, RunRecord>()
  const approved = new Set<string>()
  const merged = new Set<number>()
  const started = new Map<string, number>()
  const failures: string[] = []
  let totalUsd = 0

  // GitHub's issue listing lags behind what is true, and once showed none open while eleven were. Done means the listing
  // said so on several turns in a row, not once.
  let emptyReadings = 0

  for (;;) {
    const runs = await listRuns(graph)
    totalUsd = runs.reduce((sum, run) => sum + (run.usage?.costUsd ?? 0), 0)
    for (const listed of runs) {
      const run = await getRun(listed.runId)
      const cost = run.usage?.costUsd ?? 0
      started.set(run.runId, started.get(run.runId) ?? Date.now())
      const pr = run.pullRequests?.at(-1)?.number
      if (run.terminal) {
        if (!records.has(run.runId)) {
          // A provider's usage limit is not the benchmark failing, and the loop starts the issue again on its next turn.
          const limited = run.status === "failed" && /rate_limit|usage limit|session limit/i.test(run.failure?.error ?? "")
          const outcome = run.status === "completed" ? "completed" : limited ? "rate-limited" : run.status
          records.set(run.runId, { runId: run.runId, status: outcome, costUsd: cost, usage: run.usage, pullRequest: pr, impact: run.results?.impact, note: run.failure?.error })
          onProgress?.(`run ${run.runId} ${outcome}, $${cost.toFixed(2)}${pr ? `, PR #${pr}` : ""}`)
          if (outcome !== "completed" && outcome !== "rate-limited") failures.push(`${run.runId}: ${outcome} ${run.failure?.error ?? ""}`.trim())
        }
        continue
      }
      const minutes = (Date.now() - started.get(run.runId)!) / 60_000
      if (cost >= budget.perRunUsd || minutes >= budget.perRunMinutes) {
        await cancel(run.runId)
        const why = cost >= budget.perRunUsd ? `over $${budget.perRunUsd}` : `over ${budget.perRunMinutes} minutes`
        records.set(run.runId, { runId: run.runId, status: "over-budget", costUsd: cost, usage: run.usage, pullRequest: pr, note: why })
        failures.push(`${run.runId}: cancelled, ${why}`)
        onProgress?.(`run ${run.runId} cancelled, ${why}`)
        continue
      }
      for (const approval of run.pendingApprovals) {
        if (approval.node !== "human_review" || approved.has(approval.approvalId)) continue
        if (pr !== undefined) {
          // The run waited for CI before this step, but GitHub is the authority on it. A pull request whose checks are not
          // green is never approved or merged: the run is cancelled and recorded as a failure that names what was red.
          let pull = await pullRequest(repo, pr)
          let sentBack = false
          for (let waited = 0; !green(pull) && waited < 20 * 60_000; waited += 15_000) {
            await sleep(15_000)
            pull = await pullRequest(repo, pr)
            // The run can go back to the implementer on its own while the checks are red; its review then asks again, as a new approval.
            if (!(await getRun(run.runId)).pendingApprovals.some((pending) => pending.approvalId === approval.approvalId)) {
              sentBack = true
              break
            }
          }
          if (sentBack) {
            onProgress?.(`run ${run.runId}: sent back to the implementer while PR #${pr} was red`)
            approved.add(approval.approvalId)
            continue
          }
          if (!green(pull)) {
            const red = pull.statusCheckRollup.filter((check) => check.conclusion !== "SUCCESS" && check.conclusion !== "SKIPPED" && check.conclusion !== "NEUTRAL").map((check) => `${check.name ?? "a check"} (${check.conclusion ?? check.status})`)
            await cancel(run.runId)
            const note = `PR #${pr} was not green: ${red.join(", ") || "checks unfinished"}`
            records.set(run.runId, { runId: run.runId, status: "red-ci", costUsd: cost, usage: run.usage, pullRequest: pr, note })
            failures.push(`${run.runId}: ${note}`)
            onProgress?.(`run ${run.runId} cancelled: ${note}`)
            approved.add(approval.approvalId)
            continue
          }
        }
        await approve(run, approval)
        approved.add(approval.approvalId)
        onProgress?.(`run ${run.runId}: human review approved`)
        if (pr !== undefined) {
          if (!merged.has(pr)) await merge(repo, pr)
          merged.add(pr)
          onProgress?.(`run ${run.runId}: PR #${pr} merged`)
        }
      }
    }

    // A release pull request is not a run's: merge it once its checks pass.
    const open = JSON.parse(await ghRead("pr", "list", "--repo", repo, "--state", "open", "--json", "number,title")) as { number: number; title: string }[]
    for (const { number, title } of open) {
      if (merged.has(number) || !/^chore(\(.+\))?: release/i.test(title)) continue
      if (green(await pullRequest(repo, number))) {
        await merge(repo, number)
        merged.add(number)
        onProgress?.(`release PR #${number} merged`)
      }
    }

    // The issue a merged pull request was for stays open if its text didn't say it closes it.
    const issues = JSON.parse(await ghRead("issue", "list", "--repo", repo, "--milestone", milestone, "--state", "open", "--json", "number,title", "--limit", "100")) as { number: number; title: string }[]
    const idle = runs.every((listed) => records.has(listed.runId))
    if (until === "first-run-done" && runs.length > 0 && idle) break
    emptyReadings = issues.length === 0 ? emptyReadings + 1 : 0
    if (idle && emptyReadings >= 3) break
    if (idle && issues.length > 0 && runs.length > 0 && merged.size > 0) {
      for (const run of runs) {
        const record = records.get(run.runId)
        if (record?.status === "completed" && record.pullRequest !== undefined && merged.has(record.pullRequest)) {
          const view = JSON.parse(await ghRead("pr", "view", String(record.pullRequest), "--repo", repo, "--json", "closingIssuesReferences")) as { closingIssuesReferences: { number: number }[] }
          for (const { number } of view.closingIssuesReferences) {
            if (issues.some((issue) => issue.number === number)) {
              await gh("issue", "close", String(number), "--repo", repo, "--comment", "Closed by the benchmark: the pull request merged but did not close this issue.")
              onProgress?.(`issue #${number} closed by the benchmark`)
            }
          }
        }
      }
    }
    if (totalUsd >= budget.totalUsd) {
      failures.push(`total spend $${totalUsd.toFixed(2)} reached the cap of $${budget.totalUsd}`)
      break
    }
    await sleep(20_000)
  }
  return { runs: [...records.values()], totalUsd, failures }
}

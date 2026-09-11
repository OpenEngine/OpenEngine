/** What GitHub comments have asked of this engine, and what it did about them.
 *
 *  A comment reaches the engine through a webhook that is acknowledged in
 *  milliseconds and answered minutes later behind a queue, so the whole of it
 *  happens where nobody can see: until this panel, the only ways to tell a
 *  comment engine never received from one it deliberately ignored were GitHub's
 *  delivery log, the database, and server stdout.
 *
 *  Each comment is one row, and the row is a claim about what this process did
 *  -- seen, picked up, forwarded to a WorkOrder, answered -- with the comment
 *  itself quoted only far enough to recognise which one it is. Both ends are
 *  linked, because the two questions that follow any row are "which comment was
 *  that" and "which WorkOrder got it".
 */

import { useEffect, useState } from "react";

import { getGithubActivity, type ApiGithubActivity, type ApiGithubComment } from "./api";

/** How often the panel asks again. Slower than the WorkOrder page's own poll:
 *  a comment is answered over tens of seconds, and the one thing here that
 *  moves faster -- the queue -- is a number, not a story. */
const POLL_MS = 3000;

/** What each status says, in the reader's words rather than the wire's. */
const STATUS_LABEL: Record<string, string> = {
  queued: "Queued",
  working: "Working",
  dispatched: "Forwarded",
  replied: "Replied",
  ignored: "Ignored",
  failed: "Failed",
  handled: "Handled",
};

/** Which comments want an eye on them: one nothing answered, and one still
 *  moving. Everything else is a comment that is done with. */
const ALERT = new Set(["failed"]);
const LIVE = new Set(["queued", "working"]);

function statusLabel(status: string): string {
  return STATUS_LABEL[status] ?? status;
}

function kindLabel(event: string): string {
  return event === "pull_request_review_comment" ? "Review reply" : "Comment";
}

/** A time on the clock, in the reader's own zone. Empty for a step that has
 *  not happened, which is how an unfinished row leaves its later steps out. */
function at(seconds: number): string {
  if (!seconds) return "";
  const when = new Date(seconds * 1000);
  if (Number.isNaN(when.getTime())) return "";
  return when.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/** The steps one comment has been through, as they happened. Only the steps
 *  that did happen: a row that names a time it never reached would be reading
 *  the future out of an empty field. */
function steps(comment: ApiGithubComment): string[] {
  const told: string[] = [];
  if (comment.seenAt) told.push(`Received ${at(comment.seenAt)}`);
  if (comment.startedAt) told.push(`Picked up ${at(comment.startedAt)}`);
  if (comment.dispatchedAt) {
    // A comment that created the work it asks about reads very differently
    // from one that nudged work already running, so the step says which.
    const verb = comment.startedRun ? "Started" : "Forwarded to";
    told.push(`${verb} ${comment.dispatchedRunId} ${at(comment.dispatchedAt)}`);
  }
  if (comment.repliedAt) told.push(`Replied ${at(comment.repliedAt)}`);
  return told;
}

function CommentRow({ comment }: { comment: ApiGithubComment }) {
  const status = comment.status;
  return (
    <li className="gh-comment" data-status={status}>
      <div className="gh-comment-head">
        <span
          className={`chip ${ALERT.has(status) ? "chip-flame" : "chip-ink"}`}
          data-status={status}
        >
          {statusLabel(status)}
        </span>
        <span className="gh-comment-who">
          {kindLabel(comment.event)} from <strong>{comment.author}</strong> on{" "}
          {comment.repository}#{comment.number}
        </span>
        {comment.url && (
          <a className="gh-comment-link" href={comment.url} target="_blank" rel="noreferrer">
            View comment ↗
          </a>
        )}
      </div>
      {comment.excerpt && <p className="gh-comment-excerpt">{comment.excerpt}</p>}
      <p className="micro gh-comment-steps">{steps(comment).join(" · ")}</p>
      {/* The reply is this engine's own fixed announcement rather than
          anything a model wrote, so it is safe to repeat and worth repeating:
          it is exactly what the pull request was told. */}
      {comment.reply && <p className="micro gh-comment-reply">Posted back: {comment.reply}</p>}
      {comment.detail && <p className="micro gh-comment-detail">{comment.detail}</p>}
      {comment.dispatchedRunId && (
        <p className="micro">
          <a href={comment.runUrl || `/runs/${comment.dispatchedRunId}`}>
            Open WorkOrder {comment.dispatchedRunId}
          </a>
        </p>
      )}
    </li>
  );
}

/** The panel. Given a `runId` it shows only that WorkOrder's pull-request
 *  comments; without one it shows every comment this process has handled. */
export function GithubActivityPanel({ runId }: { runId?: string }) {
  const [activity, setActivity] = useState<ApiGithubActivity>();
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const load = () => {
      getGithubActivity(runId)
        .then((value) => {
          if (cancelled) return;
          setActivity(value);
          setError("");
        })
        .catch((reason: Error) => {
          if (!cancelled) setError(reason.message);
        })
        .finally(() => {
          if (!cancelled) timer = window.setTimeout(load, POLL_MS);
        });
    };
    load();
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [runId]);

  // Nothing is drawn before the first answer, or for a deployment no webhook
  // points at and nothing has ever reached: an empty panel that turns out to
  // be a loading one is worse than no panel at all. A later poll that fails
  // keeps what is on screen and says so, because the rows are still true.
  if (!activity || (!activity.configured && activity.comments.length === 0)) return null;

  const live = activity.comments.filter((comment) => LIVE.has(comment.status)).length;
  return (
    <section className="gh-activity" aria-label="GitHub comment activity">
      <div className="gh-activity-head">
        <div>
          <p className="eyebrow">GitHub comments</p>
          <h2>{activity.repository || "Comment activity"}</h2>
        </div>
        <div className="gh-activity-figures">
          <span className="micro">{activity.queued} queued</span>
          <span className="micro">{activity.sessions} open sessions</span>
          <span className="chip" data-tone={activity.working ? "live" : undefined}>
            {activity.working ? "Answering a comment" : "Idle"}
          </span>
        </div>
      </div>
      {error && <p className="notice">Could not read GitHub activity: {error}</p>}
      {activity.comments.length === 0 ? (
        <p className="state-inline">
          {runId
            ? "No GitHub comments have been delivered for this WorkOrder's pull request."
            : "No GitHub comments have been delivered yet."}
        </p>
      ) : (
        <ul className="gh-comments">
          {activity.comments.map((comment) => (
            <CommentRow comment={comment} key={`${comment.event}:${comment.commentId}`} />
          ))}
        </ul>
      )}
      {live > 0 && (
        <p className="micro">
          {live} comment{live === 1 ? "" : "s"} still moving through the engine.
        </p>
      )}
    </section>
  );
}

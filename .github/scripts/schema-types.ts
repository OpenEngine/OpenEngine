// The API surface used by publication, shared by the Actions adapter and test fakes.
type Repository = {owner: string; repo: string};
type Response<T> = Promise<{data: T}>;
export type TreeEntry = {
  path: string; mode: '100644'; type: 'blob'; content?: string | null; sha?: string;
};
export type CreateTree = Repository & {base_tree: string; tree: TreeEntry[]};
export type CreateCommit = Repository & {message: string; tree: string; parents: string[]};
export type UpdateRef = Repository & {ref: string; sha: string; force: boolean};
export type Comment = Repository & {issue_number: number; body: string};
type ListArgs = Repository & {
  pull_number?: number; per_page?: number; head?: string; state?: 'open';
};
export interface GitHub {
  rest: {
    git: {
      getRef(args: Repository & {ref: string}): Response<{object: {sha: string}}>;
      getCommit(args: Repository & {commit_sha: string}): Response<{tree: {sha: string}}>;
      createBlob(args: Repository & {content: string; encoding: 'base64'}): Response<{sha: string}>;
      createTree(args: CreateTree): Response<{sha: string}>;
      createCommit(args: CreateCommit): Response<{sha: string}>;
      updateRef(args: UpdateRef): Promise<unknown>;
    };
    pulls: {
      list: ListEndpoint<{number: number}>;
      listFiles: ListEndpoint<{filename: string; status: string}>;
    };
    issues: {createComment(args: Comment): Promise<unknown>};
  };
  paginate<T>(method: ListEndpoint<T>, args: ListArgs): Promise<T[]>;
}
// Octokit endpoints return response envelopes; paginate unwraps these.
export type ListEndpoint<T> = (args: ListArgs) => Promise<T[] | {data: T[]}>;
export interface Context {
  repo: Repository;
  payload: {action?: string; pull_request?: {number: number; head: {ref: string; sha: string}}};
  ref: string;
  sha: string;
}
export interface PublisherOptions {
  github: GitHub;
  context: Context;
  core: {notice(message: string): void};
}

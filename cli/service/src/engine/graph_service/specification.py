"""Specification prose; syntax placeholders are filled from the parser definitions.

Regenerate CLI output and site documentation with scripts/generate_cli_docs.py.
"""

from inspect import getdoc


def graph_spec(_arguments: object = None) -> int:
    """Print the current graph specification as Markdown, without contacting a backend.

    A graph is a YAML definition or a Python LangGraph file. Register it with
    `engine graph add FILE`; run it with `engine graph execute GRAPH INSTRUCTION`.
    Names belong to projects, versions are immutable, and registering an unchanged
    definition is a no-op. `engine graph get GRAPH --pretty` prints stored source.

    ### YAML graph specification (@API_VERSION@)

    ```yaml
    apiVersion: @API_VERSION@
    kind: Graph
    name: implement-review
    inputs:
      instruction: {description: The change to make}
    implementation:
      implement:
        agent: claude
        prompt: "Implement ${instruction}"
    review:
      reviewer:
        agent: {not: implement}
        prompt: "Review ${outputs.implement}"
    loop:
      every: 6h
      instruction: Find and fix one flaky test.
    ```

    @GRAPH_FIELDS@

    `apiVersion` is required; `kind` defaults to `@KIND@`. `name` is required:
    lowercase letters, digits, `.`, `_` and `-`, starting with a letter or digit,
    at most 63 characters. Unknown fields are rejected.

    `inputs` maps names to input declarations listed above. The
    instruction is built in; declaring `inputs.instruction` only describes it.
    Other inputs are supplied with `--input NAME=VALUE` when executing a graph.

    Steps are mappings in `plan`, `implementation` and `review`, executed in
    that order and in written order within each section. Step names are unique.
    At least one executable step is required. A checkout must come first; if
    omitted, a checkout named `workspace` is inserted. A checkout accepts
    `base_ref` and `ref: ${inputs.NAME}` (plus `name` and `description`).

    A step's keys determine its kind:

    - `agent` and `prompt`: an agent session. `agent` names a configured runner,
      uses `${inputs.NAME}`, or selects `{same: STEP}` or `{not: STEP}`.
      Runner inputs need a default or choices; @RUNNER_POLICIES@
      are supported selection policies in those inputs.
    - `human`: a question, or `{prompt, choose}` for a person to select findings.
    - `ci: true`: wait for CI on the change.
    - None of these keys: the checkout.

    Steps accept `name` and `description`. Agent steps also accept `tools`,
    `outputs`, `model`, `steering: always-open` and `facets`. Tools must be
    registered repository tools. Outputs map names to declarations with `type`,
    `enum`, `required`, `description` and `lineage: required` for findings.
    Types are @OUTPUT_TYPES@. A model may be a tier, model name or template. Facets run in
    parallel, using a built-in facet or a custom `{id, name, focus}`; extra
    fields become `item`. Outputs of a parallel step form a list.

    `flow` overrides written-order edges. Use `a -> b`, `[a, b] -> c` for a join,
    or `{from: STEP, route: [{when: CONDITION, to: STEP}, {to: end}]}` for
    routing. Routes try branches in order and require an unconditional final
    branch. `start` and `end` mark entry and exit. Cycles must pass through a
    route; every step must be reachable and able to finish.

    @EXPRESSIONS@

    `loop` accepts @LOOP_FIELDS@, defaults for `engine loop add`.
    See `engine loop spec` for cadence, limits and lifecycle.

    ### Python graph specification

    A Python file exports `workflow`, a `GraphWorkflow` or a tuple of them built
    with `graph_workflow` and the engine's LangGraph components. Its id supplies
    the default graph name. The backend loads and compiles it with its runtime;
    Python runs with daemon privileges and can be disabled by
    `[graphs] allow_python = false`. YAML and Python share the same registry,
    version history, checkpoints, approvals, steering and run feed.
    """
    print(getdoc(graph_spec))
    return 0


def loop_spec(_arguments: object = None) -> int:
    """Print the current loop specification as Markdown, without contacting a backend.

    A loop creates recurring runs of one pinned graph version. Create it with
    `engine loop add GRAPH`, where GRAPH is a name, `name@VERSION`, graph id or
    version id. Later registrations never change the pinned version.

    ### Defaults and creation

    A YAML graph may supply defaults:

    ```yaml
    loop:
      every: 6h
      instruction: Find and fix one flaky test.
    ```

    `loop` accepts @LOOP_FIELDS@. `--every` and `--instruction`
    override them. Both an instruction and a cadence must be available; neither
    is invented. Cadences are integer seconds in YAML/API, or durations such as
    `90s`, `30m`, `6h` and `1d`, with a minimum of @MIN_INTERVAL_SECONDS@ seconds.
    Duration units (seconds per unit): @UNITS@.

    `--name` defaults to the graph's name and must be unique within its project.
    `--project` scopes graph lookup, `--repo` selects the repository, and
    `--input NAME=VALUE` supplies graph inputs. `--backend` selects the daemon.
    The first run is due immediately unless `--no-run-now` delays it one interval.

    ### Scheduling and limits

    Runs never overlap. Ticks missed while a run is active collapse into one;
    each tick is recorded before starting its run so duplicate ticks do nothing.
    Limits are cumulative over the loop's lifetime and survive restarts.

    - `--max-prs` limits new pull requests opened by runs, excluding updates to
      existing pull requests. Reaching it pauses the loop; the active run may
      finish.
    - `--max-spend` limits USD of reported agent usage, using session cost or
      token pricing when available. It excludes CI, hosting and unpriced
      subscription usage. If usage is unpriced, `spendEnforceable` is false.
      The active run reserves the remaining budget and is cancelled when the
      loop's spend reaches the cap.
    - A loop also pauses when a run fails because a runner needs to sign in.

    ### Inspection and lifecycle

    `engine loops list` lists loops. `engine loop get NAME_OR_ID` reports state,
    the pinned graph version, instruction, inputs, cadence, next run, active run,
    runs, pull requests, spend and limits. Use `--pretty` for readable output.

    `engine loop pause NAME_OR_ID --reason TEXT` stops new runs.
    `engine loop resume NAME_OR_ID` restarts scheduling; it is refused while a
    limit is still reached. Supply higher `--max-prs` or `--max-spend` limits
    when resuming. Pausing does not cancel an active run.
    """
    print(getdoc(loop_spec))
    return 0

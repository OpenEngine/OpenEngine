/**
 * The calculator's issues, in the order they are worked: each one depends on the one before it. The first sets up the project, so the rest are worked inside it.
 * `description` is the paragraphs above the acceptance criteria, and `criteria` the checklist.
 */
export const issues: { title: string; description: string[]; criteria: string[] }[] = [
  {
    title: "Set up the project",
    description: [
      "Set up the calculator project so that everything after it has a place to go: a first page, tests, checks that run on GitHub, and publishing. The calculator itself comes in the next issues.",
      "Create a very small web page that works with no server, so it can be published as plain files. Choose any simple way of building it. The page only needs a heading that reads `Calculator`; nothing else yet.",
      "The project's changelog and version numbers will be built from its commit messages, so they must follow the [Conventional Commits](https://www.conventionalcommits.org/) format, like `feat: add a square root key` or `fix: show Error on divide by zero`. When a pull request is squash-merged, GitHub uses the pull request's title as the commit message, so the title matters most. Add a GitHub Actions workflow of its own that fails when a pull request's title, or any of its commits, does not follow the format. GitHub checks a pull request out as a merge commit it makes itself, called `Merge <sha> into <base>`; that commit is not the author's, so the check must ignore it.",
      "Add a GitHub Actions workflow that runs the project's tests on every pull request and on every push to the default branch. GitHub's runners do not have the same software as the machine you build on. Run the tests there with the same version of the language runtime that you run them with locally (check its `--version`), so a test that passes here passes in CI.",
      "GitHub Pages is already turned on for this repository, with GitHub Actions as its source. Do not change the repository's settings. Add a workflow that publishes the page whenever the default branch changes.",
    ],
    criteria: [
      "The page opens in a browser and shows the heading `Calculator`.",
      "There is an automated test that checks the page has that heading, and one command runs the project's tests.",
      "A workflow in `.github/workflows/` runs the tests on pull requests and on pushes to the default branch. It fails when a test fails, passes when they all pass, and uses the same runtime version as the one the tests run with locally.",
      "A workflow checks commit messages and pull request titles on every pull request: a title such as `Add a square root key` fails it, `feat: add a square root key` passes, and GitHub's own merge commit is ignored.",
      "A workflow in `.github/workflows/` publishes the page to GitHub Pages on every push to the default branch. Once the pull request is merged, the page opens at the repository's GitHub Pages address (`https://<owner>.github.io/<repository>/`) with the `Calculator` heading.",
      "The pull request that sets this up has a title in the commit message format and shows every workflow passing.",
    ],
  },
  {
    title: "Release automatically with a changelog",
    description: [
      "Cut a release automatically whenever changes are merged, using version numbers and a changelog built from the commit messages (see the commit message check).",
      "Use any release tool you like, as long as merging a pull request is enough: no release pull request or other manual step.",
    ],
    criteria: [
      "Every merge to the default branch with a `feat:` or `fix:` change publishes a new version that follows [semantic versioning](https://semver.org/): a `feat:` raises the minor version and a `fix:` the patch version.",
      "Each release is a GitHub release with a tag like `v0.1.0`.",
      "`CHANGELOG.md` is generated from the commits and lists the changes that were merged, grouped by type.",
    ],
  },
  {
    title: "Write the README",
    description: [
      "Someone new to the project should be able to use and change it from the README alone.",
    ],
    criteria: [
      "`README.md` says what the project is, in a sentence or two, and gives the address where the calculator is published.",
      "A section on how to run the calculator on your own computer, with the exact commands.",
      "A section on how to run the tests, with the exact command.",
      "A section on how it is published and released: what the workflows do, and when they run.",
      "A section on how to change the calculator: that pull request titles must follow the commit message format, and that CI must pass before merging.",
      "Every command in the README works when copied into a fresh checkout.",
    ],
  },
  {
    title: "Type numbers",
    description: [
      "Add the keys for typing a number into the display: `0` `1` `2` `3` `4` `5` `6` `7` `8` `9` and `.`. Add them to the page as buttons labelled exactly as written, with a display above them that is visible text. The other keys come in the next issues.",
      "Numbers on the display show at most four decimal places, and no trailing zeros. Each example starts from a freshly loaded page, since the `C` key comes in a later issue.",
    ],
    criteria: [
      "The display shows `0` when the page first opens.",
      "Pressing `7` shows `7`, and then pressing `2` shows `72`.",
      "A number does not start with a leading zero: `0` then `7` shows `7`, and `0` then `0` shows `0`.",
      "`0` `.` `5` shows `0.5`, and `3` `.` `1` `4` shows `3.14`.",
      "Pressing `.` first shows `0.`, and typing `5` after it shows `0.5`.",
      "A number only takes one point: `1` `.` `.` `5` shows `1.5`, and `1` `.` `5` `.` `2` shows `1.52`.",
      "The display shows what was typed and nothing else: no extra text or spaces.",
      "Automated tests cover each example, reloading the page before each one.",
    ],
  },
  {
    title: "Clear the display",
    description: [
      "Make the `C` key start over, so a new calculation doesn't need the page reloaded.",
    ],
    criteria: [
      "After typing `7` `2`, pressing `C` shows `0`.",
      "After pressing `C`, typing `5` shows `5`, with no leftover digits.",
      "`C` clears a number with a point, and the next number starts without one: `1` `.` `5` `C` `2` shows `2`.",
      "Pressing `C` on a display that already shows `0` leaves it at `0`.",
      "The keys for typing numbers still work as before.",
      "Automated tests cover each example.",
    ],
  },
  {
    title: "Add and subtract numbers",
    description: [
      "Make the `+`, `-` and `=` keys work, adding them to the page as buttons labelled exactly as written.",
    ],
    criteria: [
      "`1` `+` `1` `=` shows `2`, and `12` `+` `30` `=` shows `42`.",
      "`0.1` `+` `0.2` `=` shows `0.3`, and `1.5` `+` `1.25` `=` shows `2.75`.",
      "`5` `-` `3` `=` shows `2`, and `3` `-` `5` `=` shows `-2`.",
      "Pressing `+` or `-` after a result continues from that result: `1` `+` `1` `=` `+` `5` `=` shows `7`, and `1` `+` `1` `=` `-` `5` `=` shows `-3`.",
      "`C` forgets a half-finished sum: `5` `+` `C` `3` `=` shows `3`. After a result, `C` shows `0`.",
      "Automated tests cover each example.",
    ],
  },
  {
    title: "Flip a number between positive and negative",
    description: [
      "Make the `±` key flip the number on the display between positive and negative, so a calculation can start from or use a negative number.",
    ],
    criteria: [
      "`5` `±` shows `-5`, and pressing `±` again shows `5`.",
      "Typing continues after a flip: `5` `±` `3` shows `-53`.",
      "`1` `.` `5` `±` shows `-1.5`.",
      "The `±` key is added to the page as a button labelled exactly as written.",
      "`±` on `0` leaves `0`, never `-0`.",
      "`±` works on either side of a sum: `5` `+` `3` `±` `=` shows `2`, and `5` `±` `+` `3` `=` shows `-2`.",
      "`±` on a result flips it: `1` `+` `1` `=` `±` shows `-2`.",
      "Adding and subtracting still work.",
      "Automated tests cover each example.",
    ],
  },
  {
    title: "Multiply and divide numbers",
    description: [
      "Make the `×` and `÷` keys work with `=`, adding them to the page as buttons labelled exactly as written. Dividing may give a fraction; show at most four decimal places, and no trailing zeros.",
      "Dividing by zero has no answer. The calculator must say so, and carry on working afterwards.",
    ],
    criteria: [
      "`3` `×` `4` `=` shows `12`, and `7` `×` `0` `=` shows `0`.",
      "`10` `÷` `2` `=` shows `5`, `1` `÷` `4` `=` shows `0.25`, and `1` `÷` `3` `=` shows `0.3333`.",
      "`10` `÷` `0` `=` shows `Error`.",
      "After `Error`, pressing a digit starts a new number: `5` shows `5`. After `Error`, pressing `C` shows `0`.",
      "Adding and subtracting still work.",
      "Automated tests cover each example.",
    ],
  },
]

/**
 * The calculator's issues, in the order they are worked: each one depends on the one before it.
 * `description` is the paragraphs above the acceptance criteria, and `criteria` the checklist.
 */
export const issues: { title: string; description: string[]; criteria: string[] }[] = [
  {
    title: "Check commit messages and pull request titles",
    description: [
      "The project's changelog and version numbers will be built from its commit messages, so they must follow the [Conventional Commits](https://www.conventionalcommits.org/) format, like `feat: add a square root key` or `fix: show Error on divide by zero`. Every pull request from now on, including this one, needs a title in that format.",
      "When a pull request is squash-merged, GitHub uses the pull request's title as the commit message, so the title matters most. Add a GitHub Actions workflow of its own, in `.github/workflows/`, that fails when a pull request's title, or any of its commits, does not follow the format. There is no code to test yet, so this workflow only checks the messages.",
    ],
    criteria: [
      "The check runs on every pull request.",
      "A title such as `Add a square root key` fails the check, and `feat: add a square root key` passes.",
      "The pull request that adds the check has a valid title and passes.",
    ],
  },
  {
    title: "Scaffold the calculator page",
    description: [
      "Create the calculator as a small web page that works with no server, so it can be published as plain files. Choose any simple way of building it.",
      "This issue is only the look of the page; none of the keys do anything yet.",
      "The page shows a display and these keys, each labelled exactly as written: `0` `1` `2` `3` `4` `5` `6` `7` `8` `9` `.` `±` `+` `-` `×` `÷` `=` `C`. The display is visible text.",
    ],
    criteria: [
      "The page opens in a browser with the display and every key above on screen.",
      "The display shows `0` when the page first opens.",
      "There is an automated test that checks the display and every key are on the page, and one command runs the project's tests.",
    ],
  },
  {
    title: "Run the tests in CI",
    description: [
      "Run the calculator's automated tests on GitHub, so a broken change is caught before it is merged.",
      "Add a GitHub Actions workflow that runs the project's tests on every pull request and on every push to the default branch.",
    ],
    criteria: [
      "A workflow in `.github/workflows/` runs the tests on pull requests and on pushes to the default branch.",
      "The workflow fails when a test fails, and passes when they all pass.",
      "The pull request that adds it shows the workflow passing.",
    ],
  },
  {
    title: "Publish the calculator to GitHub Pages",
    description: [
      "Publish the calculator so anyone can use it at a web address.",
      "GitHub Pages is already turned on for this repository, with GitHub Actions as its source. Do not change the repository's settings. Add a workflow that publishes the calculator whenever the default branch changes.",
    ],
    criteria: [
      "A workflow in `.github/workflows/` builds the calculator and publishes it to GitHub Pages on every push to the default branch.",
      "After the pull request that adds it is merged, the calculator opens at the repository's GitHub Pages address (`https://<owner>.github.io/<repository>/`), with the display and every key on screen.",
      "The page there matches the one in the repository: the display shows `0`.",
      "Every later merge to the default branch publishes the new version.",
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
    title: "Type numbers with the digit keys",
    description: [
      "Make the digit keys `0` to `9` work, so a number can be typed into the display. The other keys come in the next issues.",
      "Each example starts from a freshly loaded page, since the `C` key comes in a later issue.",
    ],
    criteria: [
      "Pressing `7` shows `7`, and then pressing `2` shows `72`.",
      "A number does not start with a leading zero: `0` then `7` shows `7`, and `0` then `0` shows `0`.",
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
      "Pressing `C` on a display that already shows `0` leaves it at `0`.",
      "The digit keys still work as before.",
      "Automated tests cover each example.",
    ],
  },
  {
    title: "Type decimal numbers",
    description: [
      "Add a `.` key, so numbers with a fraction can be typed. The other operations come in the next issues.",
      "Numbers on the display show at most four decimal places, and no trailing zeros.",
    ],
    criteria: [
      "`0` `.` `5` shows `0.5`.",
      "`3` `.` `1` `4` shows `3.14`.",
      "Pressing `.` first shows `0.`, and typing `5` after it shows `0.5`.",
      "A number only takes one point: `1` `.` `.` `5` shows `1.5`, and `1` `.` `5` `.` `2` shows `1.52`.",
      "`C` clears a number with a point, and the next number starts without one: `1` `.` `5` `C` `2` shows `2`.",
      "Automated tests cover each example.",
    ],
  },
  {
    title: "Add numbers",
    description: [
      "Make the `+` and `=` keys work.",
    ],
    criteria: [
      "`1` `+` `1` `=` shows `2`.",
      "`12` `+` `30` `=` shows `42`.",
      "`0.1` `+` `0.2` `=` shows `0.3`, and `1.5` `+` `1.25` `=` shows `2.75`.",
      "Pressing `+` after a result continues from that result: `1` `+` `1` `=` `+` `5` `=` shows `7`.",
      "`C` forgets a half-finished sum: `5` `+` `C` `3` `=` shows `3`.",
      "After a result, `C` shows `0`.",
      "Automated tests cover each example.",
    ],
  },
  {
    title: "Subtract numbers",
    description: [
      "Make the `-` key work with `=`.",
    ],
    criteria: [
      "`5` `-` `3` `=` shows `2`.",
      "`3` `-` `5` `=` shows `-2`.",
      "Pressing `-` after a result continues from that result: `1` `+` `1` `=` `-` `5` `=` shows `-3`.",
      "Adding still works: the examples from the add issue pass.",
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
      "`±` on `0` leaves `0`, never `-0`.",
      "`±` works on either side of a sum: `5` `+` `3` `±` `=` shows `2`, and `5` `±` `+` `3` `=` shows `-2`.",
      "`±` on a result flips it: `1` `+` `1` `=` `±` shows `-2`.",
      "Adding and subtracting still work.",
      "Automated tests cover each example.",
    ],
  },
  {
    title: "Multiply numbers",
    description: [
      "Make the `×` key work with `=`.",
    ],
    criteria: [
      "`3` `×` `4` `=` shows `12`.",
      "`7` `×` `0` `=` shows `0`.",
      "Adding and subtracting still work.",
      "Automated tests cover each example.",
    ],
  },
  {
    title: "Divide numbers",
    description: [
      "Make the `÷` key work with `=`. Dividing may give a fraction; show at most four decimal places, and no trailing zeros.",
    ],
    criteria: [
      "`10` `÷` `2` `=` shows `5`.",
      "`1` `÷` `4` `=` shows `0.25`.",
      "`1` `÷` `3` `=` shows `0.3333`.",
      "Adding, subtracting and multiplying still work.",
      "Automated tests cover each example.",
    ],
  },
  {
    title: "Handle dividing by zero",
    description: [
      "Dividing by zero has no answer. The calculator must say so, and carry on working afterwards.",
    ],
    criteria: [
      "`10` `÷` `0` `=` shows `Error`.",
      "After `Error`, pressing a digit starts a new number: `5` shows `5`.",
      "After `Error`, pressing `C` shows `0`.",
      "Dividing by other numbers still works.",
      "Automated tests cover each example.",
    ],
  },
]

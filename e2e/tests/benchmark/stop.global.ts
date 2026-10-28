import { removeContainer } from "../../support/benchmark.ts"

// Whatever happened in the run, a tunnel or a daemon left running would stay reachable or keep spending, and both live in
// the container. The repository stays for a person to look at; the next run's setup clears it.
// Playwright runs this after every project, pass or fail. It is not a test, so it makes no page in the guide.
export default async function stop() {
  console.log("removed container:", await removeContainer())
}

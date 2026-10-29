// Delete everything the last benchmark run left: the repository, its Pages site and the container
// it ran in. Run it with `npm run oe-benchmark:clean` once you have looked.
import { teardown } from "../support/benchmark.ts"

console.log(await teardown())

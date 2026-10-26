// Delete everything the last benchmark run left: the repository, its Pages site, the local folder
// and the processes it recorded. Run it with `npm run oe-benchmark:clean` once you have looked.
import { teardown } from "../support/benchmark.ts"

console.log(await teardown())

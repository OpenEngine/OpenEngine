import { readdirSync, readFileSync } from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { test, expect } from "@playwright/test"

// The generated guides are committed (see README): every guide spec has its
// page under site/docs/guides, so a reviewer reads the page itself.

const tests = path.dirname(fileURLToPath(import.meta.url))
const guides = path.resolve(tests, "../../site/docs/guides")

function files(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory() ? files(path.join(directory, entry.name)) : [path.join(directory, entry.name)],
  )
}

/** Every guide a spec declares, by its title and slug. */
const declared = readdirSync(tests)
  .filter((name) => name.endsWith(".spec.ts"))
  .flatMap((spec) =>
    [...readFileSync(path.join(tests, spec), "utf8").matchAll(/withDocMeta\(\s*"([^"]+)",\s*\{[^}]*?slug:\s*"([^"]+)"/g)].map(
      ([, title, slug]) => ({ spec, title: title!, slug: slug! }),
    ),
  )

test.describe("committed guides", { tag: ["@skip-docs"] }, () => {
  test("finds the guide specs", () => {
    expect(declared.map((guide) => guide.title)).toEqual(
      expect.arrayContaining(["Before You Start", "Getting Help", "Use OpenEngine on Another Computer"]),
    )
  })

  for (const { spec, title, slug } of declared) {
    test(`${spec}: "${title}" has its generated page committed`, () => {
      const page = files(guides)
        .filter((file) => file.endsWith(".mdx"))
        .find((file) => readFileSync(file, "utf8").match(/^slug: (.+)$/m)?.[1] === slug)
      expect(page, `no page with slug ${slug} under site/docs/guides; run \`npm run doc\` and commit it`).toBeDefined()
      expect(readFileSync(page!, "utf8")).toContain(`# ${title}`)
    })
  }
})

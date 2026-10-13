import { readdirSync, readFileSync } from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { test, expect } from "@playwright/test"

// The generated guides are committed (see README): every guide spec has its
// page under site/docs/guides, so a reviewer reads it in the pull request.

const tests = path.dirname(fileURLToPath(import.meta.url))
const guides = path.resolve(tests, "../../site/docs/guides")

function files(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory() ? files(path.join(directory, entry.name)) : [path.join(directory, entry.name)],
  )
}

const pages = readdirSync(tests)
  .filter((name) => name.endsWith(".spec.ts"))
  .flatMap((name) =>
    [...readFileSync(path.join(tests, name), "utf8").matchAll(/withDocMeta\(\s*"([^"]+)",\s*\{[^}]*?slug:\s*"([^"]+)"/g)].map(
      ([, title, slug]) => ({ spec: name, title: title!, slug: slug! }),
    ),
  )

test.describe("committed guides", { tag: ["@skip-docs"] }, () => {
  test("finds the guide specs", () => {
    expect(pages.map((page) => page.title)).toEqual(expect.arrayContaining(["Before You Start", "Getting Help"]))
  })

  for (const { spec, title, slug } of pages) {
    test(`${spec}: "${title}" has its generated page committed`, () => {
      const committed = files(guides).filter((file) => file.endsWith(".mdx"))
      const page = committed.find((file) => readFileSync(file, "utf8").match(/^slug: (.+)$/m)?.[1] === slug)
      expect(page, `no page with slug ${slug} under site/docs/guides; run \`npm run doc\` and commit it`).toBeDefined()
      expect(readFileSync(page!, "utf8")).toContain(`# ${title}`)
    })
  }
})

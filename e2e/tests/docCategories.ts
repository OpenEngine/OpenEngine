import { withDocCategory } from "@test2doc/playwright/DocMeta"

// Every guide category lives here so that positions stay unique and a category
// split across several spec files can't drift apart on label or description.
// Positions follow the order a new user meets the CLI.

export const gettingStartedCategory = withDocCategory("Getting Started", {
  label: "Getting Started",
  position: 1,
  link: {
    type: "generated-index",
    slug: "/guides/getting-started",
    description: "What OpenEngine is, what you need, and how to find your way around the engine command.",
  },
})

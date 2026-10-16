import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Markdown } from "./markdown";

describe("Markdown", () => {
  it("renders emphasis, inline code, a bulleted list and a fenced code block", () => {
    const { container } = render(<Markdown>{[
      "Made **the change** with `npm test` passing.",
      "",
      "- first",
      "- second",
      "",
      "```",
      "const answer = 42;",
      "```",
    ].join("\n")}</Markdown>);

    expect(container.querySelector("strong")).toHaveTextContent("the change");
    const inline = [...container.querySelectorAll("code")].filter((code) => !code.closest("pre"));
    expect(inline.map((code) => code.textContent)).toEqual(["npm test"]);
    const list = screen.getByRole("list");
    expect(list.tagName).toBe("UL");
    expect(within(list).getAllByRole("listitem").map((item) => item.textContent)).toEqual(["first", "second"]);
    expect(container.querySelector("pre > code")).toHaveTextContent("const answer = 42;");
    expect(container).not.toHaveTextContent("**");
    expect(container).not.toHaveTextContent("```");
  });

  it("keeps plain text as it reads today, single line breaks included", () => {
    const { container } = render(<Markdown>{"Planned the work.\nOpened a branch."}</Markdown>);

    expect(container).toHaveTextContent(/^Planned the work\.\s*Opened a branch\.$/);
    expect(container.querySelectorAll("p")).toHaveLength(1);
    // A single newline is a break the reader sees, not one Markdown joins away.
    const paragraph = container.querySelector("p")!;
    expect(paragraph.querySelectorAll("br")).toHaveLength(1);
    expect(paragraph.firstChild?.textContent).toBe("Planned the work.");
    expect(container.querySelector("strong, em, code, ul, ol, a, h1, h2, h3")).toBeNull();
  });

  it.each([
    "<script>alert(1)</script>",
    '<img src="x" onerror="alert(1)">',
    'Before <img src="x" onerror="alert(1)"> after',
  ])("shows raw HTML %s as text rather than inserting it", (html) => {
    const { container } = render(<Markdown>{html}</Markdown>);

    expect(container).toHaveTextContent(html);
    expect(container.querySelector("script, img, [onerror]")).toBeNull();
  });

  it.each([
    ["https://example.com/docs"],
    ["http://example.com/docs"],
    ["mailto:team@example.com"],
  ])("links to %s in a new tab", (href) => {
    render(<Markdown>{`See [the docs](${href}).`}</Markdown>);

    const link = screen.getByRole("link", { name: "the docs" });
    expect(link).toHaveAttribute("href", href);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it.each([
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "vbscript:msgbox(1)",
    "data:text/html,unsafe",
  ])("does not make a %s link clickable", (href) => {
    const { container } = render(<Markdown>{`See [the docs](${href}).`}</Markdown>);

    expect(container).toHaveTextContent("the docs");
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(container.querySelector("[href]")).toBeNull();
  });

  it("renders nothing for an empty source", () => {
    const { container } = render(<Markdown>{""}</Markdown>);

    // At most an empty container: no text and no content inside it.
    expect(container.textContent).toBe("");
    expect(container.querySelector("p, ul, ol, li, pre, code, strong, em, a, br, img, h1, h2, h3")).toBeNull();
  });

  // jsdom has no layout, so whether a long line wraps is the stylesheet's to
  // show; what a test can hold is that nothing renders outside its host.
  it("keeps a long line and a wide code block inside the element it is given", () => {
    const long = "x".repeat(400);
    const { container } = render(
      <section data-testid="host">
        <Markdown>{`${long}\n\n\`\`\`\n${long}\n\`\`\``}</Markdown>
      </section>,
    );

    const host = screen.getByTestId("host");
    expect(host.querySelector("p")).toHaveTextContent(long);
    expect(host.querySelector("pre")).toHaveTextContent(long);
    expect(container.children).toHaveLength(1);
  });
});

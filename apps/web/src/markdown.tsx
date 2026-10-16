import type { AnchorHTMLAttributes, ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import { visit } from "unist-util-visit";
import type { Root, RootContent } from "mdast";

/** A summary is model output, not the app's own markup: raw HTML in the
 *  source is never inserted. Its node is replaced by the same text, which
 *  the renderer then shows escaped, as it reads. */
const rawHtmlAsText = () => (tree: Root) => {
  visit(tree, (node) => node.type === "html", (node, index, parent) => {
    if (index == null || !parent) return;
    const html = node as unknown as { value: string };
    (parent as unknown as { children: RootContent[] }).children[index] = {
      type: "text",
      value: html.value,
    };
  });
};

const SAFE_LINK_PROTOCOLS = new Set(["http:", "https:", "mailto:"]);

/** The protocols a summary link may use. Everything else — javascript:,
 *  data:, vbscript: — stays readable text rather than a click. */
function safeHref(href: unknown): string | null {
  if (typeof href !== "string") return null;
  const value = href.trim();
  if (!value) return null;
  try {
    // Relative targets resolve against the page and keep their scheme free.
    const url = new URL(value, "https://markdown.invalid/");
    return SAFE_LINK_PROTOCOLS.has(url.protocol) ? value : null;
  } catch {
    return null;
  }
}

function link({ href, children, node: _node, ...rest }: AnchorHTMLAttributes<HTMLAnchorElement> & { node?: unknown }): ReactNode {
  const safe = safeHref(href);
  if (safe === null) return <span>{children}</span>;
  return (
    <a {...rest} href={safe} rel="noopener noreferrer" target="_blank">
      {children}
    </a>
  );
}

/** Renders a Markdown source as formatted text. Single line breaks stay
 *  visible, raw HTML shows as text, and links only open in a new tab when
 *  their protocol is safe. */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown remarkPlugins={[remarkBreaks, rawHtmlAsText]} components={{ a: link }}>
        {children}
      </ReactMarkdown>
    </div>
  );
}

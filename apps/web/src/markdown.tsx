import { memo } from "react";
import type { AnchorHTMLAttributes, ImgHTMLAttributes, ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";

const SAFE_LINK_PROTOCOLS = new Set(["http:", "https:", "mailto:"]);

/** The protocols a summary link may use. A value with no scheme is relative
 *  to the page, or to another origin when it starts with //, so only an
 *  explicit, listed scheme opens; everything else — javascript:, data:,
 *  vbscript:, a path — stays readable text rather than a click. */
function safeHref(href: unknown): string | null {
  if (typeof href !== "string") return null;
  const value = href.trim();
  if (!value) return null;
  try {
    // No base: a scheme-less value throws, which is what keeps it unclickable.
    const url = new URL(value);
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

// A summary is model output: an image in it would make the viewer's browser
// fetch an address the model was told to name, so no real <img> ever renders.
// The picture's alt text -- or the address itself, when there is no alt -- is
// kept as readable text, so the summary does not lose a piece of itself.
function image({ alt, src, node: _node }: ImgHTMLAttributes<HTMLImageElement> & { node?: unknown }): ReactNode {
  const label = alt?.trim() || (typeof src === "string" ? src.trim() : "") || null;
  return label === null ? null : <span>{label}</span>;
}

// Hoisted so a re-render of the component does not rebuild them.
const remarkPlugins = [remarkBreaks];
const components = { a: link, img: image };

/** Renders a Markdown source as formatted text. Single line breaks stay
 *  visible, raw HTML shows as text, images show as their alt text, and links
 *  only open in a new tab when their protocol is safe. */
export const Markdown = memo(function Markdown({ children }: { children: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown remarkPlugins={remarkPlugins} components={components}>
        {children}
      </ReactMarkdown>
    </div>
  );
});

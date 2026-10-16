import { memo } from "react";
import MarkdownRenderer from "react-markdown";
import type { Components } from "react-markdown";
import remarkBreaks from "remark-breaks";

const SAFE_HREF = /^(?:https?:\/\/|mailto:)/i;

function safeHref(href: string | undefined): string | null {
  if (typeof href !== "string" || href.length === 0) {
    return null;
  }
  if (!SAFE_HREF.test(href)) {
    return null;
  }
  return href;
}

const components: Components = {
  a: ({ href, children }) => {
    const safe = safeHref(href);
    if (safe === null) {
      return <>{children}</>;
    }
    return (
      <a href={safe} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    );
  },
  // An image would make the viewer's browser fetch whatever URL the agent
  // wrote, leaking the data in that URL along with the viewer's IP.
  img: ({ alt }) => (alt ? <>{alt}</> : null),
};

export const Markdown = memo(function Markdown({
  children,
}: {
  children?: string | null;
}) {
  if (typeof children !== "string" || children.trim() === "") {
    return null;
  }
  return (
    <div className="markdown">
      <MarkdownRenderer
        remarkPlugins={[remarkBreaks]}
        components={components}
      >
        {children}
      </MarkdownRenderer>
    </div>
  );
});

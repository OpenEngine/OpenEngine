import { memo } from "react";
import MarkdownRenderer from "react-markdown";
import type { Components } from "react-markdown";
import remarkBreaks from "remark-breaks";

// A summary is what an agent wrote, so its links are the agent's word rather
// than the engine's. A browser reads URL schemes case-insensitively, so the
// allowlist must be too, and only a web page or an e-mail address may be
// clicked.
const SAFE_HREF = /^(?:https?:\/\/|mailto:)/i;

const components: Components = {
  a: ({ href, children }) => {
    if (typeof href !== "string" || !SAFE_HREF.test(href)) {
      return <>{children}</>;
    }
    return (
      <a href={href} target="_blank" rel="noopener noreferrer">
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
  className,
}: {
  children?: string | null;
  className?: string;
}) {
  if (typeof children !== "string" || children.trim() === "") {
    return null;
  }
  return (
    <div className={className ? `markdown ${className}` : "markdown"}>
      <MarkdownRenderer
        remarkPlugins={[remarkBreaks]}
        components={components}
      >
        {children}
      </MarkdownRenderer>
    </div>
  );
});

import type { HTMLAttributes } from "react";

interface UntrustedTextProps extends Omit<HTMLAttributes<HTMLElement>, "children"> {
  as?: "span" | "p" | "pre";
  children: string;
}

/** Render captured/model-derived text as inert text with bidi isolation. */
export function UntrustedText({
  as: Element = "span",
  children,
  className = "",
  ...props
}: UntrustedTextProps) {
  return (
    <Element className={`untrusted-text ${className}`.trim()} {...props}>
      <bdi dir="auto">{children}</bdi>
    </Element>
  );
}

/* SPDX-License-Identifier: Apache-2.0 */
// SPDX-License-Identifier: Apache-2.0
import type { CSSProperties } from "react";

export const BRAND_NAME = "LM Chat";

interface BrandMarkProps {
  size: number;
  style?: CSSProperties;
  className?: string;
}

/**
 * LM Chat mark — five stacked copper bars, echoing LM Studio's stacked-layer
 * visual language. Fill resolves from --color-brand so it tracks the theme.
 *
 * Brass glow on hover via box-shadow on the wrapping element
 * (applied by the parent via .atelier-brand or .lmchat-brand-link CSS classes).
 * The SVG itself gets a subtle filter brightness lift via .atelier-brand:hover svg.
 */
export function BrandMark({ size, style, className }: BrandMarkProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 512 512"
      aria-hidden="true"
      className={className}
      style={{
        flexShrink: 0,
        transition: `filter var(--duration-fast) var(--ease-out-quart)`,
        ...style,
      }}
    >
      <g transform="translate(256,256)" fill="var(--color-brand)">
        <rect x="-10" y="-160" width="200" height="36" rx="18" />
        <rect x="-140" y="-90" width="280" height="36" rx="18" />
        <rect x="-100" y="-20" width="240" height="36" rx="18" />
        <rect x="-150" y="50" width="270" height="36" rx="18" />
        <rect x="-30" y="120" width="180" height="36" rx="18" />
      </g>
    </svg>
  );
}

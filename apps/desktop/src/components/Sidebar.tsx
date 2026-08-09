import type { PageId } from "../contracts";

interface SidebarProps {
  current: PageId;
  reviewCount: number;
  suggestionCount: number;
  promptRescueCount: number;
  onNavigate: (page: PageId) => void;
}

const pages: Array<{ id: PageId; label: string; glyph: string }> = [
  { id: "overview", label: "Overview", glyph: "O" },
  { id: "suggestions", label: "Suggestions", glyph: "S" },
  { id: "prompt-rescue", label: "Prompt Rescue", glyph: "P" },
  { id: "review", label: "Review", glyph: "R" },
  { id: "daily-wrap", label: "Daily Wrap", glyph: "D" },
  { id: "timeline", label: "Timeline", glyph: "T" },
  { id: "privacy", label: "Privacy", glyph: "P" },
];

export function Sidebar({
  current,
  reviewCount,
  suggestionCount,
  promptRescueCount,
  onNavigate,
}: SidebarProps) {
  return (
    <aside className="sidebar" aria-label="Primary">
      <div className="brand" aria-label="OpenChronicle trusted console">
        <span className="brand__mark" aria-hidden="true">
          OC
        </span>
        <span>
          <strong>OpenChronicle</strong>
          <small>Trusted console</small>
        </span>
      </div>
      <nav className="sidebar__nav">
        {pages.map((page) => (
          <button
            aria-current={current === page.id ? "page" : undefined}
            className="sidebar__item"
            key={page.id}
            onClick={() => onNavigate(page.id)}
            type="button"
          >
            <span className="sidebar__glyph" aria-hidden="true">
              {page.glyph}
            </span>
            <span>{page.label}</span>
            {page.id === "review" && reviewCount > 0 ? (
              <span className="sidebar__count" aria-label={`${reviewCount} need review`}>
                {reviewCount}
              </span>
            ) : null}
            {page.id === "suggestions" && suggestionCount > 0 ? (
              <span className="sidebar__count" aria-label={`${suggestionCount} suggestions`}>
                {suggestionCount}
              </span>
            ) : null}
            {page.id === "prompt-rescue" && promptRescueCount > 0 ? (
              <span
                className="sidebar__count"
                aria-label={`${promptRescueCount} Prompt Rescue jobs`}
              >
                {promptRescueCount}
              </span>
            ) : null}
          </button>
        ))}
      </nav>
      <p className="sidebar__boundary">
        Memory-only alpha
        <br />
        No external actions
      </p>
    </aside>
  );
}

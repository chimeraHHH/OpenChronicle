interface StatusBadgeProps {
  tone: "positive" | "warning" | "danger" | "neutral" | "info";
  children: string;
}

export function StatusBadge({ tone, children }: StatusBadgeProps) {
  return (
    <span className={`status-badge status-badge--${tone}`}>
      <span aria-hidden="true" className="status-badge__dot" />
      {children}
    </span>
  );
}

import type { DesktopSnapshot } from "../contracts";
import { titleCase } from "../format";
import { StatusBadge } from "../components/StatusBadge";
import { UntrustedText } from "../components/UntrustedText";

interface PrivacyPageProps {
  snapshot: DesktopSnapshot;
}

function permissionTone(state: string) {
  if (state === "granted") return "positive" as const;
  if (state === "not_determined" || state === "unknown") return "neutral" as const;
  return "danger" as const;
}

function RuleList({ values, empty }: { values: string[]; empty: string }) {
  if (values.length === 0) return <p className="muted">{empty}</p>;
  return (
    <ul className="token-list">
      {values.map((value) => (
        <li key={value}><UntrustedText>{value}</UntrustedText></li>
      ))}
    </ul>
  );
}

export function PrivacyPage({ snapshot }: PrivacyPageProps) {
  const { privacy } = snapshot;
  return (
    <main className="page" id="main-content" tabIndex={-1}>
      <header className="page-header">
        <div>
          <p className="eyebrow">Trusted boundary</p>
          <h1>Privacy & permissions</h1>
          <p>Read the effective local policy. This alpha does not silently change capture scope.</p>
        </div>
        <StatusBadge tone={privacy.model_mode === "local-only" ? "positive" : "warning"}>
          {privacy.model_mode}
        </StatusBadge>
      </header>

      <section className="settings-section" aria-labelledby="permission-heading">
        <h2 id="permission-heading">System permissions</h2>
        <div className="settings-list">
          {snapshot.permissions.length === 0 ? (
            <p className="muted">Permission status is not available from the local service.</p>
          ) : (
            snapshot.permissions.map((permission) => (
              <div className="settings-row" key={permission.kind}>
                <div>
                  <strong><UntrustedText>{permission.label}</UntrustedText></strong>
                  <small>{permission.required ? "Required for configured capture" : "Optional"}</small>
                </div>
                <StatusBadge tone={permissionTone(permission.state)}>
                  {titleCase(permission.state)}
                </StatusBadge>
              </div>
            ))
          )}
        </div>
      </section>

      <section className="settings-section" aria-labelledby="scope-heading">
        <div className="section-heading-row">
          <div>
            <h2 id="scope-heading">Prospective capture scope</h2>
            <p>Exclusions apply before future AX, screenshot, persistence, indexing, and model use.</p>
          </div>
          {privacy.policy_version ? (
            <span className="technical-label">Policy <bdi>{privacy.policy_version}</bdi></span>
          ) : null}
        </div>
        <div className="rule-grid">
          <div>
            <h3>Allowed bundle IDs</h3>
            <RuleList values={privacy.allowed_bundle_ids} empty="No strict allowlist is configured." />
          </div>
          <div>
            <h3>Excluded applications</h3>
            <RuleList values={privacy.excluded_app_names} empty="No app-name exclusions." />
          </div>
          <div>
            <h3>Excluded bundle IDs</h3>
            <RuleList values={privacy.excluded_bundle_ids} empty="No bundle-ID exclusions." />
          </div>
          <div>
            <h3>Excluded title patterns</h3>
            <RuleList values={privacy.excluded_window_title_patterns} empty="No title-pattern exclusions." />
          </div>
        </div>
        <p className="boundary-note">
          Changing or adding an exclusion does not retroactively erase existing captures, timeline
          blocks, review history, or provider copies.
        </p>
      </section>

      <section className="settings-section" aria-labelledby="egress-heading">
        <h2 id="egress-heading">Model and data egress</h2>
        <dl className="definition-grid">
          <dt>Mode</dt>
          <dd>{privacy.model_mode}</dd>
          <dt>Provider</dt>
          <dd><bdi>{privacy.model_provider || (privacy.model_mode === "local-only" ? "Local model" : "Unknown")}</bdi></dd>
          <dt>Scheduled Daily Wrap</dt>
          <dd>{privacy.daily_wrap_enabled ? "Enabled" : "Disabled"}</dd>
          <dt>Screenshots</dt>
          <dd>{privacy.include_screenshot ? "Enabled" : "Off"}</dd>
          <dt>Unknown windows</dt>
          <dd>{privacy.deny_unknown_windows ? "Denied (fail closed)" : "May be captured"}</dd>
          <dt>Capture buffer retention</dt>
          <dd>{privacy.buffer_retention_hours === undefined ? "Not reported" : `${privacy.buffer_retention_hours} hours`}</dd>
          <dt>Screenshot retention</dt>
          <dd>{privacy.screenshot_retention_hours === undefined ? "Not reported" : `${privacy.screenshot_retention_hours} hours`}</dd>
        </dl>
        {privacy.include_screenshot ? (
          <p className="danger-panel" role="alert">
            Screenshots are enabled. The current capture implementation may include the primary
            display, not only the verified foreground window.
          </p>
        ) : (
          <p className="success-panel">Screenshots are off; Screen Recording permission is not needed.</p>
        )}
      </section>
    </main>
  );
}

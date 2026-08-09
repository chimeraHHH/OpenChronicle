import { useEffect, useMemo, useState } from "react";

import type { DesktopApi } from "../api";
import { StatusBadge } from "../components/StatusBadge";
import { UntrustedText } from "../components/UntrustedText";
import type {
  ResumeConfidentiality,
  ResumeFact,
  ResumeOpportunity,
  ResumeOwnership,
  ResumeProfile,
  ResumeProfileVersion,
  ResumePreview,
  ResumeProjection,
  ResumeRequirementRequest,
  ResumeRescueState,
  ResumeSectionKind,
} from "../contracts";
import { displayError, formatDateTime } from "../format";

const sectionOrder: ResumeSectionKind[] = [
  "summary",
  "experience",
  "education",
  "skill",
  "project",
  "certification",
  "language",
  "other",
];

interface ResumeRescuePageProps {
  api: DesktopApi;
}

function newProfileDraft(): ResumeProfile {
  return {
    schema_version: 1,
    profile_id: "",
    display_name: "",
    locale: "",
    facts: [],
    conflicts: [],
  };
}

export function ResumeRescuePage({ api }: ResumeRescuePageProps) {
  const [state, setState] = useState<ResumeRescueState | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [profileDraft, setProfileDraft] = useState<ResumeProfile>(newProfileDraft);
  const [profileVersion, setProfileVersion] = useState<number | undefined>();
  const [factId, setFactId] = useState("");
  const [factSection, setFactSection] = useState<ResumeSectionKind>("experience");
  const [factText, setFactText] = useState("");
  const [factConfidentiality, setFactConfidentiality] =
    useState<ResumeConfidentiality>("private");
  const [factOwnership, setFactOwnership] = useState<ResumeOwnership>("individual");
  const [employer, setEmployer] = useState("");
  const [title, setTitle] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [sourceText, setSourceText] = useState("");
  const [priorities, setPriorities] = useState("");
  const [opportunityLocale, setOpportunityLocale] = useState("");
  const [replacement, setReplacement] = useState<ResumeOpportunity | null>(null);
  const [selectedProfileId, setSelectedProfileId] = useState("");
  const [selectedOpportunityId, setSelectedOpportunityId] = useState("");
  const [selectedFactIds, setSelectedFactIds] = useState<string[]>([]);
  const [requirementText, setRequirementText] = useState("");
  const [requirements, setRequirements] = useState<ResumeRequirementRequest[]>([]);
  const [preview, setPreview] = useState<ResumePreview | null>(null);

  async function refresh() {
    const next = await api.getResumeRescueState();
    setState(next);
    setSelectedProfileId((current) =>
      next.profiles.some((profile) => profile.id === current)
        ? current
        : (next.profiles[0]?.id ?? ""),
    );
    setSelectedOpportunityId((current) =>
      next.opportunities.some((opportunity) => opportunity.id === current)
        ? current
        : (next.opportunities[0]?.id ?? ""),
    );
  }

  useEffect(() => {
    let current = true;
    setBusy("load");
    void api
      .getResumeRescueState()
      .then((next) => {
        if (!current) return;
        setState(next);
        setSelectedProfileId(next.profiles[0]?.id ?? "");
        setSelectedOpportunityId(next.opportunities[0]?.id ?? "");
      })
      .catch((reason: unknown) => current && setError(displayError(reason)))
      .finally(() => current && setBusy(""));
    return () => {
      current = false;
    };
  }, [api]);

  const selectedProfile = state?.profiles.find((profile) => profile.id === selectedProfileId);
  const selectedOpportunity = state?.opportunities.find(
    (opportunity) => opportunity.id === selectedOpportunityId,
  );
  const conflictedFactIds = useMemo(
    () =>
      new Set(
        selectedProfile?.profile.conflicts.flatMap((conflict) => conflict.fact_ids) ?? [],
      ),
    [selectedProfile],
  );
  const selectableFacts =
    selectedProfile?.profile.facts.filter((fact) => !conflictedFactIds.has(fact.id)) ?? [];
  const selectedFacts = selectableFacts.filter((fact) => selectedFactIds.includes(fact.id));

  function clearMessages() {
    setError("");
    setNotice("");
  }

  function addFact() {
    clearMessages();
    if (!factId.trim() || !factText.trim()) {
      setError("Enter a stable fact ID and reviewed fact text.");
      return;
    }
    if (profileDraft.facts.some((fact) => fact.id === factId.trim())) {
      setError("Fact IDs must be unique within the profile.");
      return;
    }
    const fact: ResumeFact = {
      id: factId.trim(),
      section: factSection,
      text: factText.trim(),
      confidentiality: factConfidentiality,
      ownership_scope: factOwnership,
      provenance: [{ kind: "manual_reviewed", reviewed_at: new Date().toISOString() }],
    };
    setProfileDraft((current) => ({ ...current, facts: [...current.facts, fact] }));
    setFactId("");
    setFactText("");
  }

  async function saveProfile() {
    clearMessages();
    setBusy("profile");
    try {
      const result = await api.saveResumeProfile(profileDraft, profileVersion);
      setNotice(
        result.created
          ? `Saved reviewed profile version ${result.profile.version}.`
          : "The reviewed profile already matched the current version.",
      );
      setProfileDraft(newProfileDraft());
      setProfileVersion(undefined);
      setSelectedFactIds([]);
      setRequirements([]);
      await refresh();
      setSelectedProfileId(result.profile.id);
    } catch (reason: unknown) {
      setError(displayError(reason));
    } finally {
      setBusy("");
    }
  }

  function editProfile(profile: ResumeProfileVersion) {
    clearMessages();
    setProfileDraft({
      ...profile.profile,
      facts: profile.profile.facts.map((fact) => ({
        ...fact,
        provenance: [...fact.provenance],
      })),
      conflicts: profile.profile.conflicts.map((conflict) => ({
        ...conflict,
        fact_ids: [...conflict.fact_ids],
      })),
    });
    setProfileVersion(profile.version);
    setNotice(
      "Loaded the current profile version. Saving uses compare-and-swap and invalidates stale projections.",
    );
  }

  function clearOpportunity() {
    setEmployer("");
    setTitle("");
    setSourceUrl("");
    setSourceText("");
    setPriorities("");
    setOpportunityLocale("");
    setReplacement(null);
  }

  async function saveOpportunity() {
    clearMessages();
    setBusy("opportunity");
    const source = {
      schema_version: 1 as const,
      employer: employer.trim(),
      title: title.trim(),
      source_url: sourceUrl.trim(),
      source_text: sourceText,
      priorities: priorities
        .split("\n")
        .map((value) => value.trim())
        .filter(Boolean),
      locale: opportunityLocale.trim(),
      captured_at: new Date().toISOString(),
    };
    try {
      const result = replacement
        ? await api.replaceResumeOpportunity(replacement.id, replacement.digest, source)
        : await api.saveResumeOpportunity(source);
      setNotice(
        result.created
          ? replacement
            ? "Saved a superseding opportunity snapshot. The earlier snapshot remains immutable."
            : "Saved an immutable opportunity snapshot."
          : "That exact opportunity snapshot already exists.",
      );
      clearOpportunity();
      setRequirements([]);
      await refresh();
      setSelectedOpportunityId(result.opportunity.id);
    } catch (reason: unknown) {
      setError(displayError(reason));
    } finally {
      setBusy("");
    }
  }

  function replaceOpportunity(opportunity: ResumeOpportunity) {
    clearMessages();
    setReplacement(opportunity);
    setEmployer(opportunity.snapshot.employer);
    setTitle(opportunity.snapshot.title);
    setSourceUrl(opportunity.snapshot.source_url);
    setSourceText(opportunity.snapshot.source_text);
    setPriorities(opportunity.snapshot.priorities.join("\n"));
    setOpportunityLocale(opportunity.snapshot.locale);
    setNotice("Loaded the current snapshot. Saving creates a digest-bound superseding snapshot.");
  }

  function toggleFact(factId: string) {
    setSelectedFactIds((current) =>
      current.includes(factId)
        ? current.filter((value) => value !== factId)
        : [...current, factId],
    );
    setRequirements((current) =>
      current.map((requirement) => ({
        ...requirement,
        fact_ids: requirement.fact_ids.filter((value) => value !== factId),
      })),
    );
  }

  function addRequirement() {
    clearMessages();
    const text = requirementText.trim();
    if (!text || !selectedOpportunity?.snapshot.source_text.includes(text)) {
      setError("Paste an exact, non-empty excerpt from the selected opportunity source.");
      return;
    }
    if (requirements.some((requirement) => requirement.text === text)) {
      setError("That exact requirement excerpt is already in the review list.");
      return;
    }
    setRequirements((current) => {
      let index = 1;
      while (current.some((requirement) => requirement.id === `req-${index}`)) index += 1;
      return [...current, { id: `req-${index}`, text, fact_ids: [] }];
    });
    setRequirementText("");
  }

  function toggleRequirementFact(requirementId: string, factId: string) {
    setRequirements((current) =>
      current.map((requirement) =>
        requirement.id !== requirementId
          ? requirement
          : {
              ...requirement,
              fact_ids: requirement.fact_ids.includes(factId)
                ? requirement.fact_ids.filter((value) => value !== factId)
                : [...requirement.fact_ids, factId],
            },
      ),
    );
  }

  async function compose() {
    if (!selectedProfile || !selectedOpportunity) return;
    clearMessages();
    setBusy("compose");
    const sections = sectionOrder
      .map((kind) => ({
        kind,
        fact_ids: selectedFacts.filter((fact) => fact.section === kind).map((fact) => fact.id),
      }))
      .filter((section) => section.fact_ids.length > 0);
    try {
      const result = await api.composeResumeExact(
        selectedProfile.id,
        selectedOpportunity.id,
        sections,
        requirements,
      );
      setNotice(
        result.created
          ? "Created an exact, review-only résumé projection. Nothing was uploaded or submitted."
          : "This exact projection already exists; the existing immutable result was reused.",
      );
      await refresh();
    } catch (reason: unknown) {
      setError(displayError(reason));
    } finally {
      setBusy("");
    }
  }

  async function openPreview(projection: ResumeProjection) {
    clearMessages();
    setBusy(`preview:${projection.id}`);
    try {
      setPreview(await api.getResumePreview(projection.id, projection.artifact_digest));
      window.requestAnimationFrame(() =>
        document.getElementById("resume-document-preview")?.scrollIntoView({ block: "start" }),
      );
    } catch (reason: unknown) {
      setError(displayError(reason));
      setPreview(null);
    } finally {
      setBusy("");
    }
  }

  async function exportPreview() {
    if (!preview) return;
    clearMessages();
    setBusy("export");
    try {
      const result = await api.exportResumeHtml(
        preview.projection_id,
        preview.document_digest,
      );
      setNotice(
        `Created ${result.file_name} (${result.byte_count} bytes). No existing file was replaced.`,
      );
    } catch (reason: unknown) {
      setError(displayError(reason));
    } finally {
      setBusy("");
    }
  }

  if (!state && busy === "load") {
    return <main className="page" id="main-content" tabIndex={-1}><p role="status">Loading local Résumé Rescue sources…</p></main>;
  }

  return (
    <main className="page resume-rescue" id="main-content" tabIndex={-1}>
      <header className="page-header">
        <div>
          <p className="eyebrow">Evidence first, never submitted</p>
          <h1>Résumé Rescue</h1>
          <p>
            Keep reviewed career facts and job text local, then assemble an exact projection.
            OpenChronicle does not invent claims, score ATS compatibility, upload files, or apply.
          </p>
        </div>
        <StatusBadge tone={state?.enabled ? "positive" : "neutral"}>
          {state?.enabled ? "Opted in" : "Off"}
        </StatusBadge>
      </header>

      <section className="info-panel">
        <h2>Deterministic safety boundary</h2>
        <p>
          Every displayed line is copied exactly from a selected reviewed fact. Requirement
          mappings are manual and unverified; missing evidence stays visible. The artifact has
          <code> action_capability: none</code>.
        </p>
      </section>

      {!state?.enabled ? (
        <section className="empty-panel">
          <h2>Résumé Rescue is off</h2>
          <p>Enable <code>[resume_rescue]</code> only after reviewing the local evidence contract.</p>
        </section>
      ) : null}

      {error ? <div className="global-error" role="alert"><UntrustedText>{error}</UntrustedText></div> : null}
      {notice ? <div className="success-panel" role="status">{notice}</div> : null}

      <div className="resume-rescue__source-grid">
        <section className="settings-section" aria-labelledby="resume-profile-heading">
          <div className="section-heading-row">
            <div><p className="eyebrow">Reviewed evidence vault</p><h2 id="resume-profile-heading">Profile facts</h2></div>
            {profileVersion ? <StatusBadge tone="info">{`Editing v${profileVersion}`}</StatusBadge> : null}
          </div>
          <div className="prompt-rescue__context-grid">
            <label className="field"><span>Stable profile ID</span><input maxLength={128} onChange={(event) => setProfileDraft((current) => ({ ...current, profile_id: event.currentTarget.value }))} value={profileDraft.profile_id} /></label>
            <label className="field"><span>Display name</span><input maxLength={512} onChange={(event) => setProfileDraft((current) => ({ ...current, display_name: event.currentTarget.value }))} value={profileDraft.display_name} /></label>
            <label className="field"><span>Locale (optional)</span><input maxLength={64} onChange={(event) => setProfileDraft((current) => ({ ...current, locale: event.currentTarget.value }))} value={profileDraft.locale} /></label>
          </div>
          <div className="resume-rescue__fact-editor">
            <label className="field"><span>Fact ID</span><input maxLength={128} onChange={(event) => setFactId(event.currentTarget.value)} placeholder="fact-api-latency" value={factId} /></label>
            <label className="field"><span>Section</span><select onChange={(event) => setFactSection(event.currentTarget.value as ResumeSectionKind)} value={factSection}>{sectionOrder.map((section) => <option key={section} value={section}>{section}</option>)}</select></label>
            <label className="field"><span>Confidentiality</span><select onChange={(event) => setFactConfidentiality(event.currentTarget.value as ResumeConfidentiality)} value={factConfidentiality}><option value="public">public</option><option value="private">private</option><option value="confidential">confidential</option></select></label>
            <label className="field"><span>Ownership</span><select onChange={(event) => setFactOwnership(event.currentTarget.value as ResumeOwnership)} value={factOwnership}><option value="individual">individual</option><option value="shared">shared</option><option value="organization">organization</option><option value="unspecified">unspecified</option></select></label>
            <label className="field field--wide"><span>Reviewed fact text</span><textarea maxLength={8_000} onChange={(event) => setFactText(event.currentTarget.value)} rows={4} value={factText} /></label>
            <button className="button button--secondary" disabled={!state?.enabled} onClick={addFact} type="button">Add reviewed fact</button>
          </div>
          {profileDraft.facts.length ? <ul className="resume-rescue__fact-list">{profileDraft.facts.map((fact) => <li key={fact.id}><div><strong><UntrustedText>{fact.id}</UntrustedText></strong> <small>{fact.section} · {fact.confidentiality} · {fact.ownership_scope}</small><p><UntrustedText>{fact.text}</UntrustedText></p></div><button className="button button--ghost" onClick={() => setProfileDraft((current) => ({ ...current, facts: current.facts.filter((value) => value.id !== fact.id), conflicts: current.conflicts.filter((conflict) => !conflict.fact_ids.includes(fact.id)) }))} type="button">Remove</button></li>)}</ul> : <p className="muted">Add only facts you have reviewed. Each new fact receives a local manual-review timestamp.</p>}
          <div className="button-row">
            <button className="button button--primary" disabled={!state?.enabled || !profileDraft.profile_id.trim() || !profileDraft.display_name.trim() || busy === "profile"} onClick={() => void saveProfile()} type="button">{busy === "profile" ? "Saving…" : profileVersion ? "Save new profile version" : "Save profile"}</button>
            {profileVersion ? <button className="button button--ghost" onClick={() => { setProfileDraft(newProfileDraft()); setProfileVersion(undefined); }} type="button">Cancel edit</button> : null}
          </div>
        </section>

        <section className="settings-section" aria-labelledby="resume-opportunity-heading">
          <div className="section-heading-row"><div><p className="eyebrow">Exact job source</p><h2 id="resume-opportunity-heading">Opportunity snapshot</h2></div>{replacement ? <StatusBadge tone="info">Superseding</StatusBadge> : null}</div>
          <div className="prompt-rescue__context-grid">
            <label className="field"><span>Employer</span><input maxLength={512} onChange={(event) => setEmployer(event.currentTarget.value)} value={employer} /></label>
            <label className="field"><span>Role title</span><input maxLength={512} onChange={(event) => setTitle(event.currentTarget.value)} value={title} /></label>
            <label className="field"><span>Source URL (optional)</span><input maxLength={4_096} onChange={(event) => setSourceUrl(event.currentTarget.value)} type="url" value={sourceUrl} /></label>
            <label className="field"><span>Locale (optional)</span><input maxLength={64} onChange={(event) => setOpportunityLocale(event.currentTarget.value)} value={opportunityLocale} /></label>
            <label className="field"><span>Priorities, one per line</span><textarea maxLength={10_000} onChange={(event) => setPriorities(event.currentTarget.value)} rows={4} value={priorities} /></label>
            <label className="field field--wide"><span>Exact opportunity text</span><textarea maxLength={50_000} onChange={(event) => setSourceText(event.currentTarget.value)} rows={12} value={sourceText} /></label>
          </div>
          <div className="button-row"><button className="button button--primary" disabled={!state?.enabled || !employer.trim() || !title.trim() || !sourceText.trim() || busy === "opportunity"} onClick={() => void saveOpportunity()} type="button">{busy === "opportunity" ? "Saving…" : replacement ? "Save superseding snapshot" : "Save opportunity"}</button>{replacement ? <button className="button button--ghost" onClick={clearOpportunity} type="button">Cancel replacement</button> : null}</div>
        </section>
      </div>

      <section className="settings-section" aria-labelledby="resume-compose-heading">
        <p className="eyebrow">Exact tailoring review</p>
        <h2 id="resume-compose-heading">Compose a role-specific projection</h2>
        <div className="resume-rescue__selectors">
          <label className="field"><span>Current profile</span><select onChange={(event) => { setSelectedProfileId(event.currentTarget.value); setSelectedFactIds([]); setRequirements([]); }} value={selectedProfileId}><option value="">Select a profile</option>{state?.profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.profile.display_name} · v{profile.version}</option>)}</select></label>
          <label className="field"><span>Current opportunity</span><select onChange={(event) => { setSelectedOpportunityId(event.currentTarget.value); setRequirements([]); }} value={selectedOpportunityId}><option value="">Select an opportunity</option>{state?.opportunities.map((opportunity) => <option key={opportunity.id} value={opportunity.id}>{opportunity.snapshot.employer} · {opportunity.snapshot.title}</option>)}</select></label>
          {selectedProfile ? <button className="button button--ghost" onClick={() => editProfile(selectedProfile)} type="button">Edit selected profile</button> : null}
          {selectedOpportunity ? <button className="button button--ghost" onClick={() => replaceOpportunity(selectedOpportunity)} type="button">Supersede opportunity</button> : null}
        </div>
        {selectedOpportunity ? <details className="lineage-details"><summary>Review exact opportunity source</summary><p><UntrustedText>{selectedOpportunity.snapshot.source_text}</UntrustedText></p><p className="metadata-list--technical">Digest: <UntrustedText>{selectedOpportunity.digest}</UntrustedText></p></details> : null}
        {selectedProfile ? <div className="resume-rescue__selection-grid"><section><h3>1. Select reviewed facts</h3>{selectableFacts.length ? selectableFacts.map((fact) => <label className="resume-rescue__check" key={fact.id}><input checked={selectedFactIds.includes(fact.id)} onChange={() => toggleFact(fact.id)} type="checkbox" /><span><strong><UntrustedText>{fact.text}</UntrustedText></strong><small>{fact.id} · {fact.section} · {fact.confidentiality} · {fact.ownership_scope}</small></span></label>) : <p className="muted">No conflict-free facts are available.</p>}{selectedProfile.profile.conflicts.length ? <div className="warning-panel"><h3>Unresolved conflicts stay excluded</h3>{selectedProfile.profile.conflicts.map((conflict) => <p key={conflict.id}><UntrustedText>{conflict.description}</UntrustedText> ({conflict.fact_ids.join(", ")})</p>)}</div> : null}</section><section><h3>2. Add exact requirements</h3><label className="field"><span>Exact excerpt from opportunity text</span><textarea maxLength={5_000} onChange={(event) => setRequirementText(event.currentTarget.value)} rows={4} value={requirementText} /></label><button className="button button--secondary" disabled={!selectedOpportunity} onClick={addRequirement} type="button">Add exact requirement</button>{requirements.map((requirement) => <div className="resume-rescue__requirement" key={requirement.id}><div className="section-heading-row"><strong><UntrustedText>{requirement.text}</UntrustedText></strong><button className="button button--ghost" onClick={() => setRequirements((current) => current.filter((value) => value.id !== requirement.id))} type="button">Remove</button></div><p className="muted">Map only facts you believe support this requirement. This mapping is not verified entailment.</p>{selectedFacts.map((fact) => <label className="resume-rescue__check" key={`${requirement.id}-${fact.id}`}><input checked={requirement.fact_ids.includes(fact.id)} onChange={() => toggleRequirementFact(requirement.id, fact.id)} type="checkbox" /><span><UntrustedText>{fact.text}</UntrustedText></span></label>)}{!requirement.fact_ids.length ? <StatusBadge tone="warning">Missing evidence</StatusBadge> : null}</div>)}</section></div> : <p className="empty-callout">Save and select a profile to review facts.</p>}
        <div className="button-row"><button className="button button--primary" disabled={!state?.enabled || !selectedProfile || !selectedOpportunity || !selectedFactIds.length || busy === "compose"} onClick={() => void compose()} type="button">{busy === "compose" ? "Composing…" : "Create exact projection"}</button><p className="muted">No model, upload, application, or submission action is used.</p></div>
      </section>

      <section aria-labelledby="resume-results-heading">
        <div className="section-heading-row"><div><p className="eyebrow">Immutable local results</p><h2 id="resume-results-heading">Projection review</h2></div><button className="button button--ghost" onClick={() => void refresh().catch((reason: unknown) => setError(displayError(reason)))} type="button">Refresh</button></div>
        {state?.projections.length ? <div className="resume-rescue__projections">{state.projections.map((projection) => <article className="settings-section" key={projection.id}><div className="detail-header"><div><p className="eyebrow">{formatDateTime(projection.created_at)}</p><h3><UntrustedText>{projection.artifact.opportunity_binding.title}</UntrustedText> · <UntrustedText>{projection.artifact.opportunity_binding.employer}</UntrustedText></h3></div><StatusBadge tone="positive">Exact only</StatusBadge></div>{projection.artifact.sections.map((section) => <section key={section.kind}><h3>{section.kind}</h3><ul>{section.items.map((item) => <li key={item.fact_id}><UntrustedText>{item.text}</UntrustedText><div className="token-list"><span className="technical-label">{item.fact_id}</span><span className="technical-label">{item.confidentiality}</span><span className="technical-label">{item.ownership_scope}</span><span className="technical-label">{item.provenance.map((source) => source.kind).join(", ")}</span></div></li>)}</ul></section>)}<section><h3>Requirement evidence ledger</h3>{projection.artifact.requirement_coverage.map((coverage) => <div className="resume-rescue__coverage" key={coverage.id}><StatusBadge tone={coverage.status === "candidate_supported" ? "info" : "warning"}>{coverage.status === "candidate_supported" ? "Candidate support" : "Missing evidence"}</StatusBadge><p><UntrustedText>{coverage.text}</UntrustedText></p><small>{coverage.support_assurance} · {coverage.fact_ids.join(", ") || "no mapped facts"}</small></div>)}</section>{projection.artifact.warnings.length ? <div className="warning-panel"><h3>Required review warnings</h3><ul>{projection.artifact.warnings.map((warning, index) => <li key={`${projection.id}-warning-${index}`}><UntrustedText>{warning}</UntrustedText></li>)}</ul></div> : null}<div className="button-row"><button className="button button--primary" disabled={Boolean(busy)} onClick={() => void openPreview(projection)} type="button">{busy === `preview:${projection.id}` ? "Rendering…" : "Open document preview"}</button><span className="muted">Fixed template · no scripts or network</span></div><details className="lineage-details"><summary>Technical bindings</summary><ul className="metadata-list--technical"><li>Profile {projection.profile_id} v{projection.profile_version}: {projection.profile_digest}</li><li>Opportunity {projection.opportunity_id}: {projection.opportunity_digest}</li><li>Artifact: {projection.artifact_digest}</li><li>Action capability: {projection.artifact.action_capability}</li></ul></details></article>)}</div> : <p className="empty-callout">No current projection exists. Profile updates and opportunity supersessions deliberately hide stale results.</p>}
      </section>

      {preview ? (
        <section className="settings-section resume-rescue__document" id="resume-document-preview">
          <div className="section-heading-row">
            <div>
              <p className="eyebrow">Sandboxed deterministic document</p>
              <h2>HTML preview</h2>
              <p className="muted">Template {preview.template_id} · renderer v{preview.renderer_version}</p>
            </div>
            <div className="button-row">
              <button className="button button--primary" disabled={busy === "export"} onClick={() => void exportPreview()} type="button">{busy === "export" ? "Saving…" : "Save new HTML file"}</button>
              <button className="button button--ghost" onClick={() => setPreview(null)} type="button">Close preview</button>
            </div>
          </div>
          <iframe
            className="resume-rescue__iframe"
            referrerPolicy="no-referrer"
            sandbox=""
            srcDoc={preview.html}
            title="Deterministic résumé document preview"
          />
          <details className="lineage-details">
            <summary>Parser-order and digest review</summary>
            <p className="metadata-list--technical">Document digest: <UntrustedText>{preview.document_digest}</UntrustedText></p>
            <pre className="resume-rescue__plain-text"><UntrustedText>{preview.plain_text}</UntrustedText></pre>
          </details>
        </section>
      ) : null}
    </main>
  );
}

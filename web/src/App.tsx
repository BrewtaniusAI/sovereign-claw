import { useMemo, useState } from "react";

type RunResult = {
  status?: string;
  reason?: string;
  trace_id?: string;
  final_drift?: number | string;
  steps?: unknown[] | number;
  drift_trajectory?: Array<number | string>;
  provider?: string;
  policy_status?: string;
  preview?: boolean;
};

type PreviewResult = {
  mode?: "preview";
  supported?: boolean;
  predicted_drift?: number | string | null;
  expected_halt_reason?: string | null;
  step_estimate?: number | string | null;
  source_status?: string | null;
  drift_trajectory?: Array<number | string>;
  trace_id?: string | null;
  note?: string | null;
  detail?: string | null;
};

type ControlState = "idle" | "preview" | "approved" | "executing";
type RuntimeState = "none" | "halted" | "executed" | "error";

type Tone = {
  label: string;
  banner: string;
  badge: string;
  card: string;
  glow: string;
  ring: string;
  accentText: string;
};

function App() {
  const [objective, setObjective] = useState("system check then run governed");
  const [loading, setLoading] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [result, setResult] = useState<RunResult | null>(null);
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [approved, setApproved] = useState(false);
  const [approvedObjective, setApprovedObjective] = useState<string | null>(null);
  const [previewObjectiveText, setPreviewObjectiveText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const bridgeBase = `${window.location.protocol}//${window.location.hostname}:8787`;

  const objectiveMatchesPreview =
    !!preview && !!previewObjectiveText && previewObjectiveText === objective;

  const canApprove =
    !!preview &&
    objectiveMatchesPreview &&
    preview.supported === true &&
    !previewLoading &&
    !loading;

  const canRun =
    approved &&
    !!approvedObjective &&
    approvedObjective === objective &&
    objectiveMatchesPreview &&
    !loading;

  const invalidateApproval = () => {
    setApproved(false);
    setApprovedObjective(null);
  };

  const onObjectiveChange = (value: string) => {
    setObjective(value);
    setPreview(null);
    setPreviewObjectiveText(null);
    invalidateApproval();
    setError(null);
  };

  const runObjective = async () => {
    if (!objective.trim()) return;

    if (!canRun) {
      setError(
        "Execution requires a successful preview and explicit approval for the current objective."
      );
      return;
    }

    setLoading(true);
    setError(null);

    try {
      const res = await fetch(`${bridgeBase}/run`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ objective }),
      });

      const data: RunResult & { error?: string } = await res.json();

      if (!res.ok) {
        throw new Error(data.error || "Execution failed");
      }

      setResult(data);
    } catch (err) {
      setError(
        `Bridge request failed: ${
          err instanceof Error ? err.message : "Unknown error"
        }`
      );
    } finally {
      setLoading(false);
    }
  };

  const previewObjective = async () => {
    if (!objective.trim()) return;

    setPreviewLoading(true);
    setError(null);
    invalidateApproval();

    try {
      const res = await fetch(`${bridgeBase}/preview`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ objective }),
      });

      const data: PreviewResult & { error?: string } = await res.json();

      if (!res.ok) {
        throw new Error(data.error || "Preview failed");
      }

      setPreview(data);
      setPreviewObjectiveText(objective);
    } catch (err) {
      setError(
        `Bridge request failed: ${
          err instanceof Error ? err.message : "Unknown error"
        }`
      );
      setPreview(null);
      setPreviewObjectiveText(null);
    } finally {
      setPreviewLoading(false);
    }
  };

  const approvePreview = () => {
    if (!canApprove) {
      setError("Approval requires a valid preview for the current objective.");
      return;
    }

    setApproved(true);
    setApprovedObjective(objective);
    setError(null);
  };

  const copyTraceId = async () => {
    const id = result?.trace_id ?? preview?.trace_id ?? null;
    if (!id) return;

    try {
      await navigator.clipboard.writeText(id);
    } catch {
      setError("Could not copy trace ID to clipboard.");
    }
  };

  const driftPoints = useMemo(() => {
    if (!result?.drift_trajectory?.length) return [];
    return result.drift_trajectory
      .map((value) => {
        const n = typeof value === "number" ? value : Number(value);
        return Number.isFinite(n) ? n : null;
      })
      .filter((v): v is number => v !== null);
  }, [result]);

  const controlState: ControlState = useMemo(() => {
    if (loading) return "executing";
    if (approved && approvedObjective === objective) return "approved";
    if (previewLoading || (preview && objectiveMatchesPreview)) return "preview";
    return "idle";
  }, [
    loading,
    approved,
    approvedObjective,
    objective,
    previewLoading,
    preview,
    objectiveMatchesPreview,
  ]);

  const runtimeState: RuntimeState = useMemo(() => {
    if (error) return "error";
    if (!result?.status) return "none";
    if (result.status === "halted") return "halted";
    if (result.status === "executed") return "executed";
    return "none";
  }, [result, error]);

  const controlTone = useMemo<Tone>(() => {
    switch (controlState) {
      case "executing":
        return {
          label: "EXECUTING",
          banner:
            "border-cyan-400/40 bg-cyan-500/10 text-cyan-200 shadow-[0_0_60px_rgba(34,211,238,0.12)]",
          badge: "border-cyan-400/50 bg-cyan-500/10 text-cyan-200",
          card: "border-cyan-400/20 bg-cyan-500/10",
          glow: "shadow-[0_0_70px_rgba(34,211,238,0.20)]",
          ring: "ring-cyan-400/40",
          accentText: "text-cyan-300",
        };
      case "approved":
        return {
          label: "APPROVED",
          banner:
            "border-emerald-400/40 bg-emerald-500/10 text-emerald-100 shadow-[0_0_70px_rgba(16,185,129,0.14)]",
          badge: "border-emerald-400/50 bg-emerald-500/10 text-emerald-200",
          card: "border-emerald-400/20 bg-emerald-500/10",
          glow: "shadow-[0_0_80px_rgba(16,185,129,0.18)]",
          ring: "ring-emerald-400/40",
          accentText: "text-emerald-300",
        };
      case "preview":
        return {
          label: "PREVIEW",
          banner:
            "border-cyan-400/40 bg-cyan-500/10 text-cyan-200 shadow-[0_0_60px_rgba(34,211,238,0.12)]",
          badge: "border-cyan-400/50 bg-cyan-500/10 text-cyan-200",
          card: "border-cyan-400/20 bg-cyan-500/10",
          glow: "shadow-[0_0_70px_rgba(34,211,238,0.20)]",
          ring: "ring-cyan-400/40",
          accentText: "text-cyan-300",
        };
      case "idle":
      default:
        return {
          label: "IDLE",
          banner:
            "border-slate-400/30 bg-slate-500/10 text-slate-100 shadow-[0_0_60px_rgba(148,163,184,0.10)]",
          badge: "border-slate-400/40 bg-slate-500/10 text-slate-200",
          card: "border-slate-400/20 bg-slate-500/10",
          glow: "shadow-[0_0_70px_rgba(96,165,250,0.10)]",
          ring: "ring-slate-400/30",
          accentText: "text-slate-300",
        };
    }
  }, [controlState]);

  const controlSummary = useMemo(() => {
    switch (controlState) {
      case "executing":
        return "Governed execution is currently active.";
      case "approved":
        return "Preview approved. Governed execution is unlocked for this exact objective.";
      case "preview":
        return "Preview is available. Explicit operator approval is required before execution.";
      case "idle":
      default:
        return "Awaiting objective preview or governed execution.";
    }
  }, [controlState]);

  const runtimeText = runtimeState === "none" ? "No runtime result yet" : runtimeState;
  const runtimeReasonText =
    runtimeState === "error"
      ? error ?? "Unknown error"
      : result?.reason ?? "No runtime result yet";

  const traceId = result?.trace_id ?? preview?.trace_id ?? "No trace issued";
  const finalDrift =
    result?.final_drift !== undefined ? String(result.final_drift) : "—";
  const stepsCount = Array.isArray(result?.steps)
    ? result?.steps.length
    : result?.steps ?? "—";

  const approvalStateText =
    approved && approvedObjective === objective
      ? "Approved for current objective"
      : objectiveMatchesPreview
      ? "Preview complete — approval required"
      : "No valid preview for current objective";

  return (
    <div className="min-h-screen bg-[#05060a] text-slate-100">
      <div className="mx-auto max-w-7xl px-6 py-8">
        <header className={`mb-6 rounded-3xl border px-6 py-5 ${controlTone.banner}`}>
          <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
            <div>
              <p className="text-xs uppercase tracking-[0.32em] opacity-80">
                Control State
              </p>
              <div className="mt-2 flex flex-wrap items-center gap-3">
                <h1 className="text-3xl font-semibold tracking-tight">
                  {controlTone.label}
                </h1>
                <span
                  className={`rounded-full border px-3 py-1 text-[11px] font-medium tracking-[0.22em] ${controlTone.badge}`}
                >
                  APPROVAL-GATED RUNTIME
                </span>
              </div>
              <p className="mt-3 max-w-3xl text-sm text-slate-300">
                {controlSummary}
              </p>
            </div>

            <div className="grid gap-3 sm:grid-cols-3">
              <BannerMetric label="Runtime Result" value={runtimeText} />
              <BannerMetric label="Trace ID" value={traceId} mono />
              <BannerMetric label="Approval" value={approvalStateText} />
            </div>
          </div>
        </header>

        <div className="mb-6 grid gap-6 lg:grid-cols-[360px_1fr]">
          <section className="relative overflow-hidden rounded-3xl border border-fuchsia-500/20 bg-[radial-gradient(circle_at_top_left,rgba(168,85,247,0.16),transparent_30%),radial-gradient(circle_at_bottom_right,rgba(251,191,36,0.12),transparent_24%),linear-gradient(180deg,rgba(10,12,18,0.98),rgba(7,9,14,0.98))] p-6 shadow-2xl">
            <div className="absolute inset-0 pointer-events-none bg-[linear-gradient(135deg,rgba(255,255,255,0.03),transparent_40%)]" />
            <div className="relative">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <p className="text-xs uppercase tracking-[0.28em] text-fuchsia-300/80">
                    Giles Projection
                  </p>
                  <h2 className="mt-2 text-2xl font-semibold text-white">
                    Control-Linked Authority
                  </h2>
                  <p className="mt-2 text-sm text-slate-300/80">
                    Visual projection of control state. Last runtime result remains separately visible.
                  </p>
                </div>

                <div
                  className={`rounded-full border px-3 py-1 text-[11px] font-medium tracking-[0.22em] ${controlTone.badge}`}
                >
                  {controlTone.label}
                </div>
              </div>

              <div
                className={`relative mx-auto mt-6 flex w-full max-w-[250px] items-center justify-center rounded-[28px] border border-white/10 bg-black/30 p-3 ring-1 ${controlTone.ring} ${controlTone.glow}`}
              >
                <div className="absolute inset-0 rounded-[28px] bg-[radial-gradient(circle_at_center,rgba(255,255,255,0.06),transparent_55%)]" />
                <img
                  src="/giles.png"
                  alt="Giles state projection"
                  className="relative h-auto w-full rounded-2xl object-cover"
                />
              </div>

              <div className="mt-5 grid grid-cols-3 gap-3">
                <StatusChip label="Control" value={controlState} accent />
                <StatusChip label="Runtime" value={runtimeText} />
                <StatusChip
                  label="Approval"
                  value={approved && approvedObjective === objective ? "locked-in" : "pending"}
                />
              </div>

              <div className="mt-5 rounded-2xl border border-white/10 bg-white/5 p-4">
                <div className="text-[10px] uppercase tracking-[0.22em] text-slate-400">
                  Visual mapping
                </div>
                <div className={`mt-2 text-sm ${controlTone.accentText}`}>
                  {controlState === "executing"
                    ? "Cyan pulse indicates active governed execution."
                    : controlState === "approved"
                    ? "Steady emerald indicates operator-approved execution readiness."
                    : controlState === "preview"
                    ? "Cyan readiness indicates preview complete and awaiting approval."
                    : "Subdued cool state indicates idle readiness."}
                </div>
              </div>
            </div>
          </section>

          <section className="rounded-3xl border border-cyan-500/20 bg-[linear-gradient(180deg,rgba(9,12,18,0.96),rgba(7,8,14,0.96))] p-6 shadow-2xl">
            <div className="mb-5 flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
              <div>
                <p className="text-xs uppercase tracking-[0.28em] text-cyan-300/80">
                  Operator Console
                </p>
                <h2 className="mt-2 text-2xl font-semibold text-white">
                  Objective Dispatch
                </h2>
              </div>

              <div className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-slate-300">
                AI proposes • operator approves • system executes
              </div>
            </div>

            <label
              htmlFor="objective"
              className="mb-2 block text-sm font-medium text-slate-300"
            >
              Objective
            </label>

            <div className="flex flex-col gap-3 lg:flex-row">
              <textarea
                id="objective"
                value={objective}
                onChange={(e) => onObjectiveChange(e.target.value)}
                rows={4}
                className="min-h-[120px] flex-1 resize-y rounded-2xl border border-cyan-400/20 bg-black/30 px-4 py-3 text-slate-100 outline-none transition placeholder:text-slate-500 focus:border-cyan-300/50"
                placeholder="Enter governed objective..."
              />

              <div className="flex w-full flex-col gap-3 lg:w-[240px]">
                <button
                  type="button"
                  onClick={previewObjective}
                  disabled={previewLoading}
                  className="rounded-2xl border border-cyan-400/40 bg-cyan-500/10 px-4 py-4 text-sm font-semibold tracking-[0.18em] text-cyan-200 transition hover:bg-cyan-500/15 disabled:cursor-wait disabled:opacity-70"
                >
                  {previewLoading ? "PREVIEWING" : "PREVIEW"}
                </button>

                <button
                  type="button"
                  onClick={approvePreview}
                  disabled={!canApprove}
                  className="rounded-2xl border border-emerald-400/40 bg-emerald-500/10 px-4 py-4 text-sm font-semibold tracking-[0.18em] text-emerald-200 transition hover:bg-emerald-500/15 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {approved && approvedObjective === objective ? "APPROVED" : "APPROVE"}
                </button>

                <button
                  type="button"
                  onClick={runObjective}
                  disabled={!canRun}
                  className="rounded-2xl border border-amber-400/40 bg-amber-500/10 px-4 py-4 text-sm font-semibold tracking-[0.18em] text-amber-200 transition hover:bg-amber-500/15 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {loading ? "RUNNING" : "RUN GOVERNED"}
                </button>

                <button
                  type="button"
                  onClick={copyTraceId}
                  disabled={!result?.trace_id && !preview?.trace_id}
                  className="rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm text-slate-200 transition hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  COPY TRACE ID
                </button>
              </div>
            </div>

            {error && (
              <div className="mt-4 rounded-2xl border border-red-400/30 bg-red-500/10 px-4 py-3 text-sm text-red-200">
                {error}
              </div>
            )}

            <div className="mt-4 rounded-2xl border border-white/10 bg-white/5 p-4">
              <div className="text-[10px] uppercase tracking-[0.22em] text-slate-400">
                Approval Gate
              </div>
              <div className="mt-2 text-sm text-slate-200">{approvalStateText}</div>
              <div className="mt-2 text-xs text-slate-400">
                Editing the objective invalidates preview and approval.
              </div>
            </div>

            <div className="mt-6 grid gap-4 md:grid-cols-3">
              <PrimaryStateCard label="Control State" value={controlState} className={controlTone.card} />
              <PrimaryStateCard label="Runtime Result" value={runtimeText} className={runtimeStateTone(runtimeState)} />
              <PrimaryStateCard label="Trace ID" value={traceId} className={controlTone.card} mono />
            </div>
          </section>
        </div>

        <div className="grid gap-6 lg:grid-cols-[1.2fr_0.8fr]">
          <section className="rounded-3xl border border-emerald-500/20 bg-[linear-gradient(180deg,rgba(8,12,14,0.96),rgba(8,10,12,0.96))] p-6 shadow-2xl">
            <div className="mb-5 flex items-center justify-between">
              <div>
                <p className="text-xs uppercase tracking-[0.28em] text-emerald-300/80">
                  Governance Metrics
                </p>
                <h3 className="mt-2 text-xl font-semibold text-white">
                  Last Runtime Result
                </h3>
              </div>
            </div>

            <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
              <MetricCard label="Runtime Result" value={runtimeText} tone={runtimeTone(runtimeState)} />
              <MetricCard label="Final Drift" value={finalDrift} tone="emerald" />
              <MetricCard label="Steps" value={String(stepsCount)} tone="fuchsia" />
              <MetricCard
                label="Policy"
                value={result?.policy_status ?? "constraint-gated"}
                tone="slate"
              />
            </div>

            <div className="mt-6 rounded-2xl border border-white/10 bg-black/20 p-4">
              <div className="mb-3 flex items-center justify-between">
                <div className="text-sm font-medium text-slate-200">
                  Drift Trajectory
                </div>
                <div className="text-xs uppercase tracking-[0.22em] text-slate-400">
                  bounded convergence
                </div>
              </div>

              {driftPoints.length ? (
                <>
                  <Sparkline points={driftPoints} halted={runtimeState === "halted"} />
                  <div className="mt-3 grid gap-2 text-xs text-slate-300 md:grid-cols-4">
                    {driftPoints.map((point, i) => (
                      <div
                        key={`${point}-${i}`}
                        className="rounded-xl border border-white/10 bg-white/5 px-3 py-2"
                      >
                        <span className="mr-2 text-slate-500">t{i + 1}</span>
                        {point}
                      </div>
                    ))}
                  </div>
                </>
              ) : (
                <div className="rounded-xl border border-dashed border-white/10 px-4 py-8 text-sm text-slate-400">
                  No live drift trajectory yet.
                </div>
              )}
            </div>
          </section>

          <section className="rounded-3xl border border-amber-500/20 bg-[linear-gradient(180deg,rgba(15,12,8,0.96),rgba(11,9,7,0.96))] p-6 shadow-2xl">
            <p className="text-xs uppercase tracking-[0.28em] text-amber-300/80">
              Proof / Explain
            </p>
            <h3 className="mt-2 text-xl font-semibold text-white">
              Governed Decision Surface
            </h3>

            <div className="mt-5 space-y-4">
              <ProofRow label="Operator Objective" value={objective || "—"} />
              <ProofRow label="Control State" value={controlState} />
              <ProofRow label="Last Runtime Result" value={runtimeText} />
              <ProofRow label="Last Runtime Reason" value={runtimeReasonText} />
              <ProofRow label="Trace" value={traceId} mono />
              <ProofRow
                label="Preview Drift"
                value={
                  preview?.predicted_drift !== undefined &&
                  preview?.predicted_drift !== null
                    ? String(preview.predicted_drift)
                    : "No preview yet"
                }
              />
              <ProofRow
                label="Expected Halt Reason"
                value={preview?.expected_halt_reason ?? "No preview yet"}
              />
              <ProofRow
                label="Step Estimate"
                value={
                  preview?.step_estimate !== undefined &&
                  preview?.step_estimate !== null
                    ? String(preview.step_estimate)
                    : "No preview yet"
                }
              />
              <ProofRow label="Approval State" value={approvalStateText} />
              <ProofRow
                label="Preview Note"
                value={preview?.note ?? "No preview yet"}
              />
              <ProofRow
                label="Preview Detail"
                value={preview?.detail ?? "No preview detail"}
              />
              <ProofRow
                label="Bounded Summary"
                value={
                  approved && approvedObjective === objective && preview
                    ? `Control state is approved for this objective. Last runtime result remains ${runtimeText}. Governed execution is unlocked with predicted drift ${
                        preview.predicted_drift ?? "—"
                      } after ${preview.step_estimate ?? "—"} steps.`
                    : preview
                    ? `Control state is preview. Last runtime result remains ${runtimeText}. Preview reports ${
                        preview.source_status ?? "unknown status"
                      } with drift ${
                        preview.predicted_drift ?? "—"
                      } after ${preview.step_estimate ?? "—"} steps.`
                    : result
                    ? `Control state is ${controlState}. Last runtime result is ${runtimeText} with final drift ${finalDrift} after ${stepsCount} steps.`
                    : `Control state is ${controlState}. No runtime result has been recorded yet.`
                }
              />
            </div>

            <div className="mt-6 rounded-2xl border border-white/10 bg-white/5 p-4 text-sm text-slate-300">
              <div className="mb-2 text-xs uppercase tracking-[0.22em] text-slate-400">
                Operator note
              </div>
              This console separates control state from runtime result. Approval and preview govern execution readiness; runtime result records the last governed outcome.
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}

function runtimeTone(state: RuntimeState): "emerald" | "amber" | "cyan" | "fuchsia" | "slate" {
  switch (state) {
    case "executed":
      return "emerald";
    case "halted":
      return "amber";
    case "error":
      return "cyan";
    case "none":
    default:
      return "slate";
  }
}

function runtimeStateTone(state: RuntimeState): string {
  switch (state) {
    case "executed":
      return "border-emerald-400/20 bg-emerald-500/10";
    case "halted":
      return "border-amber-400/20 bg-amber-500/10";
    case "error":
      return "border-cyan-400/20 bg-cyan-500/10";
    case "none":
    default:
      return "border-slate-400/20 bg-slate-500/10";
  }
}

function BannerMetric({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="rounded-2xl border border-white/10 bg-black/20 px-4 py-3">
      <div className="text-[10px] uppercase tracking-[0.22em] text-slate-400">
        {label}
      </div>
      <div className={`mt-2 text-sm text-white ${mono ? "break-all font-mono" : ""}`}>
        {value}
      </div>
    </div>
  );
}

function StatusChip({
  label,
  value,
  accent = false,
}: {
  label: string;
  value: string;
  accent?: boolean;
}) {
  return (
    <div
      className={`rounded-2xl border px-3 py-3 ${
        accent
          ? "border-amber-400/30 bg-amber-500/10"
          : "border-white/10 bg-white/5"
      }`}
    >
      <div className="text-[10px] uppercase tracking-[0.22em] text-slate-400">
        {label}
      </div>
      <div className="mt-1 text-sm font-medium text-white">{value}</div>
    </div>
  );
}

function PrimaryStateCard({
  label,
  value,
  className,
  mono = false,
}: {
  label: string;
  value: string;
  className: string;
  mono?: boolean;
}) {
  return (
    <div className={`rounded-2xl border p-4 ${className}`}>
      <div className="text-[10px] uppercase tracking-[0.22em] text-slate-300/80">
        {label}
      </div>
      <div
        className={`mt-2 text-base font-semibold text-white ${
          mono ? "break-all font-mono text-sm" : ""
        }`}
      >
        {value}
      </div>
    </div>
  );
}

function MetricCard({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: "emerald" | "amber" | "cyan" | "fuchsia" | "slate";
}) {
  const toneMap: Record<string, string> = {
    emerald: "border-emerald-400/20 bg-emerald-500/10 text-emerald-200",
    amber: "border-amber-400/20 bg-amber-500/10 text-amber-200",
    cyan: "border-cyan-400/20 bg-cyan-500/10 text-cyan-200",
    fuchsia: "border-fuchsia-400/20 bg-fuchsia-500/10 text-fuchsia-200",
    slate: "border-slate-400/20 bg-slate-500/10 text-slate-200",
  };

  return (
    <div className={`rounded-2xl border p-4 ${toneMap[tone]}`}>
      <div className="text-[10px] uppercase tracking-[0.22em] opacity-75">
        {label}
      </div>
      <div className="mt-2 text-sm font-medium text-white">{value}</div>
    </div>
  );
}

function ProofRow({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="rounded-2xl border border-white/10 bg-white/5 p-4">
      <div className="text-[10px] uppercase tracking-[0.22em] text-slate-400">
        {label}
      </div>
      <div className={`mt-2 text-sm text-slate-100 ${mono ? "break-all font-mono" : ""}`}>
        {value}
      </div>
    </div>
  );
}

function Sparkline({
  points,
  halted,
}: {
  points: number[];
  halted: boolean;
}) {
  const width = 560;
  const height = 150;
  const padding = 16;

  const min = Math.min(...points);
  const max = Math.max(...points);
  const range = max - min || 1;

  const coordinates = points.map((point, i) => {
    const x =
      padding + (i * (width - padding * 2)) / Math.max(points.length - 1, 1);
    const y =
      height -
      padding -
      ((point - min) / range) * (height - padding * 2);
    return `${x},${y}`;
  });

  const polyline = coordinates.join(" ");

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      className="h-40 w-full rounded-2xl border border-white/10 bg-[#06080d]"
      preserveAspectRatio="none"
    >
      <defs>
        <linearGradient id="driftGlow" x1="0" y1="0" x2="0" y2="1">
          <stop
            offset="0%"
            stopColor={halted ? "#fbbf24" : "#34d399"}
            stopOpacity="0.95"
          />
          <stop
            offset="100%"
            stopColor={halted ? "#fb923c" : "#22d3ee"}
            stopOpacity="0.2"
          />
        </linearGradient>
      </defs>

      <polyline
        fill="none"
        stroke="url(#driftGlow)"
        strokeWidth="4"
        strokeLinejoin="round"
        strokeLinecap="round"
        points={polyline}
      />

      {points.map((point, i) => {
        const [x, y] = coordinates[i].split(",").map(Number);
        return (
          <g key={`${point}-${i}`}>
            <circle cx={x} cy={y} r="5" fill={halted ? "#fbbf24" : "#34d399"} />
            <circle
              cx={x}
              cy={y}
              r="10"
              fill={halted ? "#fbbf24" : "#34d399"}
              opacity="0.12"
            />
          </g>
        );
      })}
    </svg>
  );
}

export default App;
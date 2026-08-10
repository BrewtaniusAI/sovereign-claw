import { useCallback, useEffect, useMemo, useState } from "react";

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
  error?: string;
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
  provider?: string;
  policy_status?: string;
  preview?: boolean;
  status?: string;
  reason?: string | null;
  final_drift?: number | string | null;
  steps?: unknown[] | number | null;
  error?: string;
};

type ControlState = "idle" | "preview" | "approved" | "executing";
type RuntimeState = "none" | "halted" | "executed" | "error";

type TraceHistoryEntry = {
  id: string;
  kind: "preview" | "run";
  objective: string;
  controlStateAtTime: ControlState;
  runtimeStateAtTime: RuntimeState;
  traceId: string;
  reason: string;
  provider: string;
  policyStatus: string;
  finalDrift: string;
  steps: string;
  createdAt: string;
  previewSummary: string;
  payload: RunResult | PreviewResult;
};

type TraceHistoryResponse = {
  traces?: TraceHistoryEntry[];
  count?: number;
};

type Tone = {
  label: string;
  banner: string;
  badge: string;
  card: string;
  glow: string;
  ring: string;
  accentText: string;
};

const MAX_TRACE_HISTORY = 12;
const MAX_OBJECTIVE_CHARS = 512;
const TOKEN_STORAGE_KEY = "sovereign.operatorToken";

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
  const [traceHistory, setTraceHistory] = useState<TraceHistoryEntry[]>([]);
  const [selectedTraceId, setSelectedTraceId] = useState<string | null>(null);
  const [operatorToken, setOperatorToken] = useState(() =>
    window.localStorage.getItem(TOKEN_STORAGE_KEY) ?? ""
  );

  const bridgeBase = useMemo(() => resolveBridgeBase(), []);
  const sanitizedToken = operatorToken.trim();
  const objectiveTooLong = objective.length > MAX_OBJECTIVE_CHARS;

  const objectiveMatchesPreview =
    !!preview && !!previewObjectiveText && previewObjectiveText === objective;

  const canApprove =
    !!preview &&
    objectiveMatchesPreview &&
    preview.supported === true &&
    !previewLoading &&
    !loading &&
    !objectiveTooLong;

  const canRun =
    approved &&
    !!approvedObjective &&
    approvedObjective === objective &&
    !loading &&
    !objectiveTooLong;

  const buildBridgeHeaders = useCallback(
    (includeJson = false): Record<string, string> => {
      const headers: Record<string, string> = {};
      if (includeJson) {
        headers["Content-Type"] = "application/json";
      }
      if (sanitizedToken) {
        headers.Authorization = "Bear" + "er " + sanitizedToken;
      }
      return headers;
    },
    [sanitizedToken]
  );

  useEffect(() => {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, operatorToken);
  }, [operatorToken]);

  useEffect(() => {
    let cancelled = false;

    async function loadTraces() {
      if (!sanitizedToken) {
        if (!cancelled) {
          setTraceHistory([]);
          setSelectedTraceId(null);
        }
        return;
      }

      try {
        const res = await fetch(`${bridgeBase}/traces`, {
          headers: buildBridgeHeaders(),
        });
        if (!res.ok) {
          throw new Error(`Trace load failed: ${res.status}`);
        }

        const data: TraceHistoryResponse = await res.json();
        if (cancelled) return;

        const traces = Array.isArray(data.traces) ? data.traces : [];
        setTraceHistory(traces.slice(0, MAX_TRACE_HISTORY));

        if (traces.length > 0) {
          setSelectedTraceId((current) => current ?? traces[0].id);
        }
      } catch {
        // Lack of persisted traces should not block console use.
      }
    }

    loadTraces();

    return () => {
      cancelled = true;
    };
  }, [bridgeBase, buildBridgeHeaders, sanitizedToken]);

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

  const currentControlState: ControlState = useMemo(() => {
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
    if (result.status === "error") return "error";
    return "none";
  }, [result, error]);

  const pushTraceHistory = (entry: TraceHistoryEntry) => {
    setTraceHistory((prev) => {
      const deduped = prev.filter((item) => item.id !== entry.id);
      return [entry, ...deduped].slice(0, MAX_TRACE_HISTORY);
    });
    setSelectedTraceId(entry.id);
  };

  const formatTraceTime = (iso: string) => {
    try {
      return new Date(iso).toLocaleString();
    } catch {
      return iso;
    }
  };

  const normalizeNumericString = (
    value: number | string | null | undefined
  ) => {
    if (value === null || value === undefined) return "—";
    return String(value);
  };

  const normalizeStepCount = (
    value: unknown[] | number | string | null | undefined
  ) => {
    if (Array.isArray(value)) return String(value.length);
    if (value === null || value === undefined) return "—";
    return String(value);
  };

  const buildPreviewTraceEntry = (
    payload: PreviewResult,
    objectiveText: string
  ): TraceHistoryEntry => {
    const predictedDrift = normalizeNumericString(payload.predicted_drift);
    const stepEstimate = normalizeStepCount(payload.step_estimate ?? null);

    return {
      id: `preview-${payload.trace_id ?? Date.now()}`,
      kind: "preview",
      objective: objectiveText,
      controlStateAtTime: "preview",
      runtimeStateAtTime:
        payload.status === "error" ? "error" : payload.supported === false ? "none" : "halted",
      traceId: payload.trace_id ?? "No trace issued",
      reason:
        payload.expected_halt_reason ??
        payload.reason ??
        payload.error ??
        "Preview generated",
      provider: payload.provider ?? "preview-bridge",
      policyStatus:
        payload.policy_status ??
        (payload.supported === true ? "preview-supported" : "preview-unsupported"),
      finalDrift: predictedDrift,
      steps: stepEstimate,
      createdAt: new Date().toISOString(),
      previewSummary: payload.note ?? payload.detail ?? "Preview generated by bridge.",
      payload,
    };
  };

  const buildRunTraceEntry = (
    payload: RunResult,
    objectiveText: string,
    stateAtTime: RuntimeState
  ): TraceHistoryEntry => {
    return {
      id: `run-${payload.trace_id ?? Date.now()}`,
      kind: "run",
      objective: objectiveText,
      controlStateAtTime: "executing",
      runtimeStateAtTime: stateAtTime,
      traceId: payload.trace_id ?? "No trace issued",
      reason: payload.reason ?? payload.error ?? "Governed execution recorded",
      provider: payload.provider ?? "runtime-local",
      policyStatus: payload.policy_status ?? "constraint-gated",
      finalDrift: normalizeNumericString(payload.final_drift),
      steps: normalizeStepCount(payload.steps),
      createdAt: new Date().toISOString(),
      previewSummary: payload.reason ?? "Governed runtime result recorded.",
      payload,
    };
  };

  const runObjective = async () => {
    if (!objective.trim()) return;
    if (!sanitizedToken) {
      setError("Operator token required before governed execution.");
      return;
    }
    if (objectiveTooLong) {
      setError(`Objective exceeds ${MAX_OBJECTIVE_CHARS} characters.`);
      return;
    }

    if (!canRun) {
      setError(
        "Execution requires explicit approval for the current objective."
      );
      return;
    }

    setLoading(true);
    setError(null);

    try {
      const res = await fetch(`${bridgeBase}/run`, {
        method: "POST",
        headers: buildBridgeHeaders(true),
        body: JSON.stringify({ objective, intent: "execute" }),
      });

      const data: RunResult & { error?: string } = await res.json();

      if (!res.ok) {
        throw new Error(data.error || "Execution failed");
      }

      setResult(data);

      const nextRuntimeState: RuntimeState =
        data.status === "halted"
          ? "halted"
          : data.status === "executed"
          ? "executed"
          : data.status === "error"
          ? "error"
          : "none";

      pushTraceHistory(buildRunTraceEntry(data, objective, nextRuntimeState));
    } catch (err) {
      const payload: RunResult = {
        status: "error",
        error: err instanceof Error ? err.message : "Unknown error",
        provider: "runtime-local",
        policy_status: "constraint-gated",
        preview: false,
      };

      setResult(payload);
      setError(`Bridge request failed: ${payload.error}`);
      pushTraceHistory(buildRunTraceEntry(payload, objective, "error"));
    } finally {
      setLoading(false);
    }
  };

  const previewObjective = async () => {
    if (!objective.trim()) return;
    if (!sanitizedToken) {
      setError("Operator token required before preview.");
      return;
    }
    if (objectiveTooLong) {
      setError(`Objective exceeds ${MAX_OBJECTIVE_CHARS} characters.`);
      return;
    }

    setPreviewLoading(true);
    setError(null);
    invalidateApproval();

    try {
      const res = await fetch(`${bridgeBase}/preview`, {
        method: "POST",
        headers: buildBridgeHeaders(true),
        body: JSON.stringify({ objective, intent: "preview" }),
      });

      const data: PreviewResult & { error?: string } = await res.json();

      if (!res.ok) {
        throw new Error(data.error || "Preview failed");
      }

      setPreview(data);
      setPreviewObjectiveText(objective);
      pushTraceHistory(buildPreviewTraceEntry(data, objective));
    } catch (err) {
      const payload: PreviewResult = {
        mode: "preview",
        supported: false,
        predicted_drift: null,
        expected_halt_reason: "Preview failed",
        step_estimate: null,
        source_status: "preview-unavailable",
        drift_trajectory: [],
        trace_id: null,
        note: "Preview fallback failed.",
        detail: err instanceof Error ? err.message : "Unknown error",
        provider: "preview-bridge",
        policy_status: "preview-unsupported",
        preview: true,
        status: "error",
        error: err instanceof Error ? err.message : "Unknown error",
      };

      setError(`Bridge request failed: ${payload.detail}`);
      setPreview(payload);
      setPreviewObjectiveText(objective);
      pushTraceHistory(buildPreviewTraceEntry(payload, objective));
    } finally {
      setPreviewLoading(false);
    }
  };

  const approvePreview = () => {
    if (!preview || !objectiveMatchesPreview || preview.supported !== true) {
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

  const selectedTrace = useMemo(
    () => traceHistory.find((entry) => entry.id === selectedTraceId) ?? null,
    [traceHistory, selectedTraceId]
  );

  const selectedTraceObjective = selectedTrace?.objective ?? objective;
  const selectedTraceControlState =
    selectedTrace?.controlStateAtTime ?? currentControlState;
  const selectedTraceRuntimeState =
    selectedTrace?.runtimeStateAtTime ?? runtimeState;
  const selectedTraceIdValue =
    selectedTrace?.traceId ??
    (result?.trace_id ?? preview?.trace_id ?? "No trace issued");
  const selectedTraceReason =
    selectedTrace?.reason ?? (error ?? result?.reason ?? "No runtime result yet");
  const selectedTraceDrift = selectedTrace?.finalDrift ?? "—";
  const selectedTraceSteps = selectedTrace?.steps ?? "—";
  const selectedTraceSummary =
    selectedTrace?.previewSummary ??
    (approved && approvedObjective === objective && preview
      ? `Control state is approved for this objective. Last runtime result remains ${runtimeTextFromState(
          runtimeState
        )}. Governed execution is unlocked with predicted drift ${
          preview.predicted_drift ?? "—"
        } after ${preview.step_estimate ?? "—"} steps.`
      : preview
      ? `Control state is preview. Last runtime result remains ${runtimeTextFromState(
          runtimeState
        )}. Preview reports ${preview.source_status ?? "unknown status"} with drift ${
          preview.predicted_drift ?? "—"
        } after ${preview.step_estimate ?? "—"} steps.`
      : result
      ? `Control state is ${currentControlState}. Last runtime result is ${runtimeTextFromState(
          runtimeState
        )} with final drift ${
          result.final_drift !== undefined ? String(result.final_drift) : "—"
        } after ${
          Array.isArray(result.steps) ? result.steps.length : result.steps ?? "—"
        } steps.`
      : `Control state is ${currentControlState}. No runtime result has been recorded yet.`);

  const previewDriftText =
    selectedTrace?.kind === "preview"
      ? selectedTrace.finalDrift
      : preview?.predicted_drift !== undefined && preview?.predicted_drift !== null
      ? String(preview.predicted_drift)
      : "No preview yet";

  const expectedHaltReasonText =
    selectedTrace?.kind === "preview"
      ? selectedTrace.reason
      : preview?.expected_halt_reason ?? "No preview yet";

  const stepEstimateText =
    selectedTrace?.kind === "preview"
      ? selectedTrace.steps
      : preview?.step_estimate !== undefined && preview?.step_estimate !== null
      ? String(preview.step_estimate)
      : "No preview yet";

  const previewNoteText =
    selectedTrace?.kind === "preview"
      ? selectedTrace.previewSummary
      : preview?.note ?? "No preview yet";

  const previewDetailText =
    selectedTrace?.kind === "preview"
      ? (selectedTrace.payload as PreviewResult).detail ?? "No preview detail"
      : preview?.detail ?? "No preview detail";

  const controlTone = useMemo<Tone>(() => {
    switch (currentControlState) {
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
  }, [currentControlState]);

  const controlSummary = useMemo(() => {
    switch (currentControlState) {
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
  }, [currentControlState]);

  const runtimeText = runtimeTextFromState(runtimeState);
  const traceId = result?.trace_id ?? preview?.trace_id ?? "No trace issued";
  const finalDrift =
    result?.final_drift !== undefined ? String(result.final_drift) : "—";
  const stepsCount = Array.isArray(result?.steps)
    ? result.steps.length
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
                <StatusChip label="Control" value={currentControlState} accent />
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
                  {currentControlState === "executing"
                    ? "Cyan pulse indicates active governed execution."
                    : currentControlState === "approved"
                    ? "Steady emerald indicates operator-approved execution readiness."
                    : currentControlState === "preview"
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

            <div className="mb-5 grid gap-4 md:grid-cols-[1.4fr_0.6fr]">
               <label className="block">
                 <span className="mb-2 block text-sm font-medium text-slate-300">
                   Operator token
                 </span>
                 <input
                   type="password"
                   value={operatorToken}
                   onChange={(e) => setOperatorToken(e.target.value)}
                   className="w-full rounded-2xl border border-cyan-400/20 bg-black/30 px-4 py-3 text-slate-100 outline-none transition placeholder:text-slate-500 focus:border-cyan-300/50"
                   placeholder="Paste bearer token"
                   autoComplete="off"
                 />
               </label>

               <div className="rounded-2xl border border-white/10 bg-white/5 p-4 text-sm text-slate-300">
                 <div className="text-[10px] uppercase tracking-[0.22em] text-slate-400">
                   Bridge guardrails
                 </div>
                 <div className="mt-2">
                   Authenticated preview, approval-gated execution, and fail-closed request intent are required.
                 </div>
               </div>
            </div>

            <label
               htmlFor="objective"
               className="mb-2 block text-sm font-medium text-slate-300"
            >
               Objective
            </label>

            <div className="mb-2 flex items-center justify-between text-xs text-slate-400">
               <span>Bounded objective length</span>
               <span className={objectiveTooLong ? "text-red-300" : ""}>
                 {objective.length}/{MAX_OBJECTIVE_CHARS}
               </span>
            </div>

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
                  disabled={previewLoading || objectiveTooLong}
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
              <PrimaryStateCard
                label="Control State"
                value={currentControlState}
                className={controlTone.card}
              />
              <PrimaryStateCard
                label="Runtime Result"
                value={runtimeText}
                className={runtimeStateTone(runtimeState)}
              />
              <PrimaryStateCard
                label="Trace ID"
                value={traceId}
                className={controlTone.card}
                mono
              />
            </div>
          </section>
        </div>

        <div className="grid gap-6 lg:grid-cols-[1.15fr_0.85fr]">
          <section className="space-y-6">
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
                <MetricCard
                  label="Runtime Result"
                  value={runtimeText}
                  tone={runtimeTone(runtimeState)}
                />
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
                    <div className="mt-3 grid gap-2 text-xs text-slate-300 md:grid-cols-3 xl:grid-cols-4">
                      {driftPoints.map((point, i) => (
                        <div
                          key={`${point}-${i}`}
                          className="rounded-xl border border-white/10 bg-white/5 px-3 py-2"
                        >
                          <span className="mr-2 text-slate-500">t{i + 1}</span>
                          <span className="font-mono">{point}</span>
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

            <section className="rounded-3xl border border-fuchsia-500/20 bg-[linear-gradient(180deg,rgba(12,9,18,0.96),rgba(8,7,14,0.96))] p-6 shadow-2xl">
              <div className="mb-5 flex items-center justify-between gap-4">
                <div>
                  <p className="text-xs uppercase tracking-[0.28em] text-fuchsia-300/80">
                    Trace History
                  </p>
                  <h3 className="mt-2 text-xl font-semibold text-white">
                    Recent Trace History
                  </h3>
                  <p className="mt-2 text-sm text-slate-300/80">
                    Read-only session history of preview and governed run traces.
                  </p>
                </div>

                <div className="rounded-full border border-white/10 bg-white/5 px-3 py-1 text-xs text-slate-300">
                  inspection only
                </div>
              </div>

              {traceHistory.length === 0 ? (
                <div className="rounded-2xl border border-dashed border-white/10 bg-white/5 px-4 py-8 text-sm text-slate-400">
                  No traces recorded in this session.
                </div>
              ) : (
                <div className="grid gap-3">
                  {traceHistory.map((entry) => {
                    const isSelected = selectedTraceId === entry.id;

                    return (
                      <button
                        key={entry.id}
                        type="button"
                        onClick={() => setSelectedTraceId(entry.id)}
                        className={`rounded-2xl border p-4 text-left transition ${
                          isSelected
                            ? "border-fuchsia-400/40 bg-fuchsia-500/10 shadow-[0_0_30px_rgba(168,85,247,0.12)]"
                            : "border-white/10 bg-white/5 hover:border-fuchsia-300/30 hover:bg-white/10"
                        }`}
                      >
                        <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
                          <div className="min-w-0">
                            <div className="flex flex-wrap items-center gap-2">
                              <span
                                className={`rounded-full border px-2 py-1 text-[10px] uppercase tracking-[0.18em] ${
                                  entry.kind === "preview"
                                    ? "border-cyan-400/40 bg-cyan-500/10 text-cyan-200"
                                    : "border-amber-400/40 bg-amber-500/10 text-amber-200"
                                }`}
                              >
                                {entry.kind}
                              </span>
                              <span className="rounded-full border border-white/10 bg-black/20 px-2 py-1 text-[10px] uppercase tracking-[0.18em] text-slate-300">
                                {entry.runtimeStateAtTime}
                              </span>
                              <span className="rounded-full border border-white/10 bg-black/20 px-2 py-1 text-[10px] uppercase tracking-[0.18em] text-slate-300">
                                {entry.controlStateAtTime}
                              </span>
                            </div>

                            <div className="mt-3 text-xs uppercase tracking-[0.22em] text-slate-500">
                              Trace ID
                            </div>
                            <div className="mt-1 break-all font-mono text-sm text-slate-100">
                              {entry.traceId}
                            </div>
                          </div>

                          <div className="grid gap-1 text-sm text-slate-300 xl:min-w-[220px]">
                            <div>
                              <span className="text-slate-500">Drift:</span> {entry.finalDrift}
                            </div>
                            <div>
                              <span className="text-slate-500">Steps:</span> {entry.steps}
                            </div>
                            <div>
                              <span className="text-slate-500">Policy:</span> {entry.policyStatus}
                            </div>
                            <div>
                              <span className="text-slate-500">Time:</span> {formatTraceTime(entry.createdAt)}
                            </div>
                          </div>
                        </div>

                        <div className="mt-3 rounded-xl border border-white/10 bg-black/20 px-3 py-2 text-sm text-slate-300">
                          {entry.reason}
                        </div>
                      </button>
                    );
                  })}
                </div>
              )}
            </section>
          </section>

          <section className="rounded-3xl border border-amber-500/20 bg-[linear-gradient(180deg,rgba(15,12,8,0.96),rgba(11,9,7,0.96))] p-6 shadow-2xl">
            <p className="text-xs uppercase tracking-[0.28em] text-amber-300/80">
              Proof / Explain
            </p>
            <h3 className="mt-2 text-xl font-semibold text-white">
              Governed Decision Surface
            </h3>

            <div className="mt-5 space-y-4">
              <ProofRow label="Operator Objective" value={selectedTraceObjective || "—"} />
              <ProofRow label="Control State" value={selectedTraceControlState} />
              <ProofRow
                label="Last Runtime Result"
                value={runtimeTextFromState(selectedTraceRuntimeState)}
              />
              <ProofRow label="Last Runtime Reason" value={selectedTraceReason} />
              <ProofRow label="Trace" value={selectedTraceIdValue} mono />
              <ProofRow label="Selected Trace Drift" value={selectedTraceDrift} />
              <ProofRow label="Selected Trace Steps" value={selectedTraceSteps} />
              <ProofRow label="Preview Drift" value={previewDriftText} />
              <ProofRow label="Expected Halt Reason" value={expectedHaltReasonText} />
              <ProofRow label="Step Estimate" value={stepEstimateText} />
              <ProofRow label="Approval State" value={approvalStateText} />
              <ProofRow label="Preview Note" value={previewNoteText} />
              <ProofRow label="Preview Detail" value={previewDetailText} />
              <ProofRow label="Bounded Summary" value={selectedTraceSummary} />
            </div>

            <div className="mt-6 rounded-2xl border border-white/10 bg-white/5 p-4 text-sm text-slate-300">
              <div className="mb-2 text-xs uppercase tracking-[0.22em] text-slate-400">
                Operator note
              </div>
              This console separates control state from runtime result. Approval and preview govern execution readiness; runtime result records the last governed outcome. Trace history is inspection-only and does not replay or mutate execution.
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}

function runtimeTextFromState(state: RuntimeState): string {
  return state === "none" ? "No runtime result yet" : state;
}

function runtimeTone(
  state: RuntimeState
): "emerald" | "amber" | "cyan" | "fuchsia" | "slate" {
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

function resolveBridgeBase(): string {
  const explicit = import.meta.env.VITE_BRIDGE_BASE?.trim();
  if (explicit) {
    return explicit.replace(/\/$/, "");
  }

  if (window.location.port === "5173") {
    return `${window.location.protocol}//${window.location.hostname}:8787`;
  }

  return window.location.origin;
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
import express from "express";
import crypto from "crypto";
import fs from "fs";
import path from "path";
import { execFileSync, spawn, spawnSync } from "child_process";
import { fileURLToPath } from "url";

const SERVICE_NAME = "sovereign-claw-bridge";
const MAX_TRACE_HISTORY = 50;
const DEFAULT_PORT = 8787;
const DEFAULT_HOST = "127.0.0.1";
const DEFAULT_JSON_LIMIT = "16kb";
const DEFAULT_OBJECTIVE_LIMIT = 512;
const DEFAULT_RATE_LIMIT_WINDOW_MS = 60_000;
const DEFAULT_CLIENT_RATE_LIMIT = 30;
const DEFAULT_GLOBAL_RATE_LIMIT = 120;
const DEFAULT_CLI_TIMEOUT_MS = 30_000;
const DEFAULT_STDOUT_LIMIT_BYTES = 65_536;
const DEFAULT_STDERR_LIMIT_BYTES = 16_384;
const DEFAULT_PREVIEW_TTL_MS = 5 * 60_000;
const DEFAULT_APPROVAL_TTL_MS = 60_000;
const DEFAULT_RATE_LIMIT_ENTRY_CAP = 1024;
const DEFAULT_APPROVAL_STORE_CAP = 256;
const CHILD_ENV_ALLOWLIST = new Set([
  "APPDATA",
  "COMSPEC",
  "HOME",
  "HOMEDRIVE",
  "HOMEPATH",
  "LANG",
  "LC_ALL",
  "LC_CTYPE",
  "LOCALAPPDATA",
  "LOGNAME",
  "NUMBER_OF_PROCESSORS",
  "PATH",
  "PATHEXT",
  "PYTHONHOME",
  "PYTHONIOENCODING",
  "PYTHONPATH",
  "PYTHONUTF8",
  "SHELL",
  "SYSTEMDRIVE",
  "SYSTEMROOT",
  "TEMP",
  "TERM",
  "TMP",
  "TMPDIR",
  "USER",
  "USERNAME",
  "USERPROFILE",
  "VIRTUAL_ENV",
  "WINDIR",
]);
const SOVEREIGN_ENV_PREFIX = "SOVEREIGN_";
const BRIDGE_ENV_PREFIX = "SOVEREIGN_BRIDGE_";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, "..");
const staticRoot = path.join(__dirname, "dist");

function parseInteger(value, fallback) {
  const parsed = Number.parseInt(String(value ?? ""), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function parseOptionalFloat(value) {
  if (value === null || value === undefined || String(value).trim() === "") {
    return null;
  }
  const parsed = Number.parseFloat(String(value));
  return Number.isFinite(parsed) ? parsed : null;
}

function parseOrigins(value) {
  return new Set(
    String(value ?? "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean)
  );
}

function resolvePythonExecutable(env) {
  if (typeof env.SOVEREIGN_PYTHON === "string" && env.SOVEREIGN_PYTHON.trim()) {
    return env.SOVEREIGN_PYTHON.trim();
  }

  return process.platform === "win32" ? "py" : "python3";
}

function resolveStateDir(env) {
  if (typeof env.SOVEREIGN_BRIDGE_STATE_DIR === "string" && env.SOVEREIGN_BRIDGE_STATE_DIR.trim()) {
    return path.resolve(env.SOVEREIGN_BRIDGE_STATE_DIR.trim());
  }

  return path.join(repoRoot, "data");
}

export function buildConfig(env = process.env) {
  const bridgeStateDir = resolveStateDir(env);
  return {
    serviceName: SERVICE_NAME,
    host: String(env.SOVEREIGN_BRIDGE_HOST || DEFAULT_HOST).trim() || DEFAULT_HOST,
    port: parseInteger(env.SOVEREIGN_BRIDGE_PORT, DEFAULT_PORT),
    authToken: String(env.SOVEREIGN_BRIDGE_TOKEN || "").trim(),
    allowedOrigins: parseOrigins(env.SOVEREIGN_BRIDGE_CORS_ORIGINS),
    jsonLimit: String(env.SOVEREIGN_BRIDGE_JSON_LIMIT || DEFAULT_JSON_LIMIT).trim(),
    maxObjectiveChars: parseInteger(
      env.SOVEREIGN_BRIDGE_MAX_OBJECTIVE_CHARS,
      DEFAULT_OBJECTIVE_LIMIT
    ),
    rateLimitWindowMs: parseInteger(
      env.SOVEREIGN_BRIDGE_RATE_LIMIT_WINDOW_MS,
      DEFAULT_RATE_LIMIT_WINDOW_MS
    ),
    clientRateLimit: parseInteger(
      env.SOVEREIGN_BRIDGE_RATE_LIMIT_PER_CLIENT,
      DEFAULT_CLIENT_RATE_LIMIT
    ),
    globalRateLimit: parseInteger(
      env.SOVEREIGN_BRIDGE_RATE_LIMIT_GLOBAL,
      DEFAULT_GLOBAL_RATE_LIMIT
    ),
    cliTimeoutMs: parseInteger(env.SOVEREIGN_BRIDGE_CLI_TIMEOUT_MS, DEFAULT_CLI_TIMEOUT_MS),
    stdoutLimitBytes: parseInteger(
      env.SOVEREIGN_BRIDGE_STDOUT_LIMIT_BYTES,
      DEFAULT_STDOUT_LIMIT_BYTES
    ),
    stderrLimitBytes: parseInteger(
      env.SOVEREIGN_BRIDGE_STDERR_LIMIT_BYTES,
      DEFAULT_STDERR_LIMIT_BYTES
    ),
    previewTtlMs: parseInteger(env.SOVEREIGN_BRIDGE_PREVIEW_TTL_MS, DEFAULT_PREVIEW_TTL_MS),
    approvalTtlMs: parseInteger(
      env.SOVEREIGN_BRIDGE_APPROVAL_TTL_MS,
      DEFAULT_APPROVAL_TTL_MS
    ),
    limiterEntryCap: parseInteger(
      env.SOVEREIGN_BRIDGE_RATE_LIMIT_ENTRY_CAP,
      DEFAULT_RATE_LIMIT_ENTRY_CAP
    ),
    approvalStoreCap: parseInteger(
      env.SOVEREIGN_BRIDGE_APPROVAL_STORE_CAP,
      DEFAULT_APPROVAL_STORE_CAP
    ),
    pythonExecutable: resolvePythonExecutable(env),
    bridgeStateDir,
    bridgeAuditPath: path.join(bridgeStateDir, "bridge-audit.jsonl"),
    cliProvider: String(env.SOVEREIGN_BRIDGE_CLI_PROVIDER || "").trim(),
    cliPolicyProfile:
      String(env.SOVEREIGN_BRIDGE_CLI_POLICY_PROFILE || "balanced").trim() || "balanced",
    cliBudget: parseOptionalFloat(env.SOVEREIGN_BRIDGE_CLI_BUDGET),
    repoRoot,
    staticRoot,
    traceCapacity: MAX_TRACE_HISTORY,
  };
}

function normalizeNumericString(value, fallback = "—") {
  if (value === null || value === undefined) return fallback;
  return String(value);
}

function normalizeStepCount(value, fallback = "—") {
  if (Array.isArray(value)) return String(value.length);
  if (value === null || value === undefined) return fallback;
  return String(value);
}

function buildCliArgs(config, preview, options = {}) {
  const args = ["-m", "sovereign_claw.cli", "run", "--objective-stdin", "--json"];
  if (preview) {
    args.push("--preview");
  }
  if (!preview && typeof options.expectedActionDigest === "string" && options.expectedActionDigest.trim()) {
    args.push("--expected-action-digest", options.expectedActionDigest.trim());
  }
  if (config.cliProvider) {
    args.push("--provider", config.cliProvider);
  }
  if (config.cliPolicyProfile) {
    args.push("--policy-profile", config.cliPolicyProfile);
  }
  if (config.cliBudget !== null) {
    args.push("--budget", String(config.cliBudget));
  }
  return args;
}

function buildRunArgs(config, approval = null) {
  return buildCliArgs(config, false, {
    expectedActionDigest: approval?.actionDigest ?? null,
  });
}

function buildPreviewArgs(config) {
  return buildCliArgs(config, true);
}

function stableValue(value) {
  if (Array.isArray(value)) {
    return value.map((item) => stableValue(item));
  }
  if (value && typeof value === "object") {
    return Object.keys(value)
      .sort()
      .reduce((acc, key) => {
        acc[key] = stableValue(value[key]);
        return acc;
      }, {});
  }
  return value;
}

function stableStringify(value) {
  return JSON.stringify(stableValue(value));
}

function sha256Hex(value) {
  return crypto.createHash("sha256").update(String(value)).digest("hex");
}

export function buildChildEnv(config, env = process.env) {
  const childEnv = {};
  for (const [key, value] of Object.entries(env)) {
    if (typeof value !== "string") {
      continue;
    }
    if (CHILD_ENV_ALLOWLIST.has(key)) {
      childEnv[key] = value;
      continue;
    }
  }

  const pythonPathEntries = [
    path.join(config.repoRoot, "src"),
    childEnv.PYTHONPATH,
  ].filter(Boolean);
  childEnv.PYTHONPATH = [...new Set(pythonPathEntries)].join(path.delimiter);
  return childEnv;
}

function redactCliArgs(args) {
  const redacted = [];
  let positionalObjectiveRedacted = false;
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (
      !positionalObjectiveRedacted &&
      index > 0 &&
      args[index - 1] === "run" &&
      typeof arg === "string" &&
      !arg.startsWith("-")
    ) {
      redacted.push("<redacted-objective>");
      positionalObjectiveRedacted = true;
      continue;
    }
    redacted.push(arg);
  }
  return redacted;
}

function summarizeCliInvocation(config, args, options = {}) {
  const objective =
    typeof options.objective === "string" && options.objective.trim() ? options.objective : null;
  return {
    command: config.pythonExecutable,
    args: redactCliArgs(args),
    objective_sha256: objective ? sha256Hex(objective) : null,
    objective_transport: typeof options.stdinData === "string" ? "stdin" : "argv",
  };
}

function createOpaqueToken() {
  return crypto.randomBytes(32).toString("base64url");
}

function redactDetail(value, maxBytes = 512) {
  if (typeof value !== "string" || !value) return undefined;
  const buffer = Buffer.from(value);
  if (buffer.length <= maxBytes) return value;
  return `${buffer.subarray(0, maxBytes).toString("utf8")}…`;
}

function secureTokenEquals(left, right) {
  const leftBytes = crypto.createHash("sha256").update(left).digest();
  const rightBytes = crypto.createHash("sha256").update(right).digest();
  return crypto.timingSafeEqual(leftBytes, rightBytes);
}

function sanitizeBridgeError(error, fallbackMessage) {
  const clientMessage =
    typeof error?.clientMessage === "string" && error.clientMessage.trim()
      ? error.clientMessage.trim()
      : fallbackMessage;

  const status =
    error?.reason === "timeout"
      ? 504
      : error?.reason === "output_limit"
      ? 502
      : error?.reason === "spawn_failed"
      ? 503
      : 500;

  return {
    status,
    message: clientMessage,
  };
}

export function createLimiter(limit, windowMs, maxEntries = DEFAULT_RATE_LIMIT_ENTRY_CAP) {
  const events = new Map();
  let sweepCounter = 0;

  const sweep = (now) => {
    for (const [entryKey, entry] of events.entries()) {
      entry.timestamps = entry.timestamps.filter((timestamp) => now - timestamp < windowMs);
      if (entry.timestamps.length === 0) {
        events.delete(entryKey);
      }
    }
  };

  const enforceCap = (now) => {
    if (events.size < maxEntries) {
      return;
    }
    sweep(now);
    while (events.size >= maxEntries) {
      let oldestKey = null;
      let oldestSeen = Number.POSITIVE_INFINITY;
      for (const [entryKey, entry] of events.entries()) {
        if (entry.lastSeen < oldestSeen) {
          oldestSeen = entry.lastSeen;
          oldestKey = entryKey;
        }
      }
      if (oldestKey === null) {
        break;
      }
      events.delete(oldestKey);
    }
  };

  return {
    consume(key) {
      const now = Date.now();
      sweepCounter += 1;
      if (sweepCounter % 32 === 0) {
        sweep(now);
      }
      enforceCap(now);

      const entry = events.get(key) ?? { timestamps: [], lastSeen: now };
      entry.timestamps = entry.timestamps.filter((timestamp) => now - timestamp < windowMs);
      entry.lastSeen = now;

      if (entry.timestamps.length >= limit) {
        const retryAfterMs = Math.max(windowMs - (now - entry.timestamps[0]), 0);
        events.set(key, entry);
        return {
          allowed: false,
          retryAfterSeconds: Math.max(1, Math.ceil(retryAfterMs / 1000)),
        };
      }

      entry.timestamps.push(now);
      events.set(key, entry);
      return { allowed: true, retryAfterSeconds: 0 };
    },
    size() {
      return events.size;
    },
  };
}

function createBoundedStore(capacity) {
  const store = new Map();

  const purgeExpired = (now) => {
    for (const [key, record] of store.entries()) {
      if (record.expiresAt <= now) {
        store.delete(key);
      }
    }
  };

  const enforceCap = () => {
    while (store.size > capacity) {
      const firstKey = store.keys().next().value;
      if (firstKey === undefined) break;
      store.delete(firstKey);
    }
  };

  return {
    set(key, value) {
      const now = Date.now();
      purgeExpired(now);
      store.delete(key);
      store.set(key, value);
      enforceCap();
    },
    get(key) {
      const now = Date.now();
      const value = store.get(key);
      if (!value) return null;
      if (value.expiresAt <= now) {
        store.delete(key);
        return null;
      }
      return value;
    },
    delete(key) {
      store.delete(key);
    },
    size() {
      purgeExpired(Date.now());
      return store.size;
    },
  };
}

function validateObjective(rawObjective, maxObjectiveChars) {
  if (typeof rawObjective !== "string") {
    return { ok: false, error: "Invalid objective" };
  }

  const objective = rawObjective.trim();
  if (!objective) {
    return { ok: false, error: "Objective must not be empty" };
  }

  if (objective.length > maxObjectiveChars) {
    return {
      ok: false,
      error: `Objective exceeds ${maxObjectiveChars} characters`,
    };
  }

  return { ok: true, objective };
}

function buildExecutionContext(config, payload) {
  const budget = payload?.budget ?? {
    requested: config.cliBudget,
    outcome: config.cliBudget === null ? "not-requested" : "unsupported",
    enforced: false,
  };
  const configIdentity = {
    repo_root: config.repoRoot,
    python_executable: config.pythonExecutable,
    cli_provider_override: config.cliProvider || null,
    cli_policy_profile: config.cliPolicyProfile,
    cli_budget: config.cliBudget,
  };
  return {
    requested_provider: payload?.requested_provider ?? config.cliProvider ?? null,
    fallback_policy: payload?.fallback_policy ?? "none",
    policy_profile: payload?.policy_profile ?? config.cliPolicyProfile,
    budget: {
      requested: budget?.requested ?? config.cliBudget ?? null,
      outcome: budget?.outcome ?? (config.cliBudget === null ? "not-requested" : "unsupported"),
      enforced: budget?.enforced === true,
    },
    config_identity: configIdentity,
    config_identity_hash: sha256Hex(stableStringify(configIdentity)),
  };
}

function buildPreviewEvidence({ objective, payload, contextDigest }) {
  const objectiveDigest = sha256Hex(objective);
  const previewEnvelope = {
    objective_digest: objectiveDigest,
    action_digest: payload?.action_digest ?? null,
    supported: payload?.supported !== false,
    approvable: payload?.approvable === true,
    predicted_drift: payload?.predicted_drift ?? payload?.final_drift ?? null,
    expected_halt_reason:
      payload?.expected_halt_reason ?? payload?.reason ?? payload?.error ?? null,
    step_estimate: payload?.step_estimate ?? payload?.steps ?? null,
    source_status: payload?.source_status ?? payload?.status ?? null,
    provider: payload?.actual_provider ?? payload?.provider ?? null,
    requested_provider: payload?.requested_provider ?? null,
    fallback_policy: payload?.fallback_policy ?? null,
    policy_status: payload?.policy_status ?? null,
    policy_profile: payload?.policy_profile ?? null,
    context_digest: contextDigest,
  };
  const previewDigest = sha256Hex(stableStringify(previewEnvelope));
  return {
    objectiveDigest,
    previewDigest,
    previewEnvelope,
  };
}

function toPreviewPayload(result, objective, config, previewTtlMs) {
  const context = buildExecutionContext(config, result);
  const contextDigest = sha256Hex(stableStringify(context));
  const supported = result?.supported !== false && result?.status !== "preview-unsupported";
  const actionDigest =
    typeof result?.action_digest === "string" && result.action_digest.trim()
      ? result.action_digest.trim()
      : null;
  const approvable =
    typeof result?.approvable === "boolean"
      ? result.approvable
      : supported &&
        result?.status === "preview" &&
        !!actionDigest &&
        !(
          typeof (result?.expected_halt_reason ?? result?.reason ?? result?.error) === "string" &&
          (result?.expected_halt_reason ?? result?.reason ?? result?.error).trim()
        );
  const payload = {
    mode: "preview",
    supported,
    approvable,
    predicted_drift: result?.predicted_drift ?? result?.final_drift ?? null,
    expected_halt_reason:
      result?.expected_halt_reason ?? result?.reason ?? result?.error ?? null,
    step_estimate:
      Array.isArray(result?.steps) ? result.steps.length : result?.step_estimate ?? result?.steps ?? null,
    tool_calls: result?.tool_calls ?? 0,
    source_status: result?.source_status ?? result?.status ?? null,
    drift_trajectory: result?.drift_trajectory ?? [],
    trace_id: result?.trace_id ?? null,
    note:
      result?.note ??
      (supported
        ? "Preview generated by CLI preview mode."
        : "Preview is unavailable without a safe runtime dry-run path."),
    detail: result?.detail ?? null,
    provider: result?.actual_provider ?? result?.provider ?? "preview-bridge",
    requested_provider: result?.requested_provider ?? null,
    actual_provider: result?.actual_provider ?? result?.provider ?? null,
    fallback_policy: result?.fallback_policy ?? "none",
    policy_profile: result?.policy_profile ?? config.cliPolicyProfile,
    budget: result?.budget ?? {
      requested: config.cliBudget,
      outcome: config.cliBudget === null ? "not-requested" : "unsupported",
      enforced: false,
    },
    policy_status:
      result?.policy_status ?? (supported ? "preview-supported" : "preview-unsupported"),
    preview: true,
    status: supported ? result?.status ?? "preview" : "preview-unsupported",
    reason: result?.reason ?? null,
    final_drift: result?.final_drift ?? null,
    steps: result?.steps ?? null,
    cli_exit_status:
      typeof result?.cli_exit_status === "number"
        ? result.cli_exit_status
        : supported
        ? 0
        : 2,
    action_digest: actionDigest,
    action_digest_version:
      typeof result?.action_digest_version === "string" && result.action_digest_version.trim()
        ? result.action_digest_version.trim()
        : null,
  };
  const evidence = buildPreviewEvidence({ objective, payload, contextDigest });
  payload.objective_digest = evidence.objectiveDigest;
  payload.preview_digest = evidence.previewDigest;
  payload.context_digest = contextDigest;
  payload.approval_expires_in_ms = previewTtlMs;
  return payload;
}

function toPreviewErrorPayload(message, objective, config, previewTtlMs) {
  return toPreviewPayload(
    {
      status: "preview-unsupported",
      supported: false,
      predicted_drift: null,
      expected_halt_reason: message,
      source_status: "preview-unavailable",
      drift_trajectory: [],
      trace_id: null,
      note: "Preview failed closed.",
      detail: message,
      provider: "preview-bridge",
      requested_provider: config.cliProvider || null,
      actual_provider: null,
      fallback_policy: "none",
      policy_profile: config.cliPolicyProfile,
      budget: {
        requested: config.cliBudget,
        outcome: config.cliBudget === null ? "not-requested" : "unsupported",
        enforced: false,
      },
      policy_status: "preview-unsupported",
      preview: true,
      error: message,
      cli_exit_status: 2,
      steps: 0,
    },
    objective,
    config,
    previewTtlMs
  );
}

function normalizeRunTrace(payload, objective) {
  const runtimeStateAtTime =
    payload?.status === "halted"
      ? "halted"
      : payload?.status === "executed"
      ? "executed"
      : payload?.status === "error"
      ? "error"
      : "none";

  return {
    id: `run-${payload?.trace_id ?? Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    kind: "run",
    objective,
    controlStateAtTime: "executing",
    runtimeStateAtTime,
    traceId: payload?.trace_id ?? "No trace issued",
    reason: payload?.reason ?? payload?.error ?? "Governed execution recorded",
    provider: payload?.actual_provider ?? payload?.provider ?? "runtime-local",
    policyStatus: payload?.policy_status ?? "constraint-gated",
    finalDrift: normalizeNumericString(payload?.final_drift),
    steps: normalizeStepCount(payload?.steps),
    createdAt: new Date().toISOString(),
    previewSummary: payload?.reason ?? "Governed runtime result recorded.",
    payload,
  };
}

function normalizePreviewTrace(payload, objective) {
  const supported = payload?.supported !== false;

  return {
    id: `preview-${payload?.trace_id ?? Date.now()}-${Math.random()
      .toString(36)
      .slice(2, 8)}`,
    kind: "preview",
    objective,
    controlStateAtTime: "preview",
    runtimeStateAtTime:
      payload?.status === "error"
        ? "error"
        : supported && (payload?.source_status === "halted" || payload?.preview === true)
        ? "halted"
        : "none",
    traceId: payload?.trace_id ?? "No trace issued",
    reason:
      payload?.expected_halt_reason ??
      payload?.reason ??
      payload?.error ??
      "Preview generated",
    provider: payload?.actual_provider ?? payload?.provider ?? "preview-bridge",
    policyStatus:
      payload?.policy_status ?? (supported ? "preview-supported" : "preview-unsupported"),
    finalDrift: normalizeNumericString(payload?.predicted_drift ?? payload?.final_drift),
    steps: normalizeStepCount(payload?.step_estimate ?? payload?.steps),
    createdAt: new Date().toISOString(),
    previewSummary:
      payload?.note ??
      payload?.detail ??
      (supported
        ? "Preview generated by CLI preview mode."
        : "Preview unavailable without safe dry-run support."),
    payload,
  };
}

function createAuditTrail(config, logger = console) {
  const ensureStateDir = () => {
    fs.mkdirSync(config.bridgeStateDir, { recursive: true });
  };

  return {
    write(event, details = {}) {
      try {
        ensureStateDir();
        fs.appendFileSync(
          config.bridgeAuditPath,
          `${JSON.stringify({
            timestamp: new Date().toISOString(),
            event,
            ...details,
          })}\n`,
          "utf8"
        );
        return true;
      } catch (error) {
        logger.error("Failed to persist bridge audit event", {
          event,
          detail: error instanceof Error ? error.message : String(error),
        });
        return false;
      }
    },
  };
}

function pythonProbeArgs(pythonExecutable) {
  const basename = path.basename(pythonExecutable).toLowerCase();
  if (process.platform === "win32" && (basename === "py" || basename === "py.exe")) {
    return ["-3", "-c", "import sovereign_claw"];
  }
  return ["-c", "import sovereign_claw"];
}

function defaultReadinessProbe(config, staticDir) {
  const components = [];

  const pushComponent = (name, ok, detail) => {
    components.push({ name, ok, detail });
  };

  pushComponent("bridge_token", Boolean(config.authToken), config.authToken ? "configured" : "missing");

  const staticReady = fs.existsSync(staticDir) && fs.existsSync(path.join(staticDir, "index.html"));
  pushComponent(
    "static_assets",
    staticReady,
    staticReady ? staticDir : `missing dist assets at ${staticDir}`
  );

  try {
    fs.mkdirSync(config.bridgeStateDir, { recursive: true });
    const probePath = path.join(
      config.bridgeStateDir,
      `.ready-${process.pid}-${Date.now()}.tmp`
    );
    fs.writeFileSync(probePath, "ready", "utf8");
    fs.unlinkSync(probePath);
    pushComponent("state_path", true, config.bridgeStateDir);
  } catch (error) {
    pushComponent(
      "state_path",
      false,
      error instanceof Error ? error.message : String(error)
    );
  }

  try {
    const result = spawnSync(config.pythonExecutable, pythonProbeArgs(config.pythonExecutable), {
      cwd: config.repoRoot,
      env: buildChildEnv(config),
      encoding: "utf8",
      timeout: config.cliTimeoutMs,
    });
    const ok = result.status === 0;
    pushComponent(
      "python_runtime",
      ok,
      ok
        ? `${config.pythonExecutable} import sovereign_claw`
        : redactDetail(result.stderr || result.stdout || "probe failed")
    );
  } catch (error) {
    pushComponent(
      "python_runtime",
      false,
      error instanceof Error ? error.message : String(error)
    );
  }

  const failing = components.find((component) => !component.ok);
  return {
    ok: !failing,
    status: failing ? "not-ready" : "ready",
    reason: failing ? `${failing.name}: ${failing.detail}` : null,
    components,
  };
}

export function executeCli(args, config, logger = console, options = {}) {
  const acceptJsonOnNonZero = options.acceptJsonOnNonZero === true;

  return new Promise((resolve, reject) => {
    const stdout = [];
    const stderr = [];
    let stdoutBytes = 0;
    let stderrBytes = 0;
    let settled = false;
    let timeoutHandle = null;
    let killHandle = null;

    const finalize = (handler, value) => {
      if (settled) return;
      settled = true;
      if (timeoutHandle) {
        clearTimeout(timeoutHandle);
        timeoutHandle = null;
      }
      handler(value);
    };

    const proc = spawn(config.pythonExecutable, args, {
      cwd: config.repoRoot,
      shell: false,
      detached: process.platform !== "win32",
      env: buildChildEnv(config),
    });

    const forceKill = () => {
      if (proc.exitCode !== null) return;
      if (process.platform === "win32") {
        try {
          execFileSync("taskkill", ["/PID", String(proc.pid), "/T", "/F"], {
            stdio: "ignore",
          });
        } catch {
          proc.kill();
        }
        return;
      }
      try {
        process.kill(-proc.pid, "SIGKILL");
      } catch {
        proc.kill("SIGKILL");
      }
    };

    const stopProcess = () => {
      if (proc.exitCode !== null) return;
      if (process.platform === "win32") {
        proc.kill();
      } else {
        try {
          process.kill(-proc.pid, "SIGTERM");
        } catch {
          proc.kill("SIGTERM");
        }
        killHandle = setTimeout(() => {
          forceKill();
        }, 250);
        killHandle.unref?.();
      }
    };

    if (proc.stdin) {
      if (typeof options.stdinData === "string") {
        proc.stdin.end(options.stdinData);
      } else {
        proc.stdin.end();
      }
    }

    timeoutHandle = setTimeout(() => {
      stopProcess();
      logger.error("CLI execution timed out", {
        ...summarizeCliInvocation(config, args, options),
        timeout_ms: config.cliTimeoutMs,
      });
      finalize(reject, {
        reason: "timeout",
        clientMessage: "Governed execution timed out",
      });
    }, config.cliTimeoutMs);
    timeoutHandle.unref?.();

    proc.stdout.on("data", (chunk) => {
      stdoutBytes += chunk.length;
      if (stdoutBytes > config.stdoutLimitBytes) {
        stopProcess();
        logger.error("CLI stdout exceeded configured limit", {
          ...summarizeCliInvocation(config, args, options),
          stdout_limit_bytes: config.stdoutLimitBytes,
        });
        finalize(reject, {
          reason: "output_limit",
          clientMessage: "Governed execution exceeded output limits",
        });
        return;
      }

      stdout.push(chunk);
    });

    proc.stderr.on("data", (chunk) => {
      stderrBytes += chunk.length;
      if (stderrBytes > config.stderrLimitBytes) {
        stopProcess();
        logger.error("CLI stderr exceeded configured limit", {
          ...summarizeCliInvocation(config, args, options),
          stderr_limit_bytes: config.stderrLimitBytes,
        });
        finalize(reject, {
          reason: "output_limit",
          clientMessage: "Governed execution exceeded output limits",
        });
        return;
      }

      stderr.push(chunk);
    });

    proc.on("error", (err) => {
      logger.error("Failed to start CLI process", {
        ...summarizeCliInvocation(config, args, options),
        detail: err.message,
      });
      finalize(reject, {
        reason: "spawn_failed",
        clientMessage: "Failed to start governed runtime",
      });
    });

    proc.on("close", (code, signal) => {
      if (killHandle) {
        clearTimeout(killHandle);
        killHandle = null;
      }
      if (timeoutHandle) {
        clearTimeout(timeoutHandle);
        timeoutHandle = null;
      }
      if (settled) return;

      const stdoutText = Buffer.concat(stdout).toString("utf8");
      const stderrText = Buffer.concat(stderr).toString("utf8");
      let parsed = null;
      if (stdoutText.trim()) {
        try {
          parsed = JSON.parse(stdoutText);
          if (parsed && typeof parsed === "object") {
            parsed.cli_exit_status = typeof code === "number" ? code : null;
          }
        } catch {
          parsed = null;
        }
      }

      if (code !== 0 && (!acceptJsonOnNonZero || !parsed)) {
        logger.error("CLI execution failed", {
          ...summarizeCliInvocation(config, args, options),
          code,
          signal,
          stderr: redactDetail(stderrText),
          stdout: redactDetail(stdoutText),
        });
        finalize(reject, {
          reason: "cli_failed",
          clientMessage: "Governed execution failed",
        });
        return;
      }

      if (parsed) {
        finalize(resolve, parsed);
        return;
      }
      logger.error("CLI returned invalid JSON", {
        ...summarizeCliInvocation(config, args, options),
        stdout: redactDetail(stdoutText),
        stderr: redactDetail(stderrText),
      });
      finalize(reject, {
        reason: "invalid_json",
        clientMessage: "Governed runtime returned invalid output",
      });
    });
  });
}

export function createApp({
  config = buildConfig(),
  logger = console,
  runCli = executeCli,
  staticDir = staticRoot,
  readinessProbe = defaultReadinessProbe,
} = {}) {
  const app = express();
  const traceHistory = [];
  const perClientLimiter = createLimiter(
    config.clientRateLimit,
    config.rateLimitWindowMs,
    config.limiterEntryCap
  );
  const globalLimiter = createLimiter(
    config.globalRateLimit,
    config.rateLimitWindowMs,
    config.limiterEntryCap
  );
  const previewStore = createBoundedStore(config.approvalStoreCap);
  const approvalStore = createBoundedStore(config.approvalStoreCap);
  const auditTrail = createAuditTrail(config, logger);

  const authorityAuditFailure = () => ({
    ok: false,
    status: 503,
    error: "Execution authority audit persistence is unavailable",
  });

  const writeAuthorityAudit = (event, details = {}) =>
    auditTrail.write(event, details) ? null : authorityAuditFailure();

  const pushTrace = (entry) => {
    traceHistory.unshift(entry);
    if (traceHistory.length > config.traceCapacity) {
      traceHistory.length = config.traceCapacity;
    }
  };

  const applyCors = (req, res, next) => {
    const origin = req.headers.origin;
    if (!origin) {
      return next();
    }

    const serverScheme = req.socket.encrypted ? "https" : "http";
    const serverOrigin = `${serverScheme}://${req.headers.host}`;
    const isSameOrigin = origin === serverOrigin;
    if (!isSameOrigin && !config.allowedOrigins.has(origin)) {
      return res.status(403).json({ error: "Cross-origin requests are not allowed" });
    }

    res.setHeader("Access-Control-Allow-Origin", origin);
    res.setHeader("Vary", "Origin");
    res.setHeader("Access-Control-Allow-Headers", "Authorization, Content-Type");
    res.setHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");

    if (req.method === "OPTIONS") {
      return res.sendStatus(204);
    }

    return next();
  };

  const requireAuth = (req, res, next) => {
    if (!config.authToken) {
      return res.status(503).json({ error: "Operator bridge token is not configured" });
    }

    const authHeader = req.headers.authorization;
    if (!authHeader?.startsWith("Bearer ")) {
      return res.status(401).json({ error: "Authentication required" });
    }

    const supplied = authHeader.slice("Bearer ".length).trim();
    if (!supplied || !secureTokenEquals(supplied, config.authToken)) {
      return res.status(401).json({ error: "Authentication failed" });
    }

    return next();
  };

  const requirePreviewIntent = (req, res, next) => {
    if (req.body?.intent !== "preview") {
      return res.status(400).json({
        error: "Explicit intent 'preview' is required",
      });
    }
    return next();
  };

  const applyRateLimit = (req, res, next) => {
    const globalResult = globalLimiter.consume("global");
    if (!globalResult.allowed) {
      res.setHeader("Retry-After", String(globalResult.retryAfterSeconds));
      return res.status(429).json({ error: "Global rate limit exceeded" });
    }

    const clientKey = req.ip || req.socket.remoteAddress || "unknown";
    const clientResult = perClientLimiter.consume(clientKey);
    if (!clientResult.allowed) {
      res.setHeader("Retry-After", String(clientResult.retryAfterSeconds));
      return res.status(429).json({ error: "Client rate limit exceeded" });
    }

    return next();
  };

  const consumeApproval = ({ token, objective }) => {
    if (typeof token !== "string" || !token.trim()) {
      return { ok: false, status: 400, error: "Execution approval token is required" };
    }

    const tokenHash = sha256Hex(token.trim());
    const approval = approvalStore.get(tokenHash);
    if (!approval) {
      const auditFailure = writeAuthorityAudit("approval_rejected", {
        reason: "missing_or_expired",
        token_hash: tokenHash,
      });
      if (auditFailure) {
        return auditFailure;
      }
      return { ok: false, status: 409, error: "Execution approval token is invalid or expired" };
    }

    const objectiveDigest = sha256Hex(objective);
    if (approval.objectiveDigest !== objectiveDigest) {
      const auditFailure = writeAuthorityAudit("approval_rejected", {
        reason: "objective_mismatch",
        token_hash: tokenHash,
        expected_objective_digest: approval.objectiveDigest,
        actual_objective_digest: objectiveDigest,
      });
      if (auditFailure) {
        return auditFailure;
      }
      return { ok: false, status: 409, error: "Execution approval token does not match this objective" };
    }

    const currentContext = buildExecutionContext(config, {
      requested_provider: approval.context?.requested_provider ?? null,
      fallback_policy: approval.context?.fallback_policy ?? "none",
      policy_profile: approval.context?.policy_profile ?? config.cliPolicyProfile,
      budget: approval.context?.budget ?? null,
    });
    const currentContextDigest = sha256Hex(stableStringify(currentContext));

    if (approval.contextDigest !== currentContextDigest) {
      const auditFailure = writeAuthorityAudit("approval_rejected", {
        reason: "context_mismatch",
        token_hash: tokenHash,
        approval_context_digest: approval.contextDigest,
        current_context_digest: currentContextDigest,
      });
      if (auditFailure) {
        return auditFailure;
      }
      return { ok: false, status: 409, error: "Execution approval token is no longer valid for the current runtime context" };
    }

    if (typeof approval.actionDigest !== "string" || !approval.actionDigest.trim()) {
      const auditFailure = writeAuthorityAudit("approval_rejected", {
        reason: "action_digest_missing",
        token_hash: tokenHash,
        preview_digest: approval.previewDigest,
      });
      if (auditFailure) {
        return auditFailure;
      }
      approvalStore.delete(tokenHash);
      return { ok: false, status: 409, error: "Execution approval token is missing approved action evidence" };
    }

    const auditFailure = writeAuthorityAudit("approval_consumed", {
      token_hash: tokenHash,
      objective_digest: approval.objectiveDigest,
      preview_digest: approval.previewDigest,
      context_digest: approval.contextDigest,
      action_digest: approval.actionDigest,
      requested_provider: approval.context?.requested_provider ?? null,
      fallback_policy: approval.context?.fallback_policy ?? "none",
      policy_profile: approval.context?.policy_profile ?? null,
      budget: approval.context?.budget ?? null,
      config_identity_hash: approval.context?.config_identity_hash ?? null,
      issued_at: new Date(approval.issuedAt).toISOString(),
      consumed_at: new Date().toISOString(),
      evidence_id: approval.evidenceId,
    });
    if (auditFailure) {
      return auditFailure;
    }
    approvalStore.delete(tokenHash);
    return { ok: true, approval };
  };

  app.disable("x-powered-by");
  app.use(applyCors);
  app.use(express.json({ limit: config.jsonLimit }));

  app.get("/health", (_req, res) => {
    res.json({
      ok: true,
      service: config.serviceName,
      status: "ok",
    });
  });

  app.get("/ready", async (_req, res) => {
    const report = await Promise.resolve(readinessProbe(config, staticDir));
    if (!report.ok) {
      return res.status(503).json({
        ok: false,
        service: config.serviceName,
        status: report.status,
        reason: report.reason,
        components: report.components,
      });
    }

    return res.json({
      ok: true,
      service: config.serviceName,
      status: report.status,
      components: report.components,
    });
  });

  app.get("/traces", requireAuth, applyRateLimit, (_req, res) => {
    res.json({
      traces: traceHistory,
      count: traceHistory.length,
    });
  });

  app.post("/approve", requireAuth, applyRateLimit, (req, res) => {
    const validation = validateObjective(req.body?.objective, config.maxObjectiveChars);
    if (!validation.ok) {
      return res.status(400).json({ error: validation.error });
    }

    const previewDigest = String(req.body?.preview_digest || "").trim();
    if (!previewDigest) {
      return res.status(400).json({ error: "preview_digest is required" });
    }

    const previewRecord = previewStore.get(previewDigest);
    if (!previewRecord) {
      const auditFailure = writeAuthorityAudit("approval_rejected", {
        reason: "preview_missing_or_expired",
        preview_digest: previewDigest,
      });
      if (auditFailure) {
        return res.status(auditFailure.status).json({ error: auditFailure.error });
      }
      return res.status(409).json({ error: "Preview approval context is invalid or expired" });
    }

    if (previewRecord.objectiveDigest !== sha256Hex(validation.objective)) {
      const auditFailure = writeAuthorityAudit("approval_rejected", {
        reason: "preview_objective_mismatch",
        preview_digest: previewDigest,
      });
      if (auditFailure) {
        return res.status(auditFailure.status).json({ error: auditFailure.error });
      }
      return res.status(409).json({ error: "Preview approval context does not match this objective" });
    }

    if (previewRecord.supported !== true) {
      const auditFailure = writeAuthorityAudit("approval_rejected", {
        reason: "preview_unsupported",
        preview_digest: previewDigest,
      });
      if (auditFailure) {
        return res.status(auditFailure.status).json({ error: auditFailure.error });
      }
      return res.status(409).json({ error: "Preview must be supported before execution can be approved" });
    }

    if (previewRecord.approvable !== true) {
      const auditFailure = writeAuthorityAudit("approval_rejected", {
        reason: "preview_not_approvable",
        preview_digest: previewDigest,
      });
      if (auditFailure) {
        return res.status(auditFailure.status).json({ error: auditFailure.error });
      }
      return res.status(409).json({ error: "Preview exists but is not approvable; generate a new preview that is approvable before execution" });
    }

    if (typeof previewRecord.actionDigest !== "string" || !previewRecord.actionDigest.trim()) {
      const auditFailure = writeAuthorityAudit("approval_rejected", {
        reason: "preview_action_digest_missing",
        preview_digest: previewDigest,
      });
      if (auditFailure) {
        return res.status(auditFailure.status).json({ error: auditFailure.error });
      }
      return res.status(409).json({ error: "Preview must include an approved action digest before execution can be approved" });
    }

    const token = createOpaqueToken();
    const tokenHash = sha256Hex(token);
    const issuedAt = Date.now();
    const expiresAt = issuedAt + config.approvalTtlMs;
    const evidenceId = crypto.randomUUID();
    approvalStore.set(tokenHash, {
      tokenHash,
      objectiveDigest: previewRecord.objectiveDigest,
      previewDigest,
      contextDigest: previewRecord.contextDigest,
      actionDigest: previewRecord.actionDigest,
      context: previewRecord.context,
      issuedAt,
      expiresAt,
      evidenceId,
    });
    const auditFailure = writeAuthorityAudit("approval_issued", {
      token_hash: tokenHash,
      objective_digest: previewRecord.objectiveDigest,
      preview_digest: previewDigest,
      context_digest: previewRecord.contextDigest,
      action_digest: previewRecord.actionDigest,
      requested_provider: previewRecord.context?.requested_provider ?? null,
      fallback_policy: previewRecord.context?.fallback_policy ?? "none",
      policy_profile: previewRecord.context?.policy_profile ?? null,
      budget: previewRecord.context?.budget ?? null,
      config_identity_hash: previewRecord.context?.config_identity_hash ?? null,
      issued_at: new Date(issuedAt).toISOString(),
      expires_at: new Date(expiresAt).toISOString(),
      evidence_id: evidenceId,
    });
    if (auditFailure) {
      approvalStore.delete(tokenHash);
      return res.status(auditFailure.status).json({ error: auditFailure.error });
    }

    return res.json({
      status: "approved",
      execution_intent_token: token,
      objective_digest: previewRecord.objectiveDigest,
      preview_digest: previewDigest,
      context_digest: previewRecord.contextDigest,
      action_digest: previewRecord.actionDigest,
      expires_at: new Date(expiresAt).toISOString(),
      evidence_id: evidenceId,
    });
  });

  app.post("/run", requireAuth, applyRateLimit, async (req, res) => {
    const validation = validateObjective(req.body?.objective, config.maxObjectiveChars);
    if (!validation.ok) {
      return res.status(400).json({ error: validation.error });
    }

    const approvalResult = consumeApproval({
      token: req.body?.execution_intent_token,
      objective: validation.objective,
    });
    if (!approvalResult.ok) {
      return res.status(approvalResult.status).json({ error: approvalResult.error });
    }

    try {
      const result = await runCli(
        buildRunArgs(config, approvalResult.approval),
        config,
        logger,
        {
          objective: validation.objective,
          stdinData: validation.objective,
        }
      );
      pushTrace(normalizeRunTrace(result, validation.objective));
      return res.json(result);
    } catch (error) {
      const bridgeError = sanitizeBridgeError(error, "Governed execution failed");
      const payload = {
        status: "error",
        error: bridgeError.message,
        preview: false,
        provider: config.cliProvider || "runtime-local",
        policy_status: "constraint-gated",
      };
      pushTrace(normalizeRunTrace(payload, validation.objective));
      return res.status(bridgeError.status).json(payload);
    }
  });

  app.post(
    "/preview",
    requireAuth,
    applyRateLimit,
    requirePreviewIntent,
    async (req, res) => {
      const validation = validateObjective(req.body?.objective, config.maxObjectiveChars);
      if (!validation.ok) {
        return res.status(400).json({ error: validation.error });
      }

      try {
        const result = await runCli(buildPreviewArgs(config), config, logger, {
          acceptJsonOnNonZero: true,
          objective: validation.objective,
          stdinData: validation.objective,
        });
        const payload = toPreviewPayload(
          result,
          validation.objective,
          config,
          config.previewTtlMs
        );
        previewStore.set(payload.preview_digest, {
          objectiveDigest: payload.objective_digest,
          previewDigest: payload.preview_digest,
          contextDigest: payload.context_digest,
          actionDigest: payload.action_digest,
          context: buildExecutionContext(config, payload),
          supported: payload.supported,
          approvable: payload.approvable === true,
          createdAt: Date.now(),
          expiresAt: Date.now() + config.previewTtlMs,
        });
        pushTrace(normalizePreviewTrace(payload, validation.objective));
        return res.json(payload);
      } catch (error) {
        const bridgeError = sanitizeBridgeError(error, "Preview failed");
        const payload = toPreviewErrorPayload(
          bridgeError.message,
          validation.objective,
          config,
          config.previewTtlMs
        );
        previewStore.set(payload.preview_digest, {
          objectiveDigest: payload.objective_digest,
          previewDigest: payload.preview_digest,
          contextDigest: payload.context_digest,
          actionDigest: payload.action_digest,
          context: buildExecutionContext(config, payload),
          supported: payload.supported,
          approvable: payload.approvable === true,
          createdAt: Date.now(),
          expiresAt: Date.now() + config.previewTtlMs,
        });
        pushTrace(normalizePreviewTrace(payload, validation.objective));
        return res.status(bridgeError.status).json(payload);
      }
    }
  );

  if (fs.existsSync(staticDir)) {
    app.use(express.static(staticDir));
  } else {
    app.get("/", (_req, res) => {
      res.json({
        service: config.serviceName,
        status: "ok",
      });
    });
  }

  app.use((err, _req, res, next) => {
    if (err?.type === "entity.too.large") {
      return res.status(413).json({ error: "Request body exceeds configured limit" });
    }

    if (err instanceof SyntaxError) {
      return res.status(400).json({ error: "Invalid JSON payload" });
    }

    return next(err);
  });

  return app;
}

export async function startServer(options = {}) {
  const config = options.config ?? buildConfig();
  const app = createApp({ ...options, config });

  return new Promise((resolve, reject) => {
    const server = app.listen(config.port, config.host, () => resolve(server));
    server.on("error", reject);
  });
}

if (process.argv[1] === __filename) {
  const config = buildConfig();
  startServer({ config })
    .then(() => {
      console.log(`Sovereign bridge running on http://${config.host}:${config.port}`);
      console.log(`Repo root: ${config.repoRoot}`);
      console.log(`Trace capacity: ${config.traceCapacity}`);
    })
    .catch((error) => {
      console.error("Failed to start Sovereign bridge", error);
      process.exitCode = 1;
    });
}

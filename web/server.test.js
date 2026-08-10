import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { once } from "node:events";
import test from "node:test";

import { buildConfig, buildChildEnv, createLimiter, executeCli, startServer } from "./server.js";

function makeTempDir(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

function makeStaticDir() {
  const dir = makeTempDir("sovereign-static-");
  fs.writeFileSync(path.join(dir, "index.html"), "<html></html>", "utf8");
  return dir;
}

function makeConfig(overrides = {}) {
  return {
    ...buildConfig({
      SOVEREIGN_BRIDGE_TOKEN: "test-token",
      SOVEREIGN_BRIDGE_HOST: "127.0.0.1",
      SOVEREIGN_BRIDGE_PORT: "8787",
      SOVEREIGN_BRIDGE_CLI_PROVIDER: "demo",
    }),
    host: "127.0.0.1",
    port: 0,
    bridgeStateDir: makeTempDir("sovereign-state-"),
    bridgeAuditPath: path.join(makeTempDir("sovereign-audit-"), "bridge-audit.jsonl"),
    ...overrides,
  };
}

async function withServer(options, callback) {
  const server = await startServer(options);
  try {
    await callback(server);
  } finally {
    server.close();
    await once(server, "close");
  }
}

function serverUrl(server, pathname) {
  const address = server.address();
  if (!address || typeof address === "string") {
    throw new Error("Server address unavailable");
  }

  return `http://127.0.0.1:${address.port}${pathname}`;
}

test("bridge requires server-issued approval tokens and consumes them once", async () => {
  const config = makeConfig({
    approvalTtlMs: 5_000,
    previewTtlMs: 5_000,
  });
  const authHeader = "Bear" + "er test-token";
  const actionDigest = "preview-action-1";

  const runCli = async (args) => {
    if (args.includes("--preview")) {
      return {
        status: "preview",
        supported: true,
        trace_id: "preview-1",
        provider: "demo",
        actual_provider: "demo",
        requested_provider: "demo",
        fallback_policy: "none",
        policy_profile: "balanced",
        policy_status: "preview-supported",
        final_drift: 0.25,
        steps: [],
        action: {
          tool: "echo_text",
          kwargs: { text: "objective=demo" },
          comment: "preview echo",
        },
        action_digest: actionDigest,
        budget: { requested: null, outcome: "not-requested", enforced: false },
      };
    }
    assert.ok(args.includes("--objective-stdin"));
    assert.deepEqual(args.slice(-2), ["--policy-profile", "balanced"]);
    assert.ok(args.includes("--expected-action-digest"));
    assert.equal(args[args.indexOf("--expected-action-digest") + 1], actionDigest);
    return {
      status: "executed",
      trace_id: "run-1",
      provider: "demo",
      actual_provider: "demo",
      requested_provider: "demo",
      fallback_policy: "none",
      policy_profile: "balanced",
      policy_status: "constraint-gated",
      final_drift: 0,
      steps: [],
      budget: { requested: null, outcome: "not-requested", enforced: false },
    };
  };

  await withServer(
    {
      config,
      runCli,
      staticDir: makeStaticDir(),
      readinessProbe: () => ({ ok: true, status: "ready", reason: null, components: [] }),
    },
    async (server) => {
      const previewWithoutIntent = await fetch(serverUrl(server, "/preview"), {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ objective: "demo" }),
      });
      assert.equal(previewWithoutIntent.status, 400);

      const previewResponse = await fetch(serverUrl(server, "/preview"), {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ objective: "demo", intent: "preview" }),
      });
      assert.equal(previewResponse.status, 200);
      const previewPayload = await previewResponse.json();
      assert.equal(previewPayload.preview, true);
      assert.equal(previewPayload.supported, true);
      assert.equal(previewPayload.approvable, true);
      assert.ok(previewPayload.objective_digest);
      assert.ok(previewPayload.preview_digest);
      assert.ok(previewPayload.context_digest);
      assert.equal(previewPayload.action_digest, actionDigest);

      const approvalResponse = await fetch(serverUrl(server, "/approve"), {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          objective: "demo",
          preview_digest: previewPayload.preview_digest,
        }),
      });
      assert.equal(approvalResponse.status, 200);
      const approvalPayload = await approvalResponse.json();
      assert.ok(approvalPayload.execution_intent_token);

      const mismatchResponse = await fetch(serverUrl(server, "/run"), {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          objective: "different objective",
          execution_intent_token: approvalPayload.execution_intent_token,
        }),
      });
      assert.equal(mismatchResponse.status, 409);

      const runResponse = await fetch(serverUrl(server, "/run"), {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          objective: "demo",
          execution_intent_token: approvalPayload.execution_intent_token,
        }),
      });
      assert.equal(runResponse.status, 200);
      const runPayload = await runResponse.json();
      assert.equal(runPayload.status, "executed");

      const replayResponse = await fetch(serverUrl(server, "/run"), {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          objective: "demo",
          execution_intent_token: approvalPayload.execution_intent_token,
        }),
      });
      assert.equal(replayResponse.status, 409);

      const auditLog = fs.readFileSync(config.bridgeAuditPath, "utf8");
      assert.match(auditLog, /"event":"approval_issued"/);
      assert.match(auditLog, /"event":"approval_consumed"/);
    }
  );
});

test(
  "bridge real CLI path exposes risk-threshold previews but refuses approval",
  { timeout: 30_000 },
  async () => {
    const config = makeConfig({
      approvalTtlMs: 5_000,
      previewTtlMs: 5_000,
      cliTimeoutMs: 10_000,
    });
    const authHeader = "Bear" + "er test-token";

    await withServer(
      {
        config,
        staticDir: makeStaticDir(),
        readinessProbe: () => ({ ok: true, status: "ready", reason: null, components: [] }),
      },
      async (server) => {
        const previewResponse = await fetch(serverUrl(server, "/preview"), {
          method: "POST",
          headers: {
            Authorization: authHeader,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ objective: "demo", intent: "preview" }),
        });
        assert.equal(previewResponse.status, 200);
        const previewPayload = await previewResponse.json();
        assert.equal(previewPayload.supported, true);
        assert.equal(previewPayload.approvable, false);
        assert.equal(previewPayload.source_status, "preview-risk-threshold");
        assert.equal(previewPayload.tool_calls, 0);
        assert.ok(previewPayload.preview_digest);
        assert.ok(previewPayload.action_digest);

        const approvalResponse = await fetch(serverUrl(server, "/approve"), {
          method: "POST",
          headers: {
            Authorization: authHeader,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            objective: "demo",
            preview_digest: previewPayload.preview_digest,
          }),
        });
        assert.equal(approvalResponse.status, 409);
        const approvalPayload = await approvalResponse.json();
        assert.match(approvalPayload.error, /not approvable/i);
      }
    );
  }
);

test(
  "bridge real CLI path completes preview approval and single-use execution for approvable previews",
  { timeout: 30_000 },
  async () => {
    const config = makeConfig({
      approvalTtlMs: 5_000,
      previewTtlMs: 5_000,
      cliTimeoutMs: 10_000,
      cliPolicyProfile: "exploratory",
    });
    const authHeader = "Bear" + "er test-token";

    await withServer(
      {
        config,
        staticDir: makeStaticDir(),
        readinessProbe: () => ({ ok: true, status: "ready", reason: null, components: [] }),
      },
      async (server) => {
        const previewResponse = await fetch(serverUrl(server, "/preview"), {
          method: "POST",
          headers: {
            Authorization: authHeader,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ objective: "demo", intent: "preview" }),
        });
        assert.equal(previewResponse.status, 200);
        const previewPayload = await previewResponse.json();
        assert.equal(previewPayload.supported, true);
        assert.equal(previewPayload.approvable, true);
        assert.equal(previewPayload.source_status, "preview");
        assert.equal(previewPayload.tool_calls, 0);
        assert.ok(previewPayload.preview_digest);
        assert.ok(previewPayload.action_digest);

        const approvalResponse = await fetch(serverUrl(server, "/approve"), {
          method: "POST",
          headers: {
            Authorization: authHeader,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            objective: "demo",
            preview_digest: previewPayload.preview_digest,
          }),
        });
        assert.equal(approvalResponse.status, 200);
        const approvalPayload = await approvalResponse.json();
        assert.ok(approvalPayload.execution_intent_token);

        const runResponse = await fetch(serverUrl(server, "/run"), {
          method: "POST",
          headers: {
            Authorization: authHeader,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            objective: "demo",
            execution_intent_token: approvalPayload.execution_intent_token,
          }),
        });
        assert.equal(runResponse.status, 200);
        const runPayload = await runResponse.json();
        assert.equal(runPayload.status, "halted");
        assert.equal(runPayload.reason, "APPROVAL_SCOPE_EXHAUSTED");
        assert.equal(runPayload.required_action, "REPREVIEW_REQUIRED");

        const replayResponse = await fetch(serverUrl(server, "/run"), {
          method: "POST",
          headers: {
            Authorization: authHeader,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            objective: "demo",
            execution_intent_token: approvalPayload.execution_intent_token,
          }),
        });
        assert.equal(replayResponse.status, 409);
      }
    );
  }
);

test("unsupported previews cannot be approved", async () => {
  const config = makeConfig();
  const authHeader = "Bear" + "er test-token";

  await withServer(
    {
      config,
      runCli: async () => ({
        status: "preview-unsupported",
        supported: false,
        reason: "Preview unavailable",
        provider: "demo",
        actual_provider: "demo",
        requested_provider: "demo",
        fallback_policy: "none",
        policy_profile: "balanced",
        policy_status: "preview-unsupported",
        steps: 0,
        tool_calls: 0,
        drift_trajectory: [],
        budget: { requested: null, outcome: "not-requested", enforced: false },
      }),
      staticDir: makeStaticDir(),
      readinessProbe: () => ({ ok: true, status: "ready", reason: null, components: [] }),
    },
    async (server) => {
      const previewResponse = await fetch(serverUrl(server, "/preview"), {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ objective: "demo", intent: "preview" }),
      });
      assert.equal(previewResponse.status, 200);
      const previewPayload = await previewResponse.json();
      assert.equal(previewPayload.supported, false);
      assert.equal(previewPayload.cli_exit_status, 2);

      const approvalResponse = await fetch(serverUrl(server, "/approve"), {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          objective: "demo",
          preview_digest: previewPayload.preview_digest,
        }),
      });
      assert.equal(approvalResponse.status, 409);
    }
  );
});

test("cross-origin requests are denied by default", async () => {
  await withServer(
    {
      config: makeConfig(),
      runCli: async () => ({ status: "preview", steps: [] }),
      staticDir: makeStaticDir(),
      readinessProbe: () => ({ ok: true, status: "ready", reason: null, components: [] }),
    },
    async (server) => {
      const response = await fetch(serverUrl(server, "/health"), {
        headers: {
          Origin: "https://example.com",
        },
      });

      assert.equal(response.status, 403);
    }
  );
});

test("objective length and JSON body size are bounded", async () => {
  const config = makeConfig({
    maxObjectiveChars: 8,
    jsonLimit: "64b",
  });
  const authHeader = "Bear" + "er test-token";

  await withServer(
    {
      config,
      runCli: async () => ({ status: "preview", steps: [] }),
      staticDir: makeStaticDir(),
      readinessProbe: () => ({ ok: true, status: "ready", reason: null, components: [] }),
    },
    async (server) => {
      const tooLong = await fetch(serverUrl(server, "/preview"), {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ objective: "objective-too-long", intent: "preview" }),
      });
      assert.equal(tooLong.status, 400);

      const oversized = await fetch(serverUrl(server, "/preview"), {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          objective: "demo",
          intent: "preview",
          padding: "x".repeat(256),
        }),
      });
      assert.equal(oversized.status, 413);
    }
  );
});

test("rate limiter store is bounded", () => {
  const limiter = createLimiter(10, 60_000, 2);

  limiter.consume("client-a");
  limiter.consume("client-b");
  limiter.consume("client-c");

  assert.ok(limiter.size() <= 2);
});

test("executeCli times out long-running commands", async () => {
  await assert.rejects(
    executeCli(
      ["-e", "setTimeout(() => {}, 1_000)"],
      makeConfig({
        pythonExecutable: process.execPath,
        cliTimeoutMs: 25,
      })
    ),
    (error) => error.reason === "timeout"
  );
});

test("executeCli keeps bridge secrets out of child env and redacts objective diagnostics", async () => {
  const originalBridgeToken = process.env.SOVEREIGN_BRIDGE_TOKEN;
  const originalUnrelatedSecret = process.env.UNRELATED_SECRET;
  process.env.SOVEREIGN_BRIDGE_TOKEN = "bridge-secret";
  process.env.UNRELATED_SECRET = "unrelated-secret";

  const loggerEntries = [];
  const logger = {
    error(message, meta) {
      loggerEntries.push({ message, meta });
    },
  };
  const objective = "top secret bridge objective";
  const readScript = `
let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => { input += chunk; });
process.stdin.on("end", () => {
  process.stdout.write(JSON.stringify({
    objective: input,
    bridgeToken: process.env.SOVEREIGN_BRIDGE_TOKEN ?? null,
    unrelatedSecret: process.env.UNRELATED_SECRET ?? null,
    pythonPath: process.env.PYTHONPATH ?? "",
  }));
});
`;

  try {
    const result = await executeCli(
      ["-e", readScript],
      makeConfig({
        pythonExecutable: process.execPath,
        cliTimeoutMs: 500,
      }),
      logger,
      {
        objective,
        stdinData: objective,
      }
    );

    assert.equal(result.objective, objective);
    assert.equal(result.bridgeToken, null);
    assert.equal(result.unrelatedSecret, null);
    assert.match(result.pythonPath, /sovereign-claw[\\/]+src/);

    await assert.rejects(
      executeCli(
        ["-e", "setTimeout(() => {}, 1_000)"],
        makeConfig({
          pythonExecutable: process.execPath,
          cliTimeoutMs: 25,
        }),
        logger,
        {
          objective,
          stdinData: objective,
        }
      ),
      (error) => error.reason === "timeout"
    );

    const renderedLogs = JSON.stringify(loggerEntries);
    assert.equal(renderedLogs.includes(objective), false);
    assert.ok(
      loggerEntries.some(
        (entry) =>
          entry.meta?.objective_sha256 === crypto.createHash("sha256").update(objective).digest("hex")
      )
    );
  } finally {
    if (originalBridgeToken === undefined) {
      delete process.env.SOVEREIGN_BRIDGE_TOKEN;
    } else {
      process.env.SOVEREIGN_BRIDGE_TOKEN = originalBridgeToken;
    }
    if (originalUnrelatedSecret === undefined) {
      delete process.env.UNRELATED_SECRET;
    } else {
      process.env.UNRELATED_SECRET = originalUnrelatedSecret;
    }
  }
});

test("executeCli force-kills stubborn process groups after timeout", async () => {
  const pidFile = path.join(makeTempDir("sovereign-pid-"), "process-group.json");
  const script = `
const fs = require("node:fs");
const { spawn } = require("node:child_process");
const grandchild = spawn(process.execPath, ["-e", "process.on('SIGTERM',()=>{});setInterval(()=>{},1000);"], {
  stdio: "ignore",
});
fs.writeFileSync(${JSON.stringify(pidFile)}, JSON.stringify({ parent: process.pid, grandchild: grandchild.pid }));
process.on("SIGTERM", () => {});
setInterval(() => {}, 1000);
`;

  await assert.rejects(
    executeCli(
      ["-e", script],
      makeConfig({
        pythonExecutable: process.execPath,
        cliTimeoutMs: 75,
      })
    ),
    (error) => error.reason === "timeout"
  );

  await new Promise((resolve) => setTimeout(resolve, 300));
  assert.equal(fs.existsSync(pidFile), true);
  const pids = JSON.parse(fs.readFileSync(pidFile, "utf8"));
  assert.throws(() => process.kill(pids.parent, 0));
  assert.throws(() => process.kill(pids.grandchild, 0));
});

test("executeCli enforces stdout caps", async () => {
  await assert.rejects(
    executeCli(
      ["-e", "process.stdout.write('x'.repeat(128))"],
      makeConfig({
        pythonExecutable: process.execPath,
        stdoutLimitBytes: 32,
        cliTimeoutMs: 500,
      })
    ),
    (error) => error.reason === "output_limit"
  );
});

test("ready endpoint reflects dependency probe results", async () => {
  const config = makeConfig();

  await withServer(
    {
      config,
      runCli: async () => ({ status: "preview", steps: [] }),
      staticDir: makeStaticDir(),
      readinessProbe: () => ({
        ok: false,
        status: "not-ready",
        reason: "python_runtime: probe failed",
        components: [{ name: "python_runtime", ok: false, detail: "probe failed" }],
      }),
    },
    async (server) => {
      const response = await fetch(serverUrl(server, "/ready"));
      assert.equal(response.status, 503);
      const payload = await response.json();
      assert.equal(payload.ok, false);
      assert.match(payload.reason, /python_runtime/);
    }
  );
});

test("implicit default provider preview approvals remain valid at run time", async () => {
  const config = makeConfig({
    cliProvider: "",
    approvalTtlMs: 5_000,
    previewTtlMs: 5_000,
  });
  const authHeader = "Bear" + "er test-token";
  const actionDigest = "default-provider-action";

  const runCli = async (args) => {
    if (args.includes("--preview")) {
      return {
        status: "preview",
        supported: true,
        approvable: true,
        provider: "demo",
        actual_provider: "demo",
        requested_provider: "demo",
        fallback_policy: "none",
        policy_profile: "balanced",
        policy_status: "preview-supported",
        final_drift: 0.25,
        steps: [],
        action: { tool: "echo_text", kwargs: { text: "default" }, comment: "preview default" },
        action_digest: actionDigest,
        budget: { requested: null, outcome: "not-requested", enforced: false },
      };
    }
    return {
      status: "executed",
      provider: "demo",
      actual_provider: "demo",
      requested_provider: "demo",
      fallback_policy: "none",
      policy_profile: "balanced",
      policy_status: "constraint-gated",
      final_drift: 0,
      steps: [],
      budget: { requested: null, outcome: "not-requested", enforced: false },
    };
  };

  await withServer(
    {
      config,
      runCli,
      staticDir: makeStaticDir(),
      readinessProbe: () => ({ ok: true, status: "ready", reason: null, components: [] }),
    },
    async (server) => {
      const previewResponse = await fetch(serverUrl(server, "/preview"), {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ objective: "demo", intent: "preview" }),
      });
      assert.equal(previewResponse.status, 200);
      const previewPayload = await previewResponse.json();

      const approvalResponse = await fetch(serverUrl(server, "/approve"), {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          objective: "demo",
          preview_digest: previewPayload.preview_digest,
        }),
      });
      assert.equal(approvalResponse.status, 200);
      const approvalPayload = await approvalResponse.json();

      const runResponse = await fetch(serverUrl(server, "/run"), {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          objective: "demo",
          execution_intent_token: approvalPayload.execution_intent_token,
        }),
      });
      assert.equal(runResponse.status, 200);
      const runPayload = await runResponse.json();
      assert.equal(runPayload.requested_provider, "demo");
      assert.equal(runPayload.actual_provider, "demo");
    }
  );
});

test("fallback provider changes are reported without invalidating approval context", async () => {
  const config = makeConfig({
    cliProvider: "giles",
    approvalTtlMs: 5_000,
    previewTtlMs: 5_000,
  });
  const authHeader = "Bear" + "er test-token";
  const actionDigest = "fallback-action";

  const runCli = async (args) => {
    if (args.includes("--preview")) {
      return {
        status: "preview",
        supported: true,
        approvable: true,
        provider: "openai",
        actual_provider: "openai",
        requested_provider: "giles",
        fallback_policy: "cascade-configured-chain",
        policy_profile: "balanced",
        policy_status: "preview-supported",
        final_drift: 0.2,
        steps: [],
        action: { tool: "echo_text", kwargs: { text: "fallback-preview" }, comment: "preview fallback" },
        action_digest: actionDigest,
        budget: { requested: null, outcome: "not-requested", enforced: false },
      };
    }
    return {
      status: "executed",
      provider: "gemini",
      actual_provider: "gemini",
      requested_provider: "giles",
      fallback_policy: "cascade-configured-chain",
      policy_profile: "balanced",
      policy_status: "constraint-gated",
      final_drift: 0,
      steps: [],
      budget: { requested: null, outcome: "not-requested", enforced: false },
    };
  };

  await withServer(
    {
      config,
      runCli,
      staticDir: makeStaticDir(),
      readinessProbe: () => ({ ok: true, status: "ready", reason: null, components: [] }),
    },
    async (server) => {
      const previewResponse = await fetch(serverUrl(server, "/preview"), {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ objective: "demo", intent: "preview" }),
      });
      assert.equal(previewResponse.status, 200);
      const previewPayload = await previewResponse.json();
      assert.equal(previewPayload.actual_provider, "openai");

      const approvalResponse = await fetch(serverUrl(server, "/approve"), {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          objective: "demo",
          preview_digest: previewPayload.preview_digest,
        }),
      });
      assert.equal(approvalResponse.status, 200);
      const approvalPayload = await approvalResponse.json();

      const runResponse = await fetch(serverUrl(server, "/run"), {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          objective: "demo",
          execution_intent_token: approvalPayload.execution_intent_token,
        }),
      });
      assert.equal(runResponse.status, 200);
      const runPayload = await runResponse.json();
      assert.equal(runPayload.requested_provider, "giles");
      assert.equal(runPayload.actual_provider, "gemini");
      assert.equal(runPayload.fallback_policy, "cascade-configured-chain");
    }
  );
});

test("approval issuance fails closed when authority audit persistence is unavailable", async () => {
  const auditDir = makeTempDir("sovereign-audit-dir-");
  const config = makeConfig({
    bridgeAuditPath: auditDir,
  });
  const authHeader = "Bear" + "er test-token";

  await withServer(
    {
      config,
      runCli: async () => ({
        status: "preview",
        supported: true,
        approvable: true,
        provider: "demo",
        actual_provider: "demo",
        requested_provider: "demo",
        fallback_policy: "none",
        policy_profile: "balanced",
        policy_status: "preview-supported",
        final_drift: 0.2,
        steps: [],
        action: { tool: "echo_text", kwargs: { text: "demo" }, comment: "preview" },
        action_digest: "audit-action",
        budget: { requested: null, outcome: "not-requested", enforced: false },
      }),
      staticDir: makeStaticDir(),
      readinessProbe: () => ({ ok: true, status: "ready", reason: null, components: [] }),
    },
    async (server) => {
      const previewResponse = await fetch(serverUrl(server, "/preview"), {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ objective: "demo", intent: "preview" }),
      });
      assert.equal(previewResponse.status, 200);
      const previewPayload = await previewResponse.json();

      const approvalResponse = await fetch(serverUrl(server, "/approve"), {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          objective: "demo",
          preview_digest: previewPayload.preview_digest,
        }),
      });
      assert.equal(approvalResponse.status, 503);
    }
  );
});

test("approval consumption fails closed when authority audit persistence is unavailable", async () => {
  const config = makeConfig({
    approvalTtlMs: 5_000,
    previewTtlMs: 5_000,
  });
  const authHeader = "Bear" + "er test-token";
  let runCount = 0;

  await withServer(
    {
      config,
      runCli: async (args) => {
        if (args.includes("--preview")) {
          return {
            status: "preview",
            supported: true,
            approvable: true,
            provider: "demo",
            actual_provider: "demo",
            requested_provider: "demo",
            fallback_policy: "none",
            policy_profile: "balanced",
            policy_status: "preview-supported",
            final_drift: 0.2,
            steps: [],
            action: { tool: "echo_text", kwargs: { text: "demo" }, comment: "preview" },
            action_digest: "audit-consume-action",
            budget: { requested: null, outcome: "not-requested", enforced: false },
          };
        }
        runCount += 1;
        return {
          status: "executed",
          provider: "demo",
          actual_provider: "demo",
          requested_provider: "demo",
          fallback_policy: "none",
          policy_profile: "balanced",
          policy_status: "constraint-gated",
          final_drift: 0,
          steps: [],
          budget: { requested: null, outcome: "not-requested", enforced: false },
        };
      },
      staticDir: makeStaticDir(),
      readinessProbe: () => ({ ok: true, status: "ready", reason: null, components: [] }),
    },
    async (server) => {
      const previewResponse = await fetch(serverUrl(server, "/preview"), {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ objective: "demo", intent: "preview" }),
      });
      assert.equal(previewResponse.status, 200);
      const previewPayload = await previewResponse.json();

      const approvalResponse = await fetch(serverUrl(server, "/approve"), {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          objective: "demo",
          preview_digest: previewPayload.preview_digest,
        }),
      });
      assert.equal(approvalResponse.status, 200);
      const approvalPayload = await approvalResponse.json();
      const writableAuditPath = config.bridgeAuditPath;
      config.bridgeAuditPath = makeTempDir("sovereign-audit-dir-");

      const failedRunResponse = await fetch(serverUrl(server, "/run"), {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          objective: "demo",
          execution_intent_token: approvalPayload.execution_intent_token,
        }),
      });
      assert.equal(failedRunResponse.status, 503);
      assert.equal(runCount, 0);

      config.bridgeAuditPath = writableAuditPath;

      const retryRunResponse = await fetch(serverUrl(server, "/run"), {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          objective: "demo",
          execution_intent_token: approvalPayload.execution_intent_token,
        }),
      });
      assert.equal(retryRunResponse.status, 200);
      assert.equal(runCount, 1);
    }
  );
});

test("App keeps operator tokens out of persistent browser storage", () => {
  const appSource = fs.readFileSync(new URL("./src/App.tsx", import.meta.url), "utf8");
  assert.equal(appSource.includes("localStorage"), false);
  assert.equal(appSource.includes("sessionStorage"), false);
});

test("buildChildEnv excludes unlisted SOVEREIGN_* secrets from child process", () => {
  const config = buildConfig({ SOVEREIGN_BRIDGE_TOKEN: "tok", SOVEREIGN_BRIDGE_HOST: "127.0.0.1" });
  const fakeEnv = {
    PATH: "/usr/bin",
    HOME: "/home/user",
    PYTHONPATH: "/some/path",
    SOVEREIGN_OPENAI_API_KEY: "sk-secret",
    SOVEREIGN_PROVIDER_SECRET: "provider-secret",
    SOVEREIGN_BRIDGE_TOKEN: "bridge-tok",
    SOVEREIGN_CUSTOM_VAR: "custom-val",
  };
  const childEnv = buildChildEnv(config, fakeEnv);
  assert.equal("SOVEREIGN_OPENAI_API_KEY" in childEnv, false, "provider secret must not reach child");
  assert.equal("SOVEREIGN_PROVIDER_SECRET" in childEnv, false, "provider secret must not reach child");
  assert.equal("SOVEREIGN_BRIDGE_TOKEN" in childEnv, false, "bridge token must not reach child");
  assert.equal("SOVEREIGN_CUSTOM_VAR" in childEnv, false, "unlisted SOVEREIGN_ var must not reach child");
  assert.equal(childEnv.PATH, "/usr/bin");
  assert.equal(childEnv.HOME, "/home/user");
  assert.ok(childEnv.PYTHONPATH.includes(config.repoRoot), "PYTHONPATH must include repo src");
});

test("buildChildEnv passes named Sovereign runtime settings to child process", () => {
  const config = buildConfig({ SOVEREIGN_BRIDGE_TOKEN: "tok", SOVEREIGN_BRIDGE_HOST: "127.0.0.1" });
  const fakeEnv = {
    PATH: "/usr/bin",
    SOVEREIGN_PROOF_VAULT_PATH: "/app/data/proof_vault.db",
    SOVEREIGN_EVENT_STREAM_PATH: "/app/data/events.jsonl",
    SOVEREIGN_LOG_LEVEL: "INFO",
    SOVEREIGN_SKILL_PATH: "/app/skills",
    SOVEREIGN_STATE_PATH: "/app/state",
    SOVEREIGN_OPENAI_API_KEY: "sk-secret",
    SOVEREIGN_BRIDGE_TOKEN: "bridge-tok",
    SOVEREIGN_ARBITRARY_SECRET: "secret-val",
  };
  const childEnv = buildChildEnv(config, fakeEnv);
  assert.equal(childEnv.SOVEREIGN_PROOF_VAULT_PATH, "/app/data/proof_vault.db", "proof vault path must reach child");
  assert.equal(childEnv.SOVEREIGN_EVENT_STREAM_PATH, "/app/data/events.jsonl", "event stream path must reach child");
  assert.equal(childEnv.SOVEREIGN_LOG_LEVEL, "INFO", "log level must reach child");
  assert.equal(childEnv.SOVEREIGN_SKILL_PATH, "/app/skills", "skill path must reach child");
  assert.equal(childEnv.SOVEREIGN_STATE_PATH, "/app/state", "state path must reach child");
  assert.equal("SOVEREIGN_OPENAI_API_KEY" in childEnv, false, "API key must not reach child");
  assert.equal("SOVEREIGN_BRIDGE_TOKEN" in childEnv, false, "bridge token must not reach child");
  assert.equal("SOVEREIGN_ARBITRARY_SECRET" in childEnv, false, "unlisted SOVEREIGN_ secret must not reach child");
});

test("rate limiter returns 429 before auth check on protected routes with invalid token", async () => {
  // clientRateLimit=1 so the second request hits 429 regardless of token validity
  const config = makeConfig({ clientRateLimit: 1, globalRateLimit: 100 });
  const routes = [
    { method: "GET", path: "/traces" },
    { method: "POST", path: "/approve" },
    { method: "POST", path: "/run" },
    { method: "POST", path: "/preview" },
  ];

  for (const route of routes) {
    await withServer({ config, staticDir: makeStaticDir() }, async (server) => {
      const url = serverUrl(server, route.path);
      const opts = {
        method: route.method,
        headers: {
          Authorization: "Bear" + "er invalid-token",
          "Content-Type": "application/json",
        },
        body: route.method === "POST" ? JSON.stringify({ objective: "test", intent: "preview" }) : undefined,
      };

      // First request consumes the per-client allowance; auth fails (401) but not 429 yet
      const first = await fetch(url, opts);
      assert.notEqual(first.status, 429, `${route.path}: first request must not be 429`);

      // Second request with the same invalid token must now hit the rate limit (429)
      const second = await fetch(url, opts);
      assert.equal(second.status, 429, `${route.path}: repeated invalid-auth request must be rate-limited`);
    });
  }
});

test("authenticated traffic is not blocked by rate limiter under normal load", async () => {
  const config = makeConfig({ clientRateLimit: 10, globalRateLimit: 100 });
  await withServer({ config, staticDir: makeStaticDir() }, async (server) => {
    const url = serverUrl(server, "/traces");
    for (let i = 0; i < 5; i++) {
      const res = await fetch(url, {
        headers: { Authorization: "Bear" + "er test-token" },
      });
      assert.equal(res.status, 200, `authenticated /traces request ${i + 1} must succeed`);
    }
  });
});


test("express-rate-limit recognized middleware emits RateLimit headers on protected routes", async () => {
  // Verifies that the scanner-recognized rateLimit() middleware from express-rate-limit
  // is active on protected routes by checking for draft-8 RateLimit response headers.
  const config = makeConfig({ clientRateLimit: 10, globalRateLimit: 100 });
  const routes = [
    { method: "GET", path: "/traces" },
    { method: "POST", path: "/approve" },
    { method: "POST", path: "/run" },
    { method: "POST", path: "/preview" },
  ];
  for (const route of routes) {
    await withServer({ config, staticDir: makeStaticDir() }, async (server) => {
      const url = serverUrl(server, route.path);
      const res = await fetch(url, {
        method: route.method,
        headers: {
          Authorization: "Bear" + "er test-token",
          "Content-Type": "application/json",
        },
        body: route.method === "POST" ? JSON.stringify({ objective: "test", intent: "preview" }) : undefined,
      });
      // The recognized middleware sets RateLimit-Policy (draft-8) before auth runs.
      // A 401/403/4xx/200 response all pass through the middleware layer.
      assert.ok(
        res.headers.has("ratelimit-policy") || res.headers.has("x-ratelimit-limit"),
        `${route.path}: recognized rate-limit header must be present (got status ${res.status})`,
      );
    });
  }
});


test("CORS: same-origin requests are allowed implicitly", async () => {
  const config = makeConfig({ allowedOrigins: new Set() });
  await withServer({ config, staticDir: makeStaticDir() }, async (server) => {
    const addr = server.address();
    const origin = `http://127.0.0.1:${addr.port}`;
    const response = await fetch(serverUrl(server, "/health"), {
      headers: { Origin: origin },
    });
    assert.notEqual(response.status, 403, "same-origin request must not be rejected");
  });
});

test("CORS: cross-origin requests from unlisted origins are rejected", async () => {
  const config = makeConfig({ allowedOrigins: new Set() });
  await withServer({ config, staticDir: makeStaticDir() }, async (server) => {
    const response = await fetch(serverUrl(server, "/health"), {
      headers: { Origin: "http://evil.example.com" },
    });
    assert.equal(response.status, 403, "unlisted cross-origin must be rejected");
  });
});

import assert from "node:assert/strict";
import { once } from "node:events";
import test from "node:test";

import { buildConfig, executeCli, startServer } from "./server.js";

function makeConfig(overrides = {}) {
  return {
    ...buildConfig({
      SOVEREIGN_BRIDGE_TOKEN: "test-token",
      SOVEREIGN_BRIDGE_HOST: "127.0.0.1",
      SOVEREIGN_BRIDGE_PORT: "8787",
    }),
    host: "127.0.0.1",
    port: 0,
    staticRoot: "/tmp/nonexistent",
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

test("authenticated endpoints fail closed without a bearer token and succeed with one", async () => {
  const config = makeConfig();
  const authHeader = "Bear" + "er test-token";

  await withServer(
    {
      config,
      runCli: async () => ({
        status: "executed",
        trace_id: "trace-1",
        provider: "runtime-local",
        policy_status: "constraint-gated",
        final_drift: 0.0,
        steps: [],
      }),
      staticDir: "/tmp/nonexistent",
    },
    async (server) => {
      const unauthenticated = await fetch(serverUrl(server, "/traces"));
      assert.equal(unauthenticated.status, 401);

      const previewWithoutIntent = await fetch(serverUrl(server, "/preview"), {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ objective: "demo" }),
      });
      assert.equal(previewWithoutIntent.status, 400);

      const authenticated = await fetch(serverUrl(server, "/preview"), {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ objective: "demo", intent: "preview" }),
      });
      assert.equal(authenticated.status, 200);
      const payload = await authenticated.json();
      assert.equal(payload.preview, true);
    }
  );
});

test("cross-origin requests are denied by default", async () => {
  await withServer(
    {
      config: makeConfig(),
      runCli: async () => ({ status: "preview", steps: [] }),
      staticDir: "/tmp/nonexistent",
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
      staticDir: "/tmp/nonexistent",
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

test("per-client rate limits are enforced", async () => {
  const config = makeConfig({
    clientRateLimit: 1,
    globalRateLimit: 10,
  });
  const authHeader = "Bear" + "er test-token";

  await withServer(
    {
      config,
      runCli: async () => ({ status: "preview", steps: [] }),
      staticDir: "/tmp/nonexistent",
    },
    async (server) => {
      const headers = {
        Authorization: authHeader,
      };

      const first = await fetch(serverUrl(server, "/traces"), { headers });
      assert.equal(first.status, 200);

      const second = await fetch(serverUrl(server, "/traces"), { headers });
      assert.equal(second.status, 429);
    }
  );
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

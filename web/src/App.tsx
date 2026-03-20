import { useState } from "react";

type RunResult = {
  status?: string;
  reason?: string;
  trace_id?: string;
  final_drift?: number | string;
  steps?: unknown[] | number;
  drift_trajectory?: Array<number | string>;
};

function App() {
  const [objective, setObjective] = useState("");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<RunResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const runObjective = async () => {
    if (!objective.trim()) return;

    setLoading(true);
    setError(null);

    try {
      const res = await fetch("http://localhost:8787/run", {
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
      setError(err instanceof Error ? err.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      style={{
        minHeight: "100vh",
        background: "#05060a",
        color: "#7CFFB2",
        padding: "24px",
        fontFamily:
          'Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
      }}
    >
      <div style={{ maxWidth: 960, margin: "0 auto" }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 16,
            marginBottom: 24,
          }}
        >
          <img
            src="/giles.png"
            alt="Giles"
            style={{
              width: 88,
              height: 88,
              objectFit: "cover",
              border: "1px solid #7CFFB2",
              display: "block",
            }}
          />
          <div>
            <h1 style={{ margin: 0, fontSize: 28 }}>Sovereign Claw</h1>
            <p style={{ margin: "6px 0 0", color: "#4ecb88" }}>
              governed execution shell
            </p>
          </div>
        </div>

        <div
          style={{
            border: "1px solid #2d7a56",
            padding: 16,
            marginBottom: 16,
            background: "#0b0d12",
          }}
        >
          <label
            htmlFor="objective"
            style={{ display: "block", marginBottom: 8, fontSize: 14 }}
          >
            Objective
          </label>

          <input
            id="objective"
            value={objective}
            onChange={(e) => setObjective(e.target.value)}
            placeholder="Enter objective..."
            style={{
              width: "100%",
              boxSizing: "border-box",
              padding: 12,
              marginBottom: 12,
              background: "#05060a",
              color: "#7CFFB2",
              border: "1px solid #2d7a56",
              outline: "none",
            }}
          />

          <button
            type="button"
            onClick={runObjective}
            disabled={loading}
            style={{
              padding: "10px 16px",
              background: loading ? "#1b2a22" : "#0d1b14",
              color: "#7CFFB2",
              border: "1px solid #7CFFB2",
              cursor: loading ? "wait" : "pointer",
            }}
          >
            {loading ? "Running..." : "Run Governed"}
          </button>
        </div>

        {error && (
          <div
            style={{
              border: "1px solid #c84b4b",
              color: "#ff8b8b",
              background: "#160909",
              padding: 16,
              marginBottom: 16,
            }}
          >
            {error}
          </div>
        )}

        {result && (
          <div
            style={{
              border: "1px solid #2d7a56",
              padding: 16,
              background: "#0b0d12",
              lineHeight: 1.6,
            }}
          >
            <div>
              <strong>Status:</strong> {result.status ?? "-"}
            </div>

            <div>
              <strong>Reason:</strong> {result.reason ?? "-"}
            </div>

            <div>
              <strong>Trace ID:</strong> {result.trace_id ?? "-"}
            </div>

            <div>
              <strong>Final Drift:</strong> {result.final_drift ?? "-"}
            </div>

            <div>
              <strong>Steps:</strong>{" "}
              {Array.isArray(result.steps)
                ? result.steps.length
                : result.steps ?? "-"}
            </div>

            <div>
              <strong>Drift Trajectory:</strong>{" "}
              {result.drift_trajectory?.length
                ? result.drift_trajectory.join(" → ")
                : "-"}
            </div>

            {Array.isArray(result.steps) && result.steps.length > 0 && (
              <div style={{ marginTop: 12 }}>
                <strong>Step Detail:</strong>
                <div
                  style={{
                    marginTop: 8,
                    fontSize: 12,
                    whiteSpace: "pre-wrap",
                    wordBreak: "break-word",
                  }}
                >
                  {result.steps.map((step, i) => (
                    <div key={i}>{JSON.stringify(step)}</div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export default App;
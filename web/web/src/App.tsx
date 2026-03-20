import { useState } from "react";

type RunResult = {
  status?: string;
  reason?: string;
  trace_id?: string;
  final_drift?: number | string;
  steps?: unknown[];
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
      const message =
        err instanceof Error ? err.message : "Unknown error";
      setError(message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-black p-6 font-mono text-green-400">
      <div className="mx-auto max-w-4xl space-y-6">
        <div className="flex items-center gap-4">
          <img
            src="/giles.png"
            alt="Giles"
            className="h-20 w-20 border border-green-500 object-cover"
          />
          <div>
            <h1 className="text-xl">Sovereign Claw</h1>
            <p className="text-sm text-green-600">
              governed execution shell
            </p>
          </div>
        </div>

        <div className="space-y-3 border border-green-700 p-4">
          <label className="block text-sm">Objective</label>

          <input
            value={objective}
            onChange={(e) => setObjective(e.target.value)}
            className="w-full border border-green-700 bg-black p-2 outline-none"
            placeholder="Enter objective..."
          />

          <button
            onClick={runObjective}
            className="border border-green-500 px-4 py-2 transition hover:bg-green-900"
          >
            {loading ? "Running..." : "Run Governed"}
          </button>
        </div>

        {error && (
          <div className="border border-red-500 p-3 text-red-400">
            {error}
          </div>
        )}

        {result && (
          <div className="space-y-3 border border-green-700 p-4">
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
              <strong>Steps:</strong>
              <div className="mt-1 space-y-1 text-xs">
                {result.steps?.length ? (
                  result.steps.map((step, i) => (
                    <div key={i}>{JSON.stringify(step)}</div>
                  ))
                ) : (
                  <div>-</div>
                )}
              </div>
            </div>

            <div>
              <strong>Drift Trajectory:</strong>{" "}
              {result.drift_trajectory?.length
                ? result.drift_trajectory.join(" → ")
                : "-"}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default App;
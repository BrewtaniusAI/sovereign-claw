import express from "express";
import cors from "cors";
import { spawn } from "child_process";
import path from "path";
import { fileURLToPath } from "url";

const app = express();
app.use(cors());
app.use(express.json());

const PORT = 8787;

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const repoRoot = path.resolve(__dirname, "..");

// Deterministic CLI execution wrapper
function runSovereign(objective) {
  return new Promise((resolve, reject) => {
    const proc = spawn(
      "python",
      ["-m", "sovereign_claw.cli", "run", objective, "--json"],
      {
        cwd: repoRoot,
        shell: false,
      }
    );

    let stdout = "";
    let stderr = "";

    proc.stdout.on("data", (data) => {
      stdout += data.toString();
    });

    proc.stderr.on("data", (data) => {
      stderr += data.toString();
    });

    proc.on("error", (err) => {
      reject({
        error: "Failed to start CLI process",
        detail: err.message,
      });
    });

    proc.on("close", (code) => {
      if (code !== 0) {
        return reject({
          error: "CLI execution failed",
          code,
          stderr,
          stdout,
        });
      }

      try {
        const parsed = JSON.parse(stdout);
        resolve(parsed);
      } catch {
        reject({
          error: "Invalid JSON from CLI",
          raw: stdout,
          stderr,
        });
      }
    });
  });
}

// API endpoint
app.post("/run", async (req, res) => {
  const { objective } = req.body;

  if (!objective || typeof objective !== "string") {
    return res.status(400).json({
      error: "Invalid objective",
    });
  }

  try {
    const result = await runSovereign(objective);
    res.json(result);
  } catch (err) {
    res.status(500).json(err);
  }
});

app.listen(PORT, () => {
  console.log(`Sovereign bridge running on http://localhost:${PORT}`);
  console.log(`Repo root: ${repoRoot}`);
});
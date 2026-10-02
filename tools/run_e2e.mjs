#!/usr/bin/env node
/*
 * Offline M3 browser acceptance runner.
 *
 * Starts only two child processes owned by this runner: the loopback FastAPI
 * service and the loopback Vite dev server.  It chooses ephemeral ports,
 * writes screenshots/evidence under work/e2e, and always terminates children.
 */
import net from "node:net";
import path from "node:path";
import fs from "node:fs/promises";
import fsSync from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import { spawn } from "node:child_process";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const nodeModules = process.env.WF_NODE_MODULES || path.join(process.env.USERPROFILE || "", ".cache", "codex-runtimes", "codex-primary-runtime", "dependencies", "node", "node_modules");
if (nodeModules) process.env.NODE_PATH = [nodeModules, process.env.NODE_PATH || ""].filter(Boolean).join(path.delimiter);
const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const { runAcceptance } = await import(pathToFileURL(path.join(root, "tests", "e2e", "m3_acceptance.mjs")).href);

const python = process.env.WF_PYTHON || (process.platform === "win32"
  ? path.join(root, "work", "python311", "python.exe")
  : path.join(root, "work", "python311", "bin", "python"));
const node = process.execPath;
const artifactDir = process.env.WF_E2E_ARTIFACT_DIR || path.join(root, "work", "e2e", `run-${new Date().toISOString().replace(/[:.]/g, "-")}`);
await fs.mkdir(artifactDir, { recursive: true });

function browserExecutable() {
  const candidates = [
    process.env.WF_BROWSER_EXECUTABLE,
    chromium.executablePath(),
    process.env.ProgramFiles && path.join(process.env.ProgramFiles, "Google", "Chrome", "Application", "chrome.exe"),
    process.env["ProgramFiles(x86)"] && path.join(process.env["ProgramFiles(x86)"], "Microsoft", "Edge", "Application", "msedge.exe"),
  ].filter(Boolean);
  return candidates.find((candidate) => fsSync.existsSync(candidate));
}
const browserPath = browserExecutable();
if (!browserPath) throw new Error("No local Chromium/Chrome/Edge executable found; refusing to download a browser in offline mode");

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
}

function childCommand(command, args, options = {}) {
  const child = spawn(command, args, {
    cwd: options.cwd || root,
    env: { ...process.env, ...options.env, PYTHONPATH: [path.join(root, "src"), process.env.PYTHONPATH || ""].filter(Boolean).join(path.delimiter) },
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  const log = [];
  child.stdout.on("data", (chunk) => { log.push(String(chunk)); if (log.length > 80) log.shift(); });
  child.stderr.on("data", (chunk) => { log.push(String(chunk)); if (log.length > 80) log.shift(); });
  child._wfLog = log;
  return child;
}

async function waitHealth(apiUrl, child) {
  let lastError;
  let ready = 0;
  for (let i = 0; i < 120; i += 1) {
    if (child.exitCode !== null) break;
    try {
      const response = await fetch(`${apiUrl}/health`);
      if (response.ok) {
        ready += 1;
        if (ready >= 3) return;
      } else {
        ready = 0;
      }
      lastError = new Error(`health ${response.status}`);
    } catch (error) { lastError = error; }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`API did not start: ${lastError?.message || "unknown"}\n${child._wfLog.join("")}`);
}

async function stop(child) {
  if (!child || child.exitCode !== null) return;
  if (process.platform === "win32") {
    await new Promise((resolve) => {
      const killer = spawn("taskkill", ["/pid", String(child.pid), "/t", "/f"], { stdio: "ignore", windowsHide: true });
      killer.once("close", resolve);
      killer.once("error", resolve);
    });
    // taskkill returns after requesting termination; wait until Node observes
    // the child exit before reusing its database and loopback port.
    if (child.exitCode === null) await new Promise((resolve) => {
      const timer = setTimeout(resolve, 2000);
      child.once("close", () => { clearTimeout(timer); resolve(); });
    });
  } else {
    child.kill("SIGTERM");
    await new Promise((resolve) => { child.once("close", resolve); setTimeout(resolve, 3000); });
  }
}

let backend;
let frontend;
let backendPort;
let frontendPort;
let dataDir = path.join(artifactDir, "data");
await fs.mkdir(dataDir, { recursive: true });

async function startBackend() {
  if (!backendPort) backendPort = await freePort();
  // Reserve the UI port first so FastAPI's strict local CORS allow-list can
  // include this run's ephemeral origin.
  if (!frontendPort) frontendPort = await freePort();
  const env = {
    WF_DATA_DIR: dataDir,
    WF_WEB_DATA_DIR: dataDir,
    WF_WEB_DB_PATH: path.join(dataDir, "worlds.sqlite"),
    WF_DB_PATH: path.join(dataDir, "worlds.sqlite"),
    WORLDFORGE_DB: path.join(dataDir, "worlds.sqlite"),
    WORLDFORGE_UI_ORIGINS: `http://127.0.0.1:${frontendPort}`,
    WF_CHAIN_MODE: "off",
    WF_AI_MODE: "offline",
  };
  backend = childCommand(python, ["-m", "uvicorn", "wf.web.main:app", "--host", "127.0.0.1", "--port", String(backendPort)], { env });
  await waitHealth(`http://127.0.0.1:${backendPort}`, backend);
}

async function startFrontend() {
  if (!frontendPort) frontendPort = await freePort();
  const vite = path.join(root, "web", "node_modules", "vite", "bin", "vite.js");
  frontend = childCommand(node, [vite, "--host", "127.0.0.1", "--port", String(frontendPort)], { cwd: path.join(root, "web") });
  for (let i = 0; i < 120; i += 1) {
    if (frontend.exitCode !== null) break;
    try { if ((await fetch(`http://127.0.0.1:${frontendPort}/`)).ok) return; } catch { /* wait */ }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`Vite did not start:\n${frontend?._wfLog?.join("") || ""}`);
}

async function restartBackend() {
  await stop(backend);
  await new Promise((resolve) => setTimeout(resolve, 400));
  await startBackend();
}

try {
  await startBackend();
  await startFrontend();
  const evidence = await runAcceptance({
    chromium,
    browserExecutable: browserPath,
    apiUrl: `http://127.0.0.1:${backendPort}`,
    uiUrl: `http://127.0.0.1:${frontendPort}`,
    artifactDir,
    restartBackend,
  });
  await fs.writeFile(path.join(artifactDir, "result.json"), JSON.stringify({ ok: true, ...evidence }, null, 2), "utf8");
  console.log(JSON.stringify({ ok: true, artifactDir, checks: evidence.checks.map((entry) => entry.name) }, null, 2));
} catch (error) {
  await fs.writeFile(path.join(artifactDir, "process-logs.txt"), ["[backend]", backend?._wfLog?.join("") || "", "[frontend]", frontend?._wfLog?.join("") || ""].join("\n"), "utf8");
  await fs.writeFile(path.join(artifactDir, "result.json"), JSON.stringify({ ok: false, artifactDir, error: String(error?.stack || error) }, null, 2), "utf8");
  console.error(String(error?.stack || error));
  process.exitCode = 1;
} finally {
  await fs.writeFile(path.join(artifactDir, "process-logs.txt"), ["[backend]", backend?._wfLog?.join("") || "", "[frontend]", frontend?._wfLog?.join("") || ""].join("\n"), "utf8").catch(() => {});
  await stop(frontend);
  await stop(backend);
}

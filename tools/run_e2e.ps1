param(
  [string]$ArtifactDir = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$node = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
$nodeModules = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules"
if (-not (Test-Path -LiteralPath $node)) {
  throw "Bundled Node.js was not found at $node"
}
$env:NODE_PATH = $nodeModules
if ($ArtifactDir) {
  $env:WF_E2E_ARTIFACT_DIR = $ArtifactDir
}
& $node (Join-Path $PSScriptRoot "run_e2e.mjs")
exit $LASTEXITCODE

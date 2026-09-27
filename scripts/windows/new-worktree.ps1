[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Branch,

    [ValidateNotNullOrEmpty()]
    [string]$Name,

    [ValidateNotNullOrEmpty()]
    [string]$WorktreeRoot,

    [switch]$Create,

    [ValidateNotNullOrEmpty()]
    [string]$CreateFrom = "origin/main"
)

$ErrorActionPreference = "Stop"

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [switch]$AllowFailure
    )

    $output = & git @Arguments 2>&1
    $exitCode = $LASTEXITCODE

    if (-not $AllowFailure -and $exitCode -ne 0) {
        $detail = $output -join [Environment]::NewLine
        throw "git $($Arguments -join ' ') failed with exit code $exitCode. $detail"
    }

    return [pscustomobject]@{
        ExitCode = $exitCode
        Output   = @($output)
    }
}

$repoProbe = Invoke-Git -Arguments @("rev-parse", "--show-toplevel")
$Repo = ($repoProbe.Output | Select-Object -First 1).Trim()
if (-not $Repo) {
    throw "Could not resolve the repository root."
}

$currentBranchProbe = Invoke-Git -Arguments @("-C", $Repo, "branch", "--show-current")
$currentBranch = ($currentBranchProbe.Output | Select-Object -First 1).Trim()
if ($currentBranch -ne "main") {
    throw "Run this helper from the canonical runtime checkout while it is on main. Current branch: '$currentBranch'"
}

if ($Branch -eq "main") {
    throw "The canonical runtime checkout keeps main. Create or attach a non-main worktree branch instead."
}

if (-not $WorktreeRoot) {
    $parent = Split-Path -Parent $Repo
    $repoName = Split-Path -Leaf $Repo
    $WorktreeRoot = Join-Path $parent "$repoName-worktrees"
}

if (-not $Name) {
    $Name = $Branch -replace "[^A-Za-z0-9._-]", "-"
}

$WorktreePath = Join-Path $WorktreeRoot $Name
if (Test-Path -LiteralPath $WorktreePath) {
    throw "Worktree path already exists: $WorktreePath"
}

Invoke-Git -Arguments @("-C", $Repo, "fetch", "origin", "--prune") | Out-Null

$worktreeList = Invoke-Git -Arguments @("-C", $Repo, "worktree", "list", "--porcelain")
$branchRef = "branch refs/heads/$Branch"
if ($worktreeList.Output -contains $branchRef) {
    throw "Branch '$Branch' is already checked out in another worktree."
}

$localProbe = Invoke-Git -Arguments @(
    "-C", $Repo, "show-ref", "--verify", "--quiet", "refs/heads/$Branch"
) -AllowFailure
$localExists = $localProbe.ExitCode -eq 0

$remoteProbe = Invoke-Git -Arguments @(
    "-C", $Repo, "show-ref", "--verify", "--quiet", "refs/remotes/origin/$Branch"
) -AllowFailure
$remoteExists = $remoteProbe.ExitCode -eq 0

New-Item -ItemType Directory -Path $WorktreeRoot -Force | Out-Null

if ($localExists) {
    Invoke-Git -Arguments @(
        "-C", $Repo, "worktree", "add", $WorktreePath, $Branch
    ) | Out-Null
}
elseif ($remoteExists) {
    Invoke-Git -Arguments @(
        "-C", $Repo, "worktree", "add", "--track", "-b", $Branch,
        $WorktreePath, "origin/$Branch"
    ) | Out-Null
}
elseif ($Create) {
    Invoke-Git -Arguments @(
        "-C", $Repo, "worktree", "add", "-b", $Branch,
        $WorktreePath, $CreateFrom
    ) | Out-Null
}
else {
    throw "Branch '$Branch' was not found locally or on origin. Re-run with -Create to create it from '$CreateFrom'."
}

$head = Invoke-Git -Arguments @("-C", $WorktreePath, "rev-parse", "--short", "HEAD")
$status = Invoke-Git -Arguments @("-C", $WorktreePath, "status", "--short", "--branch")

Write-Host ""
Write-Host "Worktree ready:"
Write-Host "  Path:   $WorktreePath"
Write-Host "  Branch: $Branch"
Write-Host "  HEAD:   $($head.Output | Select-Object -First 1)"
Write-Host ""
$status.Output | ForEach-Object { Write-Host $_ }
Write-Host ""
Write-Host "Use this path for parallel development. Keep '$Repo' on main as the canonical runtime checkout."
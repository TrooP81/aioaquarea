[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$ComposeProject,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string[]]$ComposeFile,

    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$DbService,

    [Parameter(Mandatory)]
    [ValidatePattern('^(?:sha256:[a-fA-F0-9]{64}|.+@sha256:[a-fA-F0-9]{64})$')]
    [string]$BackendImage,

    [Parameter(Mandatory)]
    [ValidatePattern('^[a-zA-Z0-9_]+$')]
    [string]$ExpectedHead,

    [Parameter(Mandatory)]
    [ValidateScript({ $_ -ceq 'APPROVE READ-ONLY PRODUCTION ALEMBIC METADATA QUERY' })]
    [string]$Approval
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -ge 7) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$tempFiles = [System.Collections.Generic.List[string]]::new()
$utf8Strict = [System.Text.UTF8Encoding]::new($false, $true)

function Read-StrictCommandOutput {
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [Parameter(Mandatory)]
        [string]$Description
    )

    try {
        $value = $utf8Strict.GetString([System.IO.File]::ReadAllBytes($Path))
    }
    catch {
        throw "$Description was not valid UTF-8."
    }

    if ($value.EndsWith("`r`n", [System.StringComparison]::Ordinal)) {
        $value = $value.Substring(0, $value.Length - 2)
    }
    elseif ($value.EndsWith("`n", [System.StringComparison]::Ordinal)) {
        $value = $value.Substring(0, $value.Length - 1)
    }

    if ($value.Contains("`r") -or $value.Contains("`n")) {
        throw "$Description contained unexpected multiple output lines."
    }

    return $value
}

function Invoke-DockerCaptured {
    param(
        [Parameter(Mandatory)]
        [string[]]$Arguments,

        [Parameter(Mandatory)]
        [string]$Description
    )

    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()
    $tempFiles.Add($stdoutPath)
    $tempFiles.Add($stderrPath)

    & docker @Arguments 1> $stdoutPath 2> $stderrPath
    $exitCode = $LASTEXITCODE
    $stdout = Read-StrictCommandOutput -Path $stdoutPath -Description "$Description stdout"

    if ($exitCode -ne 0) {
        throw "$Description failed with exit code $exitCode."
    }
    if ([System.IO.File]::ReadAllBytes($stderrPath).Length -ne 0) {
        throw "$Description wrote to stderr."
    }

    return $stdout
}

function Get-ComposeArguments {
    $arguments = [System.Collections.Generic.List[string]]::new()
    $arguments.Add('compose')
    $arguments.Add('--project-name')
    $arguments.Add($ComposeProject)
    foreach ($composeFile in $ComposeFile) {
        $arguments.Add('--file')
        $arguments.Add($composeFile)
    }
    return ,$arguments
}

function Get-DbContainerIdentity {
    $composeArguments = Get-ComposeArguments
    $composeArguments.Add('ps')
    $composeArguments.Add('--quiet')
    $composeArguments.Add($DbService)
    $containerShortId = Invoke-DockerCaptured -Arguments $composeArguments.ToArray() -Description 'Compose database container lookup'
    if ($containerShortId -notmatch '^[a-f0-9]{12,64}$') {
        throw 'Compose did not resolve exactly one running database container.'
    }

    $containerId = Invoke-DockerCaptured -Arguments @('inspect', '--format', '{{.Id}}', $containerShortId) -Description 'Database container identity lookup'
    if ($containerId -notmatch '^[a-f0-9]{64}$') {
        throw 'Database container identity was invalid.'
    }

    return $containerId
}

try {
    $resolvedImageId = Invoke-DockerCaptured -Arguments @('image', 'inspect', '--format', '{{.Id}}', $BackendImage) -Description 'Candidate backend image lookup'
    if ($resolvedImageId -notmatch '^sha256:[a-f0-9]{64}$') {
        throw 'Candidate backend image did not resolve to a full sha256 image ID.'
    }
    if ($BackendImage -match '^sha256:' -and -not [string]::Equals($BackendImage, $resolvedImageId, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Candidate backend image ID did not match its resolved sha256 image ID.'
    }

    $headCheck = @'
from alembic.config import Config
from alembic.script import ScriptDirectory
import os
import sys

expected = os.environ["EXPECTED_HEAD"]
heads = ScriptDirectory.from_config(Config("/app/alembic.ini")).get_heads()
if heads != [expected]:
    sys.exit(2)
print(expected)
'@
    $candidateHead = Invoke-DockerCaptured -Arguments @('run', '--rm', '--entrypoint', 'python', '--env', "EXPECTED_HEAD=$ExpectedHead", $resolvedImageId, '-c', $headCheck) -Description 'Candidate Alembic head lookup'
    if (-not [string]::Equals($candidateHead, $ExpectedHead, [System.StringComparison]::Ordinal)) {
        throw 'Candidate Alembic head did not match the expected head.'
    }

    $beforeContainerId = Get-DbContainerIdentity
    $query = 'set -eu; : "${POSTGRES_PASSWORD:?missing POSTGRES_PASSWORD}"; : "${POSTGRES_USER:?missing POSTGRES_USER}"; : "${POSTGRES_DB:?missing POSTGRES_DB}"; export PGPASSWORD="$POSTGRES_PASSWORD"; psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT version_num FROM alembic_version;"'
    $queryArguments = Get-ComposeArguments
    $queryArguments.Add('exec')
    $queryArguments.Add('-T')
    $queryArguments.Add('--env')
    $queryArguments.Add('PGOPTIONS=-c default_transaction_read_only=on')
    $queryArguments.Add($DbService)
    $queryArguments.Add('/bin/sh')
    $queryArguments.Add('-c')
    $queryArguments.Add($query)
    $databaseHead = Invoke-DockerCaptured -Arguments $queryArguments.ToArray() -Description 'Read-only Alembic metadata query'
    $afterContainerId = Get-DbContainerIdentity

    if (-not [string]::Equals($beforeContainerId, $afterContainerId, [System.StringComparison]::Ordinal)) {
        throw 'Database container identity changed during the metadata query.'
    }
    if (-not [string]::Equals($databaseHead, $ExpectedHead, [System.StringComparison]::Ordinal)) {
        throw 'Database Alembic head did not match the expected head.'
    }

    [pscustomobject]@{
        CandidateImageId = $resolvedImageId
        CandidateAlembicHead = $candidateHead
        DatabaseContainerId = $beforeContainerId
        DatabaseAlembicHead = $databaseHead
        ReadOnly = $true
    } | ConvertTo-Json -Compress
}
finally {
    foreach ($tempFile in $tempFiles) {
        Remove-Item -LiteralPath $tempFile -Force -ErrorAction SilentlyContinue
    }
}
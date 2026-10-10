param(
    [Parameter(Mandatory = $true)][string]$EvidenceDirectory
)

$ErrorActionPreference = 'Stop'
$checkout = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
$sourceReceipt = Join-Path $env:TEMP 'hpo-obs-evidence/obs5-implementation-evidence.json'
$fingerprint = '7ce3bfa5e14a6cec9c687ae8d771a9c80f84178a1c21e7cbef05393fe7a419cf'
$head = (git -C $checkout rev-parse HEAD) -join ''
$manifest = Get-Content -LiteralPath $sourceReceipt -Raw | ConvertFrom-Json -Depth 100
foreach ($entry in $manifest.post_change_source_manifest) {
    $path = Join-Path $checkout $entry.path
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.sha256) {
        throw ('Source drift: ' + $entry.path)
    }
}
if ($head -ne $manifest.revision.head -or $manifest.revision.tested_source_manifest_sha256 -ne $fingerprint) {
    throw 'Source revision drift'
}
if (-not (Test-Path -LiteralPath $EvidenceDirectory -PathType Container)) {
    throw 'Export directory must exist'
}
if (Test-Path -LiteralPath (Join-Path $EvidenceDirectory 'coordinator.json')) {
    throw 'Export directory already used'
}

$lease = [guid]::NewGuid().ToString('N')
$networkName = 'cop-manual-mon-' + $lease
$pgName = 'obs5-pg-' + $lease
$redisName = 'obs5-redis-' + $lease
$runnerName = 'obs5-runner-' + $lease
$pgVolume = 'obs5-pg-data-' + $lease
$tempVolume = 'obs5-temp-' + $lease
$pgImage = 'timescale/timescaledb@sha256:15e00162766bd6f0019afaad4e57b850dcf882de5909bd7633899eebd4c03d57'
$redisImage = 'redis:7.4.2-alpine@sha256:02419de7eddf55aa5bcf49efb74e88fa8d931b4d77c07eff8a6b2144472b6952'
$runnerImage = 'hpo-backend-isolation-tz:8c41'
$record = [ordered]@{
    lease       = $lease
    revision    = ($head + '+' + $fingerprint)
    export      = $EvidenceDirectory
    resources   = @()
    runner_exit = $null
    cleanup     = @()
}
$createdVolumes = @()
$networkCreated = $false
$runnerExit = $null
try {
    $existing = @(docker ps -a --filter ('label=hpo.verify.owner=' + $lease) --format '{{.ID}}')
    if ($LASTEXITCODE -ne 0 -or $existing.Count) { throw 'Ownership inventory refused' }
    foreach ($volume in @($pgVolume, $tempVolume)) {
        $created = (docker volume create --label ('hpo.verify.owner=' + $lease) $volume) -join ''
        if ($LASTEXITCODE -ne 0 -or $created -ne $volume) { throw 'Owned volume creation failed' }
        $createdVolumes += $volume
    }
    $networkId = (docker network create --internal --label ('hpo.verify.owner=' + $lease) $networkName) -join ''
    if ($LASTEXITCODE -ne 0) { throw 'Owned private network creation failed' }
    $networkCreated = $true
    $pgId = (docker run -d --pull never --name $pgName --network $networkName `
            --label ('hpo.verify.owner=' + $lease) --label 'hpo.verify.role=postgres' `
            --mount ('type=volume,source=' + $pgVolume + ',target=/var/lib/postgresql/data') `
            --health-cmd 'pg_isready -h 127.0.0.1 -p 5433 -U heatpump -d heatpump_test' `
            --health-interval 1s --health-timeout 2s --health-retries 90 `
            --env 'POSTGRES_DB=heatpump_test' --env 'POSTGRES_USER=heatpump' `
            --env 'POSTGRES_PASSWORD=heatpump_test' $pgImage postgres -c port=5433) -join ''
    if ($LASTEXITCODE -ne 0) { throw 'Owned PostgreSQL creation failed' }
    $redisId = (docker run -d --pull never --name $redisName --network ('container:' + $pgId) `
            --label ('hpo.verify.owner=' + $lease) --label 'hpo.verify.role=redis' `
            --tmpfs '/data:rw,noexec,nosuid' --health-cmd 'redis-cli -p 6380 ping' `
            --health-interval 1s --health-timeout 2s --health-retries 60 `
            $redisImage redis-server --port 6380) -join ''
    if ($LASTEXITCODE -ne 0) { throw 'Owned Redis creation failed' }
    $context = [ordered]@{
        postgres = [ordered]@{ id = $pgId; network = $networkName }
        redis    = [ordered]@{ id = $redisId; network = $networkName }
    } | ConvertTo-Json -Compress -Depth 6
    $runnerImageId = (docker image inspect --format '{{.Id}}' $runnerImage) -join ''
    if ($LASTEXITCODE -ne 0) { throw 'Existing Linux runner image unavailable' }
    $record.resources = @(
        [ordered]@{ kind = 'network'; name = $networkName; id = $networkId },
        [ordered]@{ kind = 'postgres'; name = $pgName; id = $pgId; image = $pgImage },
        [ordered]@{ kind = 'redis'; name = $redisName; id = $redisId; image = $redisImage },
        [ordered]@{ kind = 'runner-image'; name = $runnerImage; id = $runnerImageId },
        [ordered]@{ kind = 'volume'; name = $pgVolume },
        [ordered]@{ kind = 'volume'; name = $tempVolume }
    )
    Write-Output ('OWNED_LEASE=' + $lease)
    Write-Output ('RUNNER_IMAGE=' + $runnerImageId)
    $arguments = @(
        'run', '--pull', 'never', '--name', $runnerName, '--network', ('container:' + $pgId),
        '--label', ('hpo.verify.owner=' + $lease), '--label', 'hpo.verify.role=runner',
        '--user', '0', '--read-only', '--tmpfs', '/tmp:rw,exec,nosuid',
        '--mount', ('type=bind,source=' + $checkout + ',target=/checkout,readonly'),
        '--mount', ('type=bind,source=' + $EvidenceDirectory + ',target=/export'),
        '--mount', ('type=bind,source=' + $sourceReceipt + ',target=/source-receipt.json,readonly'),
        '--mount', 'type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock,readonly',
        '--mount', ('type=volume,source=' + $tempVolume + ',target=/owned-tmp'),
        '--workdir', '/checkout/heatpump-optimizer',
        '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'TMPDIR=/owned-tmp',
        '--env', 'PYTHONPATH=/checkout:/checkout/heatpump-optimizer',
        '--env', 'APP_ENVIRONMENT=test', '--env', 'API_TOKEN=test-token',
        '--env', 'SECRET_KEY=deployment-verify-public-test-secret',
        '--env', 'PRICE_PROVIDER=entsoe', '--env', 'ENTSOE_API_TOKEN=test-token',
        '--env', 'AQUAREA_USERNAME=test@test.com', '--env', 'AQUAREA_PASSWORD=testpass',
        '--env', 'DATABASE_URL=postgresql+asyncpg://heatpump:heatpump_test@127.0.0.1:5433/heatpump_test',
        '--env', 'REDIS_URL=redis://127.0.0.1:6380/0',
        '--env', ('MODEL_DIR=/owned-tmp/cop-manual-mon-' + $lease + '/models'),
        '--env', ('COP_MANUAL_TEST_OWNED=cop-manual-mon-' + $lease),
        '--env', ('COP_MANUAL_TEST_LEASE_ID=' + $lease),
        '--env', ('COP_MANUAL_TEST_REVISION=' + $head + '+' + $fingerprint),
        '--env', ('COP_MANUAL_TEST_SERVICE_CONTEXT=' + $context),
        '--env', ('HPO_OBS5_TESTED_REVISION=' + $fingerprint),
        '--env', 'HPO_OBS5_SOURCE_RECEIPT=/source-receipt.json',
        '--env', 'HPO_OBS5_EXPORT=/export', '--entrypoint', 'timeout', $runnerImage,
        '--signal=TERM', '--kill-after=25s', '660s',
        'python', 'tests/e2e/test_ml_lifecycle_verification.py'
    )
    docker @arguments 2>&1 | Tee-Object -FilePath (Join-Path $EvidenceDirectory 'runner.log')
    $runnerExit = $LASTEXITCODE
    $record.runner_exit = $runnerExit
    $runnerId = (docker inspect --format '{{.Id}}' $runnerName) -join ''
    if ($LASTEXITCODE -eq 0) {
        $record.resources += [ordered]@{ kind = 'runner'; name = $runnerName; id = $runnerId }
    }
    Write-Output ('OWNED_PG_RUNNER_EXIT=' + $runnerExit)
}
catch {
    $record.coordinator_error = $_.Exception.Message
    throw
}
finally {
    $ownedIds = @(docker ps -a --filter ('label=hpo.verify.owner=' + $lease) --format '{{.ID}}')
    foreach ($ownedId in $ownedIds) {
        $labels = (docker inspect --format '{{json .Config.Labels}}' $ownedId) | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0 -or $labels.'hpo.verify.owner' -ne $lease) {
            $record.cleanup += [ordered]@{ kind = 'container'; id = $ownedId; exit = 'ownership-refused' }
            continue
        }
        docker logs $ownedId 2>&1 | Out-File -LiteralPath (Join-Path $EvidenceDirectory ($labels.'hpo.verify.role' + '-service.log')) -Encoding utf8
        docker rm -f -v $ownedId
        $record.cleanup += [ordered]@{ kind = 'container'; id = $ownedId; exit = $LASTEXITCODE }
    }
    if ($networkCreated) {
        docker network rm $networkName
        $record.cleanup += [ordered]@{ kind = 'network'; name = $networkName; exit = $LASTEXITCODE }
    }
    foreach ($volume in $createdVolumes) {
        docker volume rm $volume
        $record.cleanup += [ordered]@{ kind = 'volume'; name = $volume; exit = $LASTEXITCODE }
    }
    $record.remaining_containers = @(docker ps -a --filter ('label=hpo.verify.owner=' + $lease) --format '{{.ID}}')
    $record.remaining_networks = @(docker network ls --filter ('label=hpo.verify.owner=' + $lease) --format '{{.ID}}')
    $record.remaining_volumes = @(docker volume ls --filter ('label=hpo.verify.owner=' + $lease) --format '{{.Name}}')
    $record | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $EvidenceDirectory 'coordinator.json') -Encoding utf8NoBOM
    $remaining = $record.remaining_containers.Count + $record.remaining_networks.Count + $record.remaining_volumes.Count
    Write-Output ('CLEANUP_REMAINING=' + $remaining)
    Write-Output ('PG_EVIDENCE=' + $EvidenceDirectory)
}
if ($null -eq $runnerExit) { exit 1 }
exit $runnerExit
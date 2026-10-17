param(
    [Parameter(Mandatory=$true)]
    [string]$TranscriptPath
)

if (-not (Test-Path $TranscriptPath)) {
    Write-Error "Transcript file not found: $TranscriptPath"
    exit 1
}

$lines = Get-Content -Path $TranscriptPath -Encoding UTF8 | ForEach-Object { $_ | ConvertFrom-Json }

$turns = @()
$currentTurn = $null
$turnIndex = 0

foreach ($step in $lines) {
    if ($step.type -eq "USER_INPUT") {
        if ($null -ne $currentTurn) {
            $turns += $currentTurn
        }
        $turnIndex++
        $rawText = ($step.content -replace '(?s).*<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>.*', '$1').Trim()
        if ($rawText.Length -gt 48) { $rawText = $rawText.Substring(0, 48) + "..." }
        $currentTurn = [PSCustomObject]@{
            Turn = $turnIndex
            Prompt = $rawText
            Steps = 0
            InputTokens = 0
            CacheReadTokens = 0
            OutputTokens = 0
            LastContextSize = 0
        }
    }
    elseif ($step.type -eq "PLANNER_RESPONSE" -and $null -ne $currentTurn) {
        $inTok = if ($null -ne $step.input_tokens) { [int64]$step.input_tokens } else { 0 }
        $cacheTok = if ($null -ne $step.cache_read_tokens) { [int64]$step.cache_read_tokens } else { 0 }
        $outTok = if ($null -ne $step.output_tokens) { [int64]$step.output_tokens } else { 0 }

        $currentTurn.Steps += 1
        $currentTurn.InputTokens += $inTok
        $currentTurn.CacheReadTokens += $cacheTok
        $currentTurn.OutputTokens += $outTok
        $currentTurn.LastContextSize = $inTok + $cacheTok + $outTok
    }
    elseif ($step.type -eq "CHECKPOINT" -and $null -ne $currentTurn) {
        $summaryChars = if ($null -ne $step.content) { $step.content.Length } else { 0 }
        $currentTurn.Steps += 1
        $currentTurn.LastContextSize = 15500 + [math]::Floor($summaryChars / 4)
        $currentTurn.Prompt = "/compact (Context Reset)"
    }
}

if ($null -ne $currentTurn) {
    $turns += $currentTurn
}

$completedTurns = @($turns | Where-Object { $_.Steps -gt 0 })
$activeCtx = if ($completedTurns.Count -gt 0) { $completedTurns[-1].LastContextSize } else { 0 }

$totalIn = ($turns | Measure-Object -Property InputTokens -Sum).Sum
$totalCache = ($turns | Measure-Object -Property CacheReadTokens -Sum).Sum
$totalOut = ($turns | Measure-Object -Property OutputTokens -Sum).Sum
$totalSteps = ($turns | Measure-Object -Property Steps -Sum).Sum

[PSCustomObject]@{
    Turns = $turns
    Totals = [PSCustomObject]@{
        TotalTurns = $turns.Count
        TotalModelSteps = $totalSteps
        UncachedInputTokens = $totalIn
        CacheReadTokens = $totalCache
        OutputTokens = $totalOut
        TotalTokensProcessed = ($totalIn + $totalCache + $totalOut)
        ActiveContextTokens = $activeCtx
    }
} | ConvertTo-Json -Depth 5

<#
    One-command entry points. Usage:

        .\tasks.ps1 test        all tests, both projects
        .\tasks.ps1 stdlib      the same tests with no pytest installed
        .\tasks.ps1 calc        run the HTTP/1.1 calculator on :8080
        .\tasks.ps1 marking     run the brief's marking script against it
        .\tasks.ps1 bserve      run the BHT/1 file server on :9000
        .\tasks.ps1 demo        start bserve, fetch two files with bcurl -v, stop
        .\tasks.ps1 capture     regenerate 02-binary-http/HEXDUMP.md
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet("test", "stdlib", "calc", "marking", "bserve", "demo", "capture")]
    [string]$Task = "test"
)

$root = $PSScriptRoot
$partA = Join-Path $root "01-http11-calculator"
$partB = Join-Path $root "02-binary-http"

function Invoke-Python {
    # pytest and unittest both report on stderr, which PowerShell would
    # otherwise turn into terminating errors. Exit status is the truth.
    param([string[]]$Arguments, [string]$WorkingDirectory = $root)
    Push-Location $WorkingDirectory
    try {
        # Out-Host, not the pipeline: the only thing this returns is the code.
        & python @Arguments 2>&1 | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message }
            else { "$_" }
        } | Out-Host
        return $LASTEXITCODE
    } finally {
        Pop-Location
    }
}

switch ($Task) {
    "test" {
        exit (Invoke-Python @("-m", "pytest", "-q"))
    }
    "stdlib" {
        $failed = 0
        foreach ($project in @($partA, $partB)) {
            Write-Host "== $(Split-Path $project -Leaf)" -ForegroundColor Cyan
            $env:PYTHONPATH = $project
            try {
                $code = Invoke-Python @(
                    "-m", "unittest", "discover", "-s", "tests", "-t", "tests", "-p", "test_*.py"
                ) $project
                if ($code -ne 0) { $failed = 1 }
            } finally {
                Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
            }
        }
        exit $failed
    }
    "calc" {
        exit (Invoke-Python @("-m", "calcserver", "--port", "8080") $partA)
    }
    "marking" {
        exit (Invoke-Python @("marking_script.py", "8080") $partA)
    }
    "bserve" {
        exit (Invoke-Python @("bserve.py", "./www", "9000") $partB)
    }
    "capture" {
        exit (Invoke-Python @("tools/capture.py") $partB)
    }
    "demo" {
        $server = Start-Process python -ArgumentList "bserve.py", "./www", "9000" `
            -WorkingDirectory $partB -PassThru -NoNewWindow
        Start-Sleep -Seconds 1
        try {
            Invoke-Python @("bcurl.py", "-v", "localhost:9000/index.html") $partB | Out-Null
            Write-Host "-- two files, one connection --" -ForegroundColor Cyan
            Invoke-Python @(
                "bcurl.py", "localhost:9000/hello.txt", "localhost:9000/docs/nested.txt"
            ) $partB
        } finally {
            Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

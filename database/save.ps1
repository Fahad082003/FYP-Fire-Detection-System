# Saves the local SentinelDb into database\SentinelDb.bak, so it can be committed and restored on another PC.
# Usage (PowerShell):  .\database\save.ps1        then: git add database\SentinelDb.bak; git commit; git push
param([string]$Server = 'localhost', [string]$Database = 'SentinelDb')
$ErrorActionPreference = 'Stop'

$target = Join-Path $PSScriptRoot "$Database.bak"
# SQL Server writes the backup with its own service account, which cannot write into a user's Desktop.
# C:\Users\Public is writable by both, so the file is staged there and then moved here.
$stage = 'C:\Users\Public\SentinelDbSync'
New-Item -ItemType Directory -Force $stage | Out-Null
$staged = Join-Path $stage "$Database.bak"
if (Test-Path $staged) { Remove-Item $staged -Force }

$conn = New-Object System.Data.SqlClient.SqlConnection "Server=$Server;Database=master;Integrated Security=true;TrustServerCertificate=true"
$conn.Open()
try {
    $cmd = $conn.CreateCommand()
    $cmd.CommandTimeout = 600
    # COPY_ONLY: does not disturb any other backup plan on this server. CHECKSUM: the file is verified while written.
    $cmd.CommandText = "BACKUP DATABASE [$Database] TO DISK = @path WITH INIT, COPY_ONLY, CHECKSUM"
    $null = $cmd.Parameters.AddWithValue('@path', $staged)
    $null = $cmd.ExecuteNonQuery()
} finally { $conn.Close() }

Move-Item $staged $target -Force
$size = [math]::Round((Get-Item $target).Length / 1MB, 1)
Write-Host "Saved $Database -> $target ($size MB). Now: git add database\$Database.bak; git commit; git push"

# Replaces the local SentinelDb with database\SentinelDb.bak (made by save.ps1 on the other PC).
# Everything in the local database is overwritten. Stop the server first.
# Usage (PowerShell):  git pull; .\database\load.ps1
param([string]$Server = 'localhost', [string]$Database = 'SentinelDb', [switch]$Force)
$ErrorActionPreference = 'Stop'

$source = Join-Path $PSScriptRoot 'SentinelDb.bak'
if (-not (Test-Path $source)) { throw "Backup not found: $source. Run save.ps1 on the other PC, commit and push it, then git pull here." }
if (-not $Force) {
    $answer = Read-Host "This OVERWRITES the local database '$Database' with $source. Type yes to continue"
    if ($answer -ne 'yes') { Write-Host 'Cancelled, nothing changed.'; return }
}

# The SQL Server service account must be able to read the file, and it cannot read a user's Desktop: stage it in C:\Users\Public.
$stage = 'C:\Users\Public\SentinelDbSync'
New-Item -ItemType Directory -Force $stage | Out-Null
$staged = Join-Path $stage 'SentinelDb.bak'
Copy-Item $source $staged -Force

$conn = New-Object System.Data.SqlClient.SqlConnection "Server=$Server;Database=master;Integrated Security=true;TrustServerCertificate=true"
$conn.Open()
try {
    function Query([string]$sql) {
        $cmd = $conn.CreateCommand(); $cmd.CommandTimeout = 600; $cmd.CommandText = $sql
        $null = $cmd.Parameters.AddWithValue('@path', $staged)
        $table = New-Object System.Data.DataTable; $table.Load($cmd.ExecuteReader()); return ,$table
    }
    # Put the files in this PC's own SQL Server folders (they differ between PCs and SQL Server versions).
    $paths = Query "SELECT CAST(SERVERPROPERTY('InstanceDefaultDataPath') AS nvarchar(400)) AS DataPath, CAST(SERVERPROPERTY('InstanceDefaultLogPath') AS nvarchar(400)) AS LogPath"
    $files = Query 'RESTORE FILELISTONLY FROM DISK = @path'
    $moves = foreach ($f in $files.Rows) {
        $dir = if ($f.Type -eq 'L') { $paths.Rows[0].LogPath } else { $paths.Rows[0].DataPath }
        $ext = if ($f.Type -eq 'L') { '_log.ldf' } else { '.mdf' }
        "MOVE N'$($f.LogicalName -replace "'", "''")' TO N'$(Join-Path $dir "$Database$ext")'"
    }
    # Disconnect anyone still using the database (e.g. a running API), restore, then reopen it.
    $null = Query @"
IF DB_ID(N'$Database') IS NOT NULL ALTER DATABASE [$Database] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
RESTORE DATABASE [$Database] FROM DISK = @path WITH REPLACE, CHECKSUM, $($moves -join ', ');
ALTER DATABASE [$Database] SET MULTI_USER;
"@
} finally { $conn.Close(); Remove-Item $staged -Force -ErrorAction SilentlyContinue }

Write-Host "Restored $Database from $source. The server never changes the schema: it expects sentinel-django\schema\sentinel.sql."

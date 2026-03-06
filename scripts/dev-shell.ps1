Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$utf8NoBom = [System.Text.UTF8Encoding]::new($false)

# Keep console IO in UTF-8 to avoid mojibake for Markdown and logs.
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$global:OutputEncoding = $utf8NoBom

# On Windows PowerShell 5.x, switch active console code page to UTF-8.
if ($PSVersionTable.PSEdition -eq "Desktop") {
    chcp 65001 > $null
}

# Use UTF-8 by default for common file IO commands in this session.
$global:PSDefaultParameterValues["Get-Content:Encoding"] = "utf8"
$global:PSDefaultParameterValues["Set-Content:Encoding"] = "utf8"
$global:PSDefaultParameterValues["Add-Content:Encoding"] = "utf8"
$global:PSDefaultParameterValues["Out-File:Encoding"] = "utf8"
$global:PSDefaultParameterValues["Export-Csv:Encoding"] = "utf8"

Write-Host "UTF-8 session defaults applied."

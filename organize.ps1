# organize.ps1 — tidy the loose media into video/ img/ fonts/
# Run it from the project root:  F:\2DecideMyMovie
#     cd F:\2DecideMyMovie
#     .\organize.ps1
# Safe to re-run: files already moved are simply skipped.
# Pair it with the updated index.html (which now points at these new folders).

$root = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
Set-Location $root
Write-Host "Organizing media in: $root`n"

New-Item -ItemType Directory -Force -Path "video","img","fonts" | Out-Null

$moves = [ordered]@{
  "video" = @("movie-bg.mp4","vibe.mp4","describe.mp4","roll.mp4","search-bg.mp4")
  "img"   = @("auth-panel.webp","describe-art.webp","mood-faces.webp","movie-poster.jpg","auth-posters.jpg")
  "fonts" = @("anola.woff2")
}

foreach ($dest in $moves.Keys) {
  foreach ($file in $moves[$dest]) {
    if (Test-Path -LiteralPath $file -PathType Leaf) {
      Move-Item -LiteralPath $file -Destination (Join-Path $dest $file) -Force
      Write-Host "  moved   $file  ->  $dest\"
    } else {
      Write-Host "  skip    $file  (not at root — already moved or lives elsewhere)" -ForegroundColor DarkYellow
    }
  }
}

Write-Host "`nDone. Replace index.html with the updated one, then hard-refresh (Ctrl+F5)." -ForegroundColor Green
Write-Host "Note: search-bg.mp4 and auth-posters.jpg aren't used anywhere — moved for tidiness; you can delete them if you like." -ForegroundColor DarkGray

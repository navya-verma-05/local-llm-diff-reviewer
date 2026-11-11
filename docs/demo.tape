Output docs/demo.gif

Set Shell "powershell"
Set FontSize 14
Set Width 1050
Set Height 860
Set Theme "Catppuccin Mocha"
Set Padding 20
Set TypingSpeed 35ms

Hide
Type@2ms '$env:Path = "$PWD\.venv\Scripts;" + $env:Path'
Enter
Type@2ms '$orig = $PWD; $demo = Join-Path $env:TEMP ("diffwise-demo-" + [guid]::NewGuid().ToString("N").Substring(0,6))'
Enter
Type@2ms 'New-Item -ItemType Directory -Force $demo | Out-Null; Set-Location $demo'
Enter
Type@2ms "git init -q; git config user.name Dev; git config user.email dev@example.com; git config core.autocrlf false"
Enter
Type@2ms "Set-Content db.py 'def get_user(conn, user_id):'; Add-Content db.py '    return None'"
Enter
Type@2ms "git add db.py; git commit -q -m init"
Enter
Type@2ms "Set-Content db.py 'def get_user(conn, user_id):'"
Enter
Type@2ms 'Add-Content db.py "    query = f""SELECT id, email FROM users WHERE id = {user_id}"""'
Enter
Type@2ms "Add-Content db.py '    return conn.execute(query).fetchone()'"
Enter
Type@2ms "git add db.py"
Enter
Type@2ms "function prompt { 'PS demo> ' }"
Enter
Type@2ms "Clear-Host"
Enter
Sleep 500ms
Show

Type "git diff --staged"
Enter
Sleep 1.5s

Type "diffwise --staged --fail-on bug"
Enter
Wait+Screen@60s /Commit blocked/
Sleep 1.5s

Type 'echo "Exit code: $LASTEXITCODE"'
Enter
Sleep 4s

Hide
Type@2ms 'Set-Location $orig; Remove-Item -Recurse -Force $demo'
Enter

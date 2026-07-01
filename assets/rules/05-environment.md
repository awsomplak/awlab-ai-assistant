<!-- → authority: 00-meta.md -->
# Environment Detection Rule

**Note:** Cline automatically provides the user’s operating system, default shell, and current working directory in the `environment_details` section of every prompt. This rule does **not** re‑detect those values.

## Purpose

Ensure that any shell command you construct is compatible with the user’s environment as described in `environment_details`. Use the provided `Default Shell` and `Operating System` to choose correct syntax (e.g., PowerShell vs. Bash, Windows paths vs. Unix paths).

## Command Generation Rules

- **Read the shell from `environment_details`** – not from `./.ai/memory-bank/environment.md`.
- **Never ask the user to confirm their shell** – it is already known.
- **Do not create or update `./.ai/memory-bank/environment.md`**
- **Fallback only when no environment info is available** (e.g., very old Cline version) – then use the heuristic detection previously defined, but print a warning.

## Shell‑Specific Syntax

Use the translation table below to map common operations between PowerShell and Bash. When `Default Shell` contains `pwsh`, `powershell`, or `cmd`, use PowerShell syntax; otherwise (bash, zsh, sh) use Bash syntax.

| Operation | PowerShell | Bash / Zsh |
|-----------|-----------|------------|
| List directory | `Get-ChildItem` or `dir` | `ls` |
| Change directory | `cd` | `cd` |
| Print working directory | `Get-Location` or `pwd` | `pwd` |
| Copy item | `Copy-Item src dest` | `cp src dest` |
| Move item | `Move-Item src dest` | `mv src dest` |
| Remove file | `Remove-Item file` | `rm file` |
| Remove directory (recursive) | `Remove-Item -Recurse -Force dir` | `rm -rf dir` |
| Create directory | `New-Item -ItemType Directory -Force -Path path` | `mkdir -p path` |
| Create empty file | `New-Item -ItemType File -Force -Path file` | `touch file` |
| Read file | `Get-Content file` | `cat file` |
| Write to file | `Set-Content -Path file -Value "text"` | `echo "text" > file` |
| Append to file | `Add-Content -Path file -Value "text"` | `echo "text" >> file` |
| Search in files (regex) | `Select-String -Path "*.js" -Pattern "regex"` | `grep "regex" *.js` |
| Environment variable (read) | `$env:VARNAME` | `$VARNAME` |
| User home directory | `$env:USERPROFILE` | `$HOME` or `~` |
| Check if file exists | `Test-Path path` | `test -f path` or `[ -f path ]` |
| Check exit code | `$LASTEXITCODE` | `$?` or `${PIPESTATUS[@]}` |

## Anti‑Patterns by Shell

**On PowerShell, NEVER use:**
- `ls`, `cat`, `touch`, `grep` (use native cmdlets or fallback to `Get-ChildItem`, `Get-Content`, `New-Item`, `Select-String`)
- `mkdir -p` (PowerShell’s `mkdir` already creates parents)
- `rm -rf` (use `Remove-Item -Recurse -Force`)
- `&&` or `||` under legacy `powershell` 5.1 (use `; if ($?)` instead)
- `Format-Table`, `Format-List` (use `Select-Object`)

**On Bash/Zsh, NEVER use:**
- PowerShell cmdlets (`Get-ChildItem`, `Copy-Item`, etc.)

## Output Capture Workaround (PowerShell)

Cline’s terminal may fail to capture output from complex PowerShell pipelines. Use these workarounds:

1. **Never use `Format-Table` or `Format-List`** – use `Select-Object`.
2. **Pipe long chains to `Out-String`** to force text output.
3. **If output is still blank**, fall back to file‑based capture:
   ```powershell
   your-command | Out-File -FilePath ".ai/.tmp-cmd-output.txt" -Encoding utf8
   Get-Content ".ai/.tmp-cmd-output.txt"
   Remove-Item ".ai/.tmp-cmd-output.txt"
   ```
4. **Keep pipelines short** – break complex queries into multiple simpler commands.
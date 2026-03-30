# Fishtank Logger - Windows 11 Deployment Guide

## Prerequisites

- **Python 3.10 or newer** (required for Ctrl+C to work reliably)
- **A fishtank.live account** with an active session

---

## Step 1: Install Python

If you don't have Python installed, open PowerShell and run:

```powershell
winget install Python.Python.3.13
```

Or download from https://www.python.org/downloads/ (if using the installer, check "Add Python to PATH" during setup).

**After installation, close and reopen your terminal** (this is required for PATH changes to take effect).

Then verify with whichever command works:

```powershell
python --version
```

or:

```powershell
py --version
```

You should see `Python 3.10` or higher. Use whichever command worked (`python` or `py`) for all subsequent steps. The examples below use `python`, but substitute `py` if that's what works on your machine.

---

## Step 2: Install fishclient

```powershell
python -m pip install fishclient requests
```

If you get a permissions error, add `--user`:

```powershell
python -m pip install fishclient requests --user
```

---

## Step 3: Get your auth cookie

**IMPORTANT: Copy from the Network tab, NOT the Application/Cookies tab.**
The Application tab shows Supabase session JWTs which look similar but will not work.

1. Open Chrome/Edge/Firefox and log into **fishtank.live**
2. Press **F12** to open DevTools
3. Go to the **Network** tab
4. In the filter bar at the top, type `api.fishtank.live`
5. Refresh the page or wait for any request to appear
6. Click on one of the requests to `api.fishtank.live`
7. In the right panel, look at **Request Headers**
8. Find the `Cookie:` header. It will look like:
   ```
   Cookie: sb-wcsaaupukpdmqdjcgaoo-auth-token=some_short_value_here
   ```
9. Copy only the part AFTER the `=` sign. This is your cookie value. It should be roughly 30-40 characters long.

> **Common mistakes:**
> - Do NOT copy from Application > Cookies (those values are long JWTs and won't work)
> - Do NOT include the cookie name or the `=` sign
> - The value is short (~33 chars), not hundreds of characters

---

## Step 4: Place the script

1. Create a folder for the logger, e.g.:
   ```powershell
   mkdir C:\fishtank-logger
   ```

2. Move (or save) `fishtank_logger.py` into that folder.

---

## Step 5: Set your cookie and run

### Option A: PowerShell (recommended)

```powershell
cd C:\fishtank-logger
$env:FISHTANK_COOKIE = 'PASTE_YOUR_COOKIE_VALUE_HERE'
python fishtank_logger.py
```

### Option B: Command Prompt (cmd.exe)

```cmd
cd C:\fishtank-logger
set FISHTANK_COOKIE=PASTE_YOUR_COOKIE_VALUE_HERE
python fishtank_logger.py
```

Note: In cmd.exe, do NOT wrap the value in quotes.

### Option C: Edit the script directly

Open `fishtank_logger.py` in a text editor (Notepad, VS Code, etc.) and replace line 45:

```python
COOKIE = os.environ.get("FISHTANK_COOKIE", "YOUR_COOKIE_HERE")
```

with:

```python
COOKIE = "PASTE_YOUR_COOKIE_VALUE_HERE"
```

Then run:

```powershell
cd C:\fishtank-logger
python fishtank_logger.py
```

---

## What you should see

```
[2026-03-29 01:00:00 UTC] Connecting to fishtank.live...
Cookie:        ...last20charsofcookie
Socket events: tts:queued, tts:update, sfx:queued, sfx:update, chat:message, happening
Fishtoy poll:  /v1/items/recent every 2s
Item catalog:  47 items loaded
Log file:      fishtank_log.jsonl
Verbose:       False
------------------------------------------------------------
[2026-03-29 01:00:01 UTC] Socket.IO connected.
[2026-03-29 01:00:01 UTC] Fishtoy poller: 10 items in initial snapshot. Watching for new ones.
[2026-03-29 01:00:01 UTC] Fishtoy poller started.
[2026-03-29 01:00:01 UTC] Listening... (Ctrl+C to stop)

[2026-03-29 01:02:15 UTC] === Love Letter ===
  User:        bugman69
  Cost:        1000
  Target:      TWIN
  Status:      used
  METADATA:    this is the hidden love letter content
```

Events are also saved to `fishtank_log.jsonl` in the same folder.

---

## First run recommendation

For your first run, set `VERBOSE = True` on line 60 of the script:

```python
VERBOSE = True
```

This dumps the full raw JSON for every event, so you can see exactly what fields the server is sending. Turn it back to `False` once you've confirmed everything looks right.

---

## Stopping

Press **Ctrl+C** in the terminal. You should see:

```
[2026-03-29 02:00:00 UTC] Shutting down...
Done.
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `'python' is not recognized` | Python isn't on PATH. Close and reopen your terminal. If still broken, reinstall Python and check "Add to PATH", or try `py` instead of `python` |
| `ModuleNotFoundError: No module named 'fishclient'` | Run `python -m pip install fishclient` again |
| `Connection FAILED: Failed to retrieve auth token` | Cookie expired or wrong. Re-copy from Network tab (Step 3) |
| `Connection FAILED: No session found` | You probably copied from the Application/Cookies tab. You must copy from the **Network tab > Request Headers > Cookie header**. The correct value is ~33 chars, not a long JWT |
| `WARNING: Cookie value looks too short` | You probably copied the cookie name instead of the value |
| Script prints nothing after "Waiting for events..." | Normal if no one is redeeming fishtoys. Try with VERBOSE=True and wait a few minutes. If still nothing, the cookie may be invalid |
| `ConnectionRefusedError` or timeout | fishtank.live servers may be down, or no season is currently live |
| Ctrl+C doesn't stop the script | Press Ctrl+C multiple times, or close the terminal window |

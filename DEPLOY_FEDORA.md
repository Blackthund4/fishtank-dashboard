# Fishtank Logger - Fedora Deployment Guide

## Prerequisites

- **Python 3.10 or newer** (Fedora ships with Python 3.12+)
- **A fishtank.live account** with an active session

---

## Step 1: Verify Python

Python 3 comes preinstalled on Fedora. Verify:

```bash
python3 --version
```

You should see `Python 3.12` or similar.

---

## Step 2: Install fishclient

Modern Fedora uses PEP 668 (externally managed environments), so you need either a virtual environment or the `--break-system-packages` flag.

### Option A: Virtual environment (recommended)

```bash
python3 -m venv ~/fishtank-env
source ~/fishtank-env/bin/activate
pip install fishclient requests
```

If using a venv, you need to run `source ~/fishtank-env/bin/activate` each time before running the script.

### Option B: System-wide (quick and dirty)

```bash
pip install fishclient requests --break-system-packages
```

This works fine for a standalone tool like this. If you get a permissions error, add `--user`:

```bash
pip install fishclient requests --break-system-packages --user
```

---

## Step 3: Get your auth cookie

**IMPORTANT: Copy from the Network tab, NOT the Application/Storage/Cookies tab.**
The Application/Storage tab shows Supabase session JWTs which look similar but will not work.

1. Open Firefox/Chrome and log into **fishtank.live**
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
> - Do NOT copy from Application/Storage > Cookies (those values are long JWTs and won't work)
> - Do NOT include the cookie name or the `=` sign
> - The value is short (~33 chars), not hundreds of characters

---

## Step 4: Place the script

```bash
mkdir -p ~/fishtank-logger
cp fishtank_logger.py ~/fishtank-logger/
cd ~/fishtank-logger
```

---

## Step 5: Set your cookie and run

### Option A: Environment variable (recommended)

```bash
cd ~/fishtank-logger

# If you used a venv in Step 2, activate it first:
# source ~/fishtank-env/bin/activate

export FISHTANK_COOKIE='PASTE_YOUR_COOKIE_VALUE_HERE'
python3 fishtank_logger.py
```

### Option B: Edit the script directly

Open `fishtank_logger.py` and replace line 45:

```python
COOKIE = os.environ.get("FISHTANK_COOKIE", "YOUR_COOKIE_HERE")
```

with:

```python
COOKIE = "PASTE_YOUR_COOKIE_VALUE_HERE"
```

Then run:

```bash
cd ~/fishtank-logger
python3 fishtank_logger.py
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

Events are also saved to `fishtank_log.jsonl` in the same directory.

---

## First run recommendation

For your first run, set `VERBOSE = True` on line 70 of the script:

```python
VERBOSE = True
```

This dumps full raw JSON for every event so you can confirm field names. Set it back to `False` once verified.

---

## Running in the background

To keep it running after you close the terminal:

```bash
cd ~/fishtank-logger

# If using venv:
# source ~/fishtank-env/bin/activate

export FISHTANK_COOKIE='YOUR_COOKIE_HERE'
nohup python3 fishtank_logger.py > fishtank_stdout.log 2>&1 &
echo $! > fishtank.pid
```

To stop it later:

```bash
kill $(cat fishtank.pid)
```

Or use `tmux` / `screen` for an interactive session you can detach from.

---

## Stopping

Press **Ctrl+C**. You should see:

```
[2026-03-29 02:00:00 UTC] Shutting down...
Done.
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `pip: command not found` | Use `python3 -m pip install fishclient requests --break-system-packages` |
| `ModuleNotFoundError: No module named 'fishclient'` | If using a venv, make sure you activated it first |
| `Connection FAILED: Failed to retrieve auth token` | Cookie expired. Re-copy from Network tab (Step 3) |
| `Connection FAILED: No session found` | You probably copied from Application/Storage > Cookies. You must copy from the **Network tab > Request Headers > Cookie header**. The correct value is ~33 chars, not a long JWT |
| `WARNING: Cookie value looks too short` | You probably copied the cookie name instead of the value |
| Nothing happens after "Waiting for events..." | Normal if nobody is using fishtoys. Try with VERBOSE=True and wait. If still nothing after 5+ minutes, cookie may be invalid |
| `ConnectionRefusedError` or timeout | fishtank.live may be down or no season is live |

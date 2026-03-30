# GitHub Guide

Two tasks: submit a bug fix PR to fishclient, and upload the dashboard as your own repo.

Prerequisites: You have a GitHub account and Git installed. If Git isn't installed:

```powershell
winget install Git.Git
```

Close and reopen PowerShell, then configure:

```powershell
git config --global user.name "Your Name"
git config --global user.email "your_github_email@example.com"
```

---

## PART A: Bug Fix PR to fishclient

### Step 1: Fork the repo

1. Go to https://github.com/pluhian/fishclient
2. Click **Fork** (top right corner)
3. On the fork creation page, leave all defaults and click **Create fork**
4. You now have your own copy at `https://github.com/YOUR_USERNAME/fishclient`

### Step 2: Clone your fork

```powershell
cd C:\
git clone https://github.com/YOUR_USERNAME/fishclient.git
cd fishclient
```

Replace `YOUR_USERNAME` with your actual GitHub username.

### Step 3: Create a branch

```powershell
git checkout -b fix-message-handling-bugs
```

### Step 4: Replace client.py

I've provided the patched file (`fishclient_patched_client.py`). Copy it over the original:

```powershell
copy C:\path\to\fishclient_patched_client.py C:\fishclient\fishclient\client.py
```

Adjust the source path to wherever you saved the downloaded file.

### Step 5: Verify the changes

Run this to see exactly what changed:

```powershell
git diff
```

You should see 5 changes. Here's what each one does:

**Change 1 (lines 32-33): Disconnect handler signature fix**

```
- self.dispatcher.on("disconnect")(self.disconnect)
+ self.dispatcher.on("disconnect")(lambda data=None: self.disconnect())
```

Socket.IO passes a data argument to event handlers. `self.disconnect` doesn't accept one, causing a silent TypeError every time the server disconnects. The lambda accepts and discards the argument.

**Change 2 (lines 85-95): Shutdown deadlock fix**

The original calls `thread.join()` before `websocket.close()`. The thread is blocked on `recv()`, so `join()` waits forever. Fix: close the socket first (unblocks recv), then join with a 5-second timeout.

**Change 3 (lines 121-136): Listen loop fixes**

Three sub-fixes: (a) `raise Exception` changed to `return` since raising in a thread crashes silently, (b) added `if not self.is_connected: break` so clean shutdown doesn't trigger reconnect, (c) wrapped reconnect in try/except so failure doesn't crash the thread.

**Change 4 (lines 159-167): Binary message handling fix**

The original checks `first_byte == b'\x83'` which is the msgpack header for exactly 3 keys. Packets with 4+ keys (0x84, 0x85) are silently dropped. Fix: process all binary messages with `isinstance(message, bytes)`, and wrap in try/except for malformed frames.

### Step 6: Commit

```powershell
git add fishclient/client.py
git commit -m "Fix 5 bugs in message handling, shutdown, and event dispatch

- Process all binary msgpack packets, not just 0x83 fixmap (3 keys)
- Wrap handle_packed in try/except to prevent listener crashes
- Fix shutdown deadlock: close websocket before joining thread
- Check is_connected after recv errors to prevent spurious reconnection
- Fix disconnect/connect_error handler signatures for Socket.IO"
```

### Step 7: Push

```powershell
git push origin fix-message-handling-bugs
```

Git will ask you to authenticate. A browser window should open. If it asks for a password instead, you need a Personal Access Token:

1. GitHub > click your avatar (top right) > **Settings**
2. Scroll down to **Developer settings** (bottom of left sidebar)
3. **Personal access tokens** > **Tokens (classic)** > **Generate new token**
4. Name: `git push`, check the **repo** scope, click **Generate token**
5. Copy the token immediately (you won't see it again)
6. When Git asks for password, paste the token

### Step 8: Create the Pull Request

1. Go to your fork: `https://github.com/YOUR_USERNAME/fishclient`
2. Click the **Compare & pull request** button (yellow banner at the top)
3. Title: `Fix 5 bugs in message handling, shutdown, and event dispatch`
4. Description -- copy-paste this:

---

## Summary

Fixes 5 bugs in `client.py` that cause dropped events, listener crashes, shutdown deadlocks, and silent disconnect failures. All discovered while building a monitoring dashboard against the Season 5 API.

## Bugs Fixed

### 1. Only 0x83 msgpack packets are processed (line 166)
`handle_message` checks for `b'\x83'` (fixmap with exactly 3 keys). The server sends packets with 4+ keys using 0x84, 0x85, etc. These are silently dropped.

**Fix:** Process all binary frames using `isinstance(message, bytes)`.

### 2. Malformed binary frames crash the listener (line 167)
If `handle_packed` throws on an unexpected frame, the exception propagates up and triggers reconnection.

**Fix:** Wrap `handle_packed` in try/except.

### 3. Shutdown deadlock (lines 88-92)
`disconnect()` calls `socket_thread.join()` before `websocket.close()`. The listener thread is blocked on `websocket.recv()`, so `join()` waits forever.

**Fix:** Close websocket first (with try/except), then join with timeout.

### 4. Spurious reconnection on clean shutdown (lines 132-136)
`listen()` catches recv exceptions but doesn't check `is_connected`, so it reconnects on intentional shutdown. Also raises Exception when websocket is None, which crashes silently in a thread.

**Fix:** Check `is_connected` before reconnect. Change `raise` to `return`. Wrap reconnect in try/except.

### 5. Disconnect handler signature mismatch (lines 32-33)
`self.disconnect` is registered as handler for "disconnect" and "connect_error", but Socket.IO passes data to handlers. `disconnect()` only takes `self`, causing TypeError. EventDispatcher catches this silently, so `disconnect()` never runs.

**Fix:** Wrap in `lambda data=None: self.disconnect()`.

## Testing

Tested against fishtank.live Season 5 (March 2026). Chat, TTS, and SFX events arrive reliably. Clean shutdown works. No crashes on malformed frames.

---

5. Click **Create pull request**

Done. The maintainer (pluhian) gets notified.

---

## PART B: Upload the Dashboard as Your Own Repo

### Step 1: Create the repo on GitHub

1. Go to https://github.com/new
2. Repository name: `fishtank-dashboard`
3. Description: `Real-time event monitoring dashboard for fishtank.live`
4. Set to **Public**
5. Do NOT check any initialization boxes (no README, no .gitignore, no license)
6. Click **Create repository**

### Step 2: Prepare the local project

```powershell
cd C:\fishtank-dashboard
```

Create a `.gitignore` so database files, node_modules, etc. aren't uploaded:

```powershell
@"
__pycache__/
*.pyc
*.db
*.db-wal
*.db-shm
venv/
node_modules/
dist/
package-lock.json
*.jsonl
.DS_Store
Thumbs.db
.vscode/
.idea/
"@ | Out-File -Encoding utf8 .gitignore
```

### Step 3: Initialize and push

```powershell
git init
git add .
git status
```

Check `git status` output. You should NOT see `node_modules`, `dist`, `.db` files, or `.jsonl` files. If you do, the .gitignore didn't work. Let me know.

If it looks clean, commit and push:

```powershell
git commit -m "Initial commit: fishtank.live event monitoring dashboard"
git remote add origin https://github.com/YOUR_USERNAME/fishtank-dashboard.git
git branch -M main
git push -u origin main
```

### Step 4: Verify

Go to `https://github.com/YOUR_USERNAME/fishtank-dashboard`. You should see your files and the README rendered on the front page.

---

## Pushing Future Changes

Any time you update the dashboard:

```powershell
cd C:\fishtank-dashboard
git add .
git commit -m "Brief description of what changed"
git push
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `git push` asks for username/password | Use GitHub username + Personal Access Token (not password). See Step 7 in Part A |
| `error: failed to push some refs` | Run `git pull --rebase origin main` then `git push` |
| `fatal: not a git repository` | You're in the wrong directory. `cd` to the project folder |
| Unwanted files on GitHub | Add to `.gitignore`, run `git rm --cached filename`, commit, push |
| `Everything up-to-date` | You haven't committed. Run `git add .` then `git commit` first |

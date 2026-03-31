# Fishtank Dashboard - Windows Setup Guide

## Prerequisites

- **Python 3.10+** (you already have this from the logger setup)
- **Node.js 18+** (needed for the React frontend)
- **A fishtank.live account** with an active session

---

## Step 1: Install Node.js (if you don't have it)

```powershell
winget install OpenJS.NodeJS.LTS
```

Close and reopen your terminal, then verify:

```powershell
node --version
npm --version
```

---

## Step 2: Extract the project

Extract `fishtank-dashboard.tar.gz` to a folder, e.g. `C:\fishtank-dashboard`.

If you have Git Bash or 7-Zip, use those. Otherwise in PowerShell:

```powershell
tar -xzf fishtank-dashboard.tar.gz -C C:\
```

This creates `C:\fishtank-dashboard\` with `backend\`, `frontend\`, and `README.md`.

---

## Step 3: Install dependencies

### Backend (Python)

```powershell
cd C:\fishtank-dashboard\backend
python -m pip install -r requirements.txt
```

### Frontend (Node.js)

```powershell
cd C:\fishtank-dashboard\frontend
npm install
```

---

## Step 4: Build the frontend

```powershell
cd C:\fishtank-dashboard\frontend
npm run build
```

This creates a `dist\` folder with the compiled dashboard. The backend serves these files automatically.

---

## Step 5: Set up authentication

### Option A: Automatic auth (recommended)

```powershell
cd C:\fishtank-dashboard\backend
copy .env.example .env
notepad .env
```

Edit `.env` with your fishtank.live email and password:
```
FISHTANK_EMAIL=your_email@example.com
FISHTANK_PASSWORD=your_password
```

Save and close. The dashboard logs in automatically on startup and re-authenticates if the token expires. No manual cookie copying needed.

### Option B: Manual cookie (legacy)

If you prefer not to store credentials, get the cookie from DevTools:

1. Log into fishtank.live, press F12, go to **Network** tab
2. Filter by `api.fishtank.live`, click any request
3. Find the `Cookie:` header in Request Headers
4. Copy the value after `sb-wcsaaupukpdmqdjcgaoo-auth-token=` (~33 chars)

Then set it before running:
```powershell
$env:FISHTANK_COOKIE = 'your_cookie_value_here'
```

**Important**: Copy from the Network tab, not Application/Cookies tab.

---

## Step 6: Run

You only need **one terminal**. The backend serves both the API and the frontend:

```powershell
cd C:\fishtank-dashboard\backend
python server.py
```

You should see:

```
[AUTH] Mode: automatic (email/password from .env)
[AUTH] Logging in as you***@example.com...
[AUTH] Login successful (login #1)
[AUTH]   Access token expires:  2026-04-01 12:30:00 UTC
[AUTH]   Refresh token expires: 2026-05-01 12:15:00 UTC
Starting Fishtank Dashboard on http://localhost:8000
[OK] Connected to fishtank.live
```

---

## Step 7: Open the dashboard

Open your browser and go to:

```
http://localhost:8000
```

You should see the dashboard with four tabs (Dashboard, Analytics, Hidden Content, User Search). Director message banners and live poll bars appear at the top when active. Events arrive in real-time.

---

## Development mode (optional)

If you want to make changes to the frontend with live reload, run two terminals:

**Terminal 1 (backend):**
```powershell
cd C:\fishtank-dashboard\backend
python server.py
```

**Terminal 2 (frontend dev server):**
```powershell
cd C:\fishtank-dashboard\frontend
npm run dev
```

Then open `http://localhost:3000` (the Vite dev server proxies API/WebSocket requests to the backend).

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `npm: command not found` | Install Node.js and reopen terminal |
| `Module not found: fishclient` | Run `python -m pip install -r requirements.txt` in the backend folder |
| `[AUTH] Login failed: HTTP 401` | Wrong email or password in `.env`. Double-check credentials |
| `[AUTH] Mode: none` | No `.env` file found and no `FISHTANK_COOKIE` set. Copy `.env.example` to `.env` and fill in your credentials |
| Backend starts but no events | Auth failed or token expired. Check console for `[AUTH]` messages |
| `[!] Socket disconnected` | Normal. The reconnect loop will re-establish the connection automatically |
| Dashboard loads but panels are empty | Check the terminal for connection errors. Backend logs all events to console |
| `EACCES` or permission errors | Run PowerShell as Administrator |
| Frontend build fails | Make sure you ran `npm install` first |

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

## Step 5: Get your auth cookie

Same as the logger. DevTools > **Network** tab > filter `api.fishtank.live` > click a request > Request Headers > Cookie header > copy the value after `sb-wcsaaupukpdmqdjcgaoo-auth-token=`.

The value is ~33 characters. Do NOT use the Application/Cookies tab.

---

## Step 6: Run

You only need **one terminal**. The backend serves both the API and the frontend:

```powershell
cd C:\fishtank-dashboard\backend
$env:FISHTANK_COOKIE = 'your_cookie_value_here'
python server.py
```

You should see:

```
Starting Fishtank Dashboard on http://localhost:8000
[OK] Connected to fishtank.live
```

---

## Step 7: Open the dashboard

Open your browser and go to:

```
http://localhost:8000
```

You should see the dashboard with three panels (Fishtoys, Chat, Activity) and a stats sidebar. Events will appear in real-time.

---

## Development mode (optional)

If you want to make changes to the frontend with live reload, run two terminals:

**Terminal 1 (backend):**
```powershell
cd C:\fishtank-dashboard\backend
$env:FISHTANK_COOKIE = 'your_cookie_value_here'
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
| Backend starts but no events | Cookie expired, re-copy from Network tab |
| Dashboard loads but panels are empty | Check the terminal for connection errors. Backend logs all events to console |
| `EACCES` or permission errors | Run PowerShell as Administrator |
| Frontend build fails | Make sure you ran `npm install` first |

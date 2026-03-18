# RTX Converter – iPad / Web Version

Convert Bruker EDS `.rtx` files to EMSA/MSA, CSV, and metadata in Safari on iPad (or any modern browser).

## How to Run

### Option 1: Local server (recommended for iPad)

1. On your computer, open a terminal in this folder:
   ```bash
   cd rtx_web
   python -m http.server 8080
   ```

2. Find your computer's IP address (e.g. `ipconfig` on Windows, `ifconfig` on Mac/Linux).

3. On your iPad (same Wi‑Fi), open Safari and go to:
   ```
   http://YOUR_COMPUTER_IP:8080
   ```
   Example: `http://192.168.1.100:8080`

4. Add to Home Screen (optional): In Safari, tap Share → Add to Home Screen for an app-like experience.

### Option 2: GitHub Pages

1. Push this repo to GitHub.
2. Go to **Settings → Pages**.
3. Under **Build and deployment**, set **Source** to **GitHub Actions**.
4. Push to `main` (or `master`) — the workflow deploys `rtx_web` automatically.
5. Your site will be at `https://<username>.github.io/<repo>/`.

### Option 3: Other static hosting

Upload the `rtx_web` folder to Netlify, Vercel, or any static host. The app works over HTTPS.

### Option 4: Open locally (desktop only)

Double-click `index.html` or open it in a browser. **Note:** File access may be blocked when loading from `file://`. Use a local server for best results.

## Usage

1. Tap **"Tap to select .rtx file"** and choose an RTX file (from Files, iCloud Drive, etc.).
2. Tap **Convert**.
3. Wait for conversion (first load may take 30–60 seconds while Python loads).
4. Tap each file under **Download** to save it to your device.

## Requirements

- iPad with Safari (iOS 15+)
- Wi‑Fi connection (for Pyodide CDN on first load)
- RTX files from Bruker ESPRIT

## Troubleshooting

**Stuck on "Loading Python runtime…"**
- First load downloads ~15 MB; wait 1–2 minutes.
- If it still hangs: refresh, try a different network, or use a local server.
- Some hosts (e.g. Netlify) may throttle or block the Pyodide CDN; Cloudflare Pages or GitHub Pages often work better.

# InstaVault

A local web dashboard for browsing and downloading **your own** saved Instagram
items — posts, reels and videos — for personal backup.

Everything runs on your machine. Your credentials go directly to Instagram and
the resulting session is cached locally in `session/`; nothing is sent anywhere
else.

## Heads up before you use this

Instagram has no official API for the Saved feed, so this uses Instaloader,
which talks to Instagram's private endpoints. That's against Instagram's Terms
of Service, and heavy use can get an account rate-limited or temporarily
flagged. Keep `REQUEST_DELAY` conservative, download in small batches, and use
this only for content you saved yourself.

## Setup

PowerShell (note: `&&` is not a valid separator in Windows PowerShell 5.1 —
run these one line at a time, or join them with `;`):

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Then edit `.env` and set at least `IG_USERNAME`. Leave `IG_PASSWORD` blank and
sign in through the dashboard instead — that way the password never sits on
disk. `.env` is optional to start: without it the app boots straight to the
sign-in form.

## Run

```powershell
.\venv\Scripts\python.exe app.py
```

Open http://localhost:5000.

Calling the venv's `python.exe` directly avoids `Activate.ps1`, which
PowerShell blocks under a restricted execution policy. If you prefer an
activated shell, use `.\venv\Scripts\Activate.ps1`.

## Using it

1. **Sign in.** Password login, with a two-factor step if your account has it.
   The session is cached, so later launches skip straight past this.

   If Instagram answers with *"wants you to verify this login"*, it has flagged
   the password login rather than your credentials — this is common, and
   retrying rarely helps. Use **browser session** sign-in instead: copy the
   `sessionid` cookie from a browser where you're already signed in
   (`F12` → Application/Storage → Cookies → `instagram.com`) and paste it in.
   Instagram already trusts that session, so there is nothing to challenge.
   Treat that value like a password — it authenticates as you.
2. **Sync saved.** Walks your Saved feed and indexes it locally. Nothing is
   downloaded yet — this just builds the library so you can browse offline.
3. **Pick and download.** Click cards to select (or `Select all matching` to
   take everything behind the current filter), then hit download. Progress
   streams into the dock at the bottom and can be cancelled at any point.

Keyboard: `/` focuses search, `a` selects all matching, `d` downloads the
selection, `Esc` closes the preview, arrow keys move between items in it.

## How it works

| Path | Role |
| --- | --- |
| `app.py` | Flask routes, SSE stream, thumbnail proxy, local media serving |
| `instavault/config.py` | Settings loaded from `.env` |
| `instavault/db.py` | SQLite index of saved items and download state |
| `instavault/client.py` | Instaloader wrapper: auth, rate limiting, retries, downloads |
| `instavault/jobs.py` | Background worker with progress, cancellation, resume |
| `instavault/events.py` | Pub/sub feeding live updates to the browser |
| `templates/`, `static/` | Dashboard UI |
| `downloads/` | Where media lands, one folder per item (gitignored) |
| `cache/` | SQLite index and cached thumbnails (gitignored) |
| `session/` | Cached login session (gitignored) |

### What it does about the awkward parts

Instagram is hostile to this kind of tool, so most of the code is about
failure rather than the happy path.

- **Nothing slow runs in a web request.** Syncs and downloads go to a single
  background worker; the browser gets live progress over server-sent events and
  a cancel button that takes effect between items.
- **One job at a time.** Running parallel jobs against Instagram is the
  quickest route to a rate limit, so a second job is refused rather than queued
  behind your back.
- **Rate limits back off automatically.** A global floor sits between every
  request and widens itself when Instagram pushes back with a 429, then relaxes
  again after a clean run.
- **Two-factor and checkpoints are real states,** not generic failures — the UI
  prompts for a code or tells you to approve the login on your phone.
- **Expired sessions are detected** on a cheap verification call rather than
  halfway through a long download.
- **Failures retry with backoff** and are recorded per item, so a failed batch
  can be re-run from the Failed filter instead of starting over.
- **Already-downloaded items are skipped,** tracked in SQLite, so an
  interrupted run resumes instead of re-fetching.
- **Thumbnails are proxied and cached locally,** because Instagram's CDN URLs
  expire and block hotlinking — the grid keeps working after they rot.

## Configuration

All optional; defaults are in `.env.example`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `IG_USERNAME` | — | Pre-fills sign-in and restores a cached session at boot |
| `REQUEST_DELAY` | `2.0` | Minimum seconds between Instagram requests |
| `RATE_LIMIT_BACKOFF` | `60` | Cooldown after a rate-limit response |
| `MAX_RETRIES` | `3` | Attempts per item before it's marked failed |
| `EXTRACT_AUDIO` | `false` | Split a `.m4a` out of each video (needs ffmpeg) |
| `DOWNLOAD_DIR` | `downloads` | Where media is written |
| `FLASK_HOST` | `127.0.0.1` | Bound to localhost only by default |
| `FLASK_PORT` | `5000` | Port |

## Known gaps

- **Named collections are best-effort.** There's no supported endpoint for
  them; the app tries a private one and falls back to a single flat Saved feed
  when the shape changes. Per-item collection tagging is not wired up yet.
- **Audio extraction needs ffmpeg on PATH.** Without it the checkbox is
  inert — the sidebar reports whether it was found.
- **Downloads are sequential by design.** It's slower than it could be, and
  that's the point.

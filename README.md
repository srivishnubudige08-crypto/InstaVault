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

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Then edit `.env` and set at least `IG_USERNAME`. Leave `IG_PASSWORD` blank and
sign in through the dashboard instead — that way the password never sits on
disk.

## Run

```bash
venv\Scripts\activate
python app.py
```

Open http://localhost:5000.

## How it works

| Path | Role |
| --- | --- |
| `app.py` | Flask routes and JSON API |
| `instavault/config.py` | Settings loaded from `.env` |
| `instavault/client.py` | Instaloader wrapper: login, session cache, saved feed, downloads |
| `templates/`, `static/` | Dashboard UI |
| `downloads/` | Where media lands (gitignored) |
| `session/` | Cached login session (gitignored) |

## Status

Scaffolding is in place and the app runs. Still to build:

- Collections support — the Saved feed is flat right now; Instagram's named
  collections need a separate endpoint.
- Audio extraction for reels.
- Progress reporting during bulk downloads (currently blocking).
- Resume / skip-already-downloaded logic.

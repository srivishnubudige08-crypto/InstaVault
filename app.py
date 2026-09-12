"""InstaVault local web dashboard."""

from flask import Flask, jsonify, render_template, request

from instavault import client, config

app = Flask(__name__)

# Cached loader for the life of the process. Single-user local app, so a module
# level handle is fine here.
_loader = None


def _current_loader():
    global _loader
    if _loader is None and config.USERNAME:
        try:
            _loader = client.load_session(config.USERNAME)
        except client.LoginRequired:
            _loader = None
    return _loader


@app.route("/")
def index():
    return render_template(
        "index.html",
        username=config.USERNAME,
        logged_in=_current_loader() is not None,
    )


@app.route("/api/status")
def status():
    return jsonify(
        {
            "username": config.USERNAME,
            "logged_in": _current_loader() is not None,
            "download_dir": str(config.DOWNLOAD_DIR),
            "request_delay": config.REQUEST_DELAY,
        }
    )


@app.route("/api/login", methods=["POST"])
def login():
    global _loader
    payload = request.get_json(silent=True) or {}
    username = payload.get("username") or config.USERNAME
    password = payload.get("password") or config.PASSWORD

    if not username or not password:
        return jsonify({"error": "Username and password are required."}), 400

    try:
        _loader = client.login(username, password)
    except Exception as exc:  # instaloader raises several distinct types
        return jsonify({"error": str(exc)}), 401

    return jsonify({"logged_in": True, "username": username})


@app.route("/api/saved")
def saved():
    loader = _current_loader()
    if loader is None:
        return jsonify({"error": "Not logged in."}), 401

    limit = request.args.get("limit", type=int, default=24)
    items = client.fetch_saved(loader, limit=limit)
    return jsonify([item.__dict__ for item in items])


@app.route("/api/download", methods=["POST"])
def download():
    loader = _current_loader()
    if loader is None:
        return jsonify({"error": "Not logged in."}), 401

    payload = request.get_json(silent=True) or {}
    shortcodes = payload.get("shortcodes") or []
    if not shortcodes:
        return jsonify({"error": "No items selected."}), 400

    results = []
    for shortcode in shortcodes:
        try:
            path = client.download(loader, shortcode)
            results.append({"shortcode": shortcode, "ok": True, "path": str(path)})
        except Exception as exc:
            results.append({"shortcode": shortcode, "ok": False, "error": str(exc)})

    return jsonify(results)


if __name__ == "__main__":
    config.ensure_dirs()
    app.run(port=config.FLASK_PORT, debug=True)

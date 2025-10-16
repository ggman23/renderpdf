from flask import Flask, request, jsonify, Response, send_from_directory
from flask_cors import CORS
import os, requests

app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app)

ALLOW_DOMAINS = (
    "index-education.net",
    "pronote",  # au cas où certains sous-domaines incluent ce mot
)

@app.route("/")
def root():
    # Sert la page si présente, sinon un petit message
    index_path = os.path.join(app.static_folder or "", "index.html")
    if app.static_folder and os.path.isfile(index_path):
        return send_from_directory(app.static_folder, "index.html")
    return jsonify({"ok": True, "msg": "Backend Render actif", "version": "proxy-1.0"})

@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "version": "proxy-1.0"})

def domain_allowed(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        return any(host.endswith(dom) for dom in ALLOW_DOMAINS)
    except Exception:
        return False

@app.route("/api/proxy_image")
def proxy_image():
    url = request.args.get("url", "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "unsupported url"}), 400
    if not domain_allowed(url):
        # Si tu veux autoriser toutes les images, commente le bloc suivant
        return jsonify({"error": "domain not allowed"}), 403
    try:
        r = requests.get(url, timeout=12)  # timeout important
        r.raise_for_status()
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    ct = r.headers.get("Content-Type", "image/jpeg")
    return Response(r.content, headers={"Content-Type": ct})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False)

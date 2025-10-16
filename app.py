# app.py — Render (Flask + CORS + PyMuPDF) v2.2
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import base64, io, re
import fitz  # PyMuPDF

VERSION = "v2.2"

app = Flask(__name__, static_folder="static", static_url_path="/")
CORS(app)  # Access-Control-Allow-Origin: *

NAME_RX = re.compile(r"[A-Za-zÀ-ÿ'’\\- ]{3,}")

# ---------- Utils ----------
def xref_to_dataurl(doc, xref):
    """Extraction robuste d'une image à partir d'un xref (extract_image puis Pixmap fallback)."""
    try:
        meta = doc.extract_image(xref)
        if meta and "image" in meta:
            ext = (meta.get("ext") or "png").lower()
            if ext not in ("png", "jpg", "jpeg"):
                ext = "png"
            b64 = base64.b64encode(meta["image"]).decode("ascii")
            return f"data:image/{ext};base64,{b64}"
    except Exception:
        pass
    try:
        pix = fitz.Pixmap(doc, xref)
        if pix.n >= 4 and pix.alpha == 0:
            p

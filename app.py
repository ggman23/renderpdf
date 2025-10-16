
from flask import Flask, request, jsonify, send_from_directory
import base64, io
import fitz  # PyMuPDF
from flask_cors import CORS

app = Flask(__name__, static_folder="static", static_url_path="/")
CORS(app)  # Access-Control-Allow-Origin: *

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")

@app.get("/api/extract_images")
def ok():
    return jsonify({"ok": True})

@app.post("/api/extract_images")
def extract_images():
    if "pdf" not in request.files:
        return jsonify({"error": "missing file field 'pdf'"}), 400
    f = request.files["pdf"]
    data = f.read()
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        return jsonify({"error": f"cannot open pdf: {e}"}), 400

    pages = []
    for page_index in range(len(doc)):
        page = doc[page_index]
        images = []
        for im in page.get_images(full=True):
            xref = im[0]
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.n >= 4 and pix.alpha == 0:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                buf = io.BytesIO()
                pix.save(buf, format="png")
                b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                images.append("data:image/png;base64," + b64)
            except Exception:
                pass
        pages.append({"page": page_index + 1, "images": images})
    return jsonify({"pages": pages})

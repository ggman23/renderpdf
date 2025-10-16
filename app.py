# app.py — Flask backend pour Render (PyMuPDF image extract)
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import base64, io
import fitz  # PyMuPDF

# Sert les fichiers statiques depuis ./static (facultatif pour ta page de test)
app = Flask(__name__, static_folder="static", static_url_path="/")
CORS(app)  # Access-Control-Allow-Origin: *

@app.get("/")
def index():
    """
    Si ./static/index.html existe, on le sert.
    Sinon, on renvoie un message simple.
    """
    try:
        return send_from_directory(app.static_folder, "index.html")
    except Exception:
        return jsonify({"ok": True, "msg": "Backend Render en ligne. POST /api/extract_images avec un champ 'pdf'."})

@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})

@app.get("/api/extract_images")
def ok():
    return jsonify({"ok": True})

@app.post("/api/extract_images")
def extract_images():
    """
    Reçoit un formulaire multipart avec un champ 'pdf'.
    Extrait les images intégrées de chaque page et renvoie des Data URLs.
    """
    if "pdf" not in request.files:
        return jsonify({"error": "missing file field 'pdf'"}), 400

    data = request.files["pdf"].read()
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        return jsonify({"error": f"cannot open pdf: {e}"}), 400

    pages_out = []
    total = 0

    for page_index in range(len(doc)):
        page = doc[page_index]

        # Récupère les xrefs d'images (incluant XObjects), en dédoublonnant
        seen = set()
        xrefs = []
        for info in page.get_images(full=True):
            xref = info[0]
            if xref not in seen:
                seen.add(xref)
                xrefs.append(xref)

        images_dataurls = []
        for xref in xrefs:
            # 1) Méthode recommandée: doc.extract_image(xref)
            try:
                meta = doc.extract_image(xref)
                if meta and "image" in meta:
                    ext = (meta.get("ext") or "png").lower()
                    if ext not in ("png", "jpg", "jpeg"):
                        ext = "png"
                    b64 = base64.b64encode(meta["image"]).decode("ascii")
                    images_dataurls.append(f"data:image/{ext};base64,{b64}")
                    total += 1
                    continue
            except Exception:
                pass

            # 2) Fallback: Pixmap → PNG
            try:
                pix = fitz.Pixmap(doc, xref)
                # Convertit CMYK/Gray vers RGB si nécessaire (sans alpha)
                if pix.n >= 4 and pix.alpha == 0:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                buf = io.BytesIO()
                pix.save(buf, format="png")
                b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                images_dataurls.append("data:image/png;base64," + b64)
                total += 1
            except Exception:
                # on ignore l'image problématique mais on continue
                pass

        pages_out.append({
            "page": page_index + 1,
            "images": images_dataurls,
            "debug": {
                "image_xrefs": len(xrefs),
                "images_extracted": len(images_dataurls)
            }
        })

    return jsonify({
        "pages": pages_out,
        "debug": {
            "pages": len(doc),
            "total_images": total
        }
    })

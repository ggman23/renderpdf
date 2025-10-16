# app.py — Flask backend Render : extraction images + appariement des noms
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import base64, io, re
import fitz  # PyMuPDF

app = Flask(__name__, static_folder="static", static_url_path="/")
CORS(app)  # Access-Control-Allow-Origin: *

NAME_RX = re.compile(r"[A-Za-zÀ-ÿ'’\- ]{3,}")  # filtre simple pour lignes "nom prénom"

@app.get("/")
def index():
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

def xref_to_dataurl(doc, xref):
    """Essaye d'extraire une image base64 à partir d'un xref (extract_image puis fallback Pixmap)."""
    # Piste 1: extract_image (souvent JPEG/PNG/JPX déjà décodé)
    try:
        meta = doc.extract_image(xref)
        if meta and "image" in meta:
            ext = (meta.get("ext") or "png").lower()
            if ext not in ("png", "jpg", "jpeg"):  # normalise
                ext = "png"
            b64 = base64.b64encode(meta["image"]).decode("ascii")
            return f"data:image/{ext};base64,{b64}"
    except Exception:
        pass
    # Piste 2: Pixmap -> PNG
    try:
        pix = fitz.Pixmap(doc, xref)
        if pix.n >= 4 and pix.alpha == 0:
            pix = fitz.Pixmap(fitz.csRGB, pix)
        buf = io.BytesIO()
        pix.save(buf, format="png")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return "data:image/png;base64," + b64
    except Exception:
        return None

def overlap_ratio(a0, a1, b0, b1):
    """Chevauchement horizontal relatif entre [a0,a1] et [b0,b1]."""
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    width = max(a1 - a0, 1e-6)
    return inter / width

@app.post("/api/extract_images")
def extract_images():
    if "pdf" not in request.files:
        return jsonify({"error": "missing file field 'pdf'"}), 400

    data = request.files["pdf"].read()
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        return jsonify({"error": f"cannot open pdf: {e}"}), 400

    pages_out = []
    pairs_all = []
    total_images = 0

    for page_index in range(len(doc)):
        page = doc[page_index]

        # --- RÉCUP TEXT + IMAGES AVEC BBOX via rawdict ---
        rd = page.get_text("rawdict")  # blocks: type 0 = texte, 1 = image
        text_lines = []  # [{text, bbox(x0,y0,x1,y1)}]
        image_objs = []  # [{xref, bbox(x0,y0,x1,y1)}]

        for block in rd.get("blocks", []):
            if block.get("type") == 0:  # texte
                for line in block.get("lines", []):
                    # Concaténer spans d'une ligne
                    parts = []
                    x0, y0, x1, y1 = None, None, None, None
                    for span in line.get("spans", []):
                        s = (span.get("text") or "").strip()
                        if not s:
                            continue
                        parts.append(s)
                        bx0, by0, bx1, by1 = span.get("bbox", [0,0,0,0])
                        x0 = bx0 if x0 is None else min(x0, bx0)
                        y0 = by0 if y0 is None else min(y0, by0)
                        x1 = bx1 if x1 is None else max(x1, bx1)
                        y1 = by1 if y1 is None else max(y1, by1)
                    if parts:
                        text = " ".join(parts).strip()
                        if NAME_RX.search(text) and len(text.split()) >= 2:
                            text_lines.append({"text": text, "bbox": (x0,y0,x1,y1)})
            elif block.get("type") == 1:  # image
                bbox = tuple(block.get("bbox", [0,0,0,0]))
                xref = block.get("xref")
                if xref:
                    image_objs.append({"xref": xref, "bbox": bbox})

        # Dédupliquer les images par xref
        seen = set()
        unique_images = []
        for im in image_objs:
            xr = im["xref"]
            if xr not in seen:
                seen.add(xr)
                unique_images.append(im)

        # Extraire les DataURL pour les images identifiées
        images_data = []
        for im in unique_images:
            dataurl = xref_to_dataurl(doc, im["xref"])
            if dataurl:
                images_data.append({"dataurl": dataurl, "bbox": im["bbox"], "xref": im["xref"]})

        # --- APPARIEMENT : image -> ligne de texte la plus plausible en dessous ---
        # heuristique :
        #  - texte dont y0 est proche et > image.y1 (sous la photo) (fenêtre dy)
        #  - chevauchement horizontal suffisant (>= 0.3)
        #  - si aucun sous la photo, nearest absolu par distance verticale
        pairs_page = []
        used_text = [False] * len(text_lines)
        dy_limit = max(page.rect.height * 0.06, 12)  # fenêtre verticale (~6% de la hauteur page, min 12px)
        for im in images_data:
            ix0, iy0, ix1, iy1 = im["bbox"]
            best_j = -1
            best_score = 1e9
            best_overlap = 0.0

            # priorité aux noms "en dessous"
            for j, tl in enumerate(text_lines):
                if used_text[j]:
                    continue
                tx0, ty0, tx1, ty1 = tl["bbox"]
                if ty0 < iy1:  # commence au-dessus du bas de la photo -> on le garde pour fallback
                    continue
                dy = ty0 - iy1
                if dy > dy_limit:
                    continue
                overlap = overlap_ratio(ix0, ix1, tx0, tx1)
                if overlap < 0.3:
                    continue
                # score = dy plus petit et overlap plus grand
                score = dy - (overlap * 5.0)  # pondère en faveur d'un bon chevauchement
                if score < best_score:
                    best_score = score
                    best_overlap = overlap
                    best_j = j

            # fallback : prendre la ligne la plus proche en distance verticale (haut/bas) avec un peu de chevauchement
            if best_j == -1:
                nearest_j = -1
                nearest_d = 1e9
                nearest_overlap = 0.0
                for j, tl in enumerate(text_lines):
                    if used_text[j]:
                        continue
                    tx0, ty0, tx1, ty1 = tl["bbox"]
                    # distance verticale au centre
                    cy_img = 0.5 * (iy0 + iy1)
                    cy_txt = 0.5 * (ty0 + ty1)
                    d = abs(cy_txt - cy_img)
                    overlap = overlap_ratio(ix0, ix1, tx0, tx1)
                    if overlap < 0.2:
                        continue
                    if d < nearest_d:
                        nearest_d = d
                        nearest_overlap = overlap
                        nearest_j = j
                best_j = nearest_j

            # si on a trouvé quelque chose, on appaire
            if best_j != -1:
                used_text[best_j] = True
                name = text_lines[best_j]["text"].strip()
                pairs_page.append({
                    "name": name,
                    "photo": im["dataurl"],
                    "img_bbox": im["bbox"],
                    "txt_bbox": text_lines[best_j]["bbox"]
                })

        # fallback d'affichage basique (images et lignes)
        pages_out.append({
            "page": page_index + 1,
            "images": [img["dataurl"] for img in images_data],
            "names": [tl["text"] for tl in text_lines],
            "debug": {
                "text_lines": len(text_lines),
                "images_found": len(images_data),
                "pairs": len(pairs_page)
            }
        })
        pairs_all.extend([{"page": page_index + 1, **p} for p in pairs_page])
        total_images += len(images_data)

    return jsonify({
        "pages": pages_out,
        "pairs": pairs_all,  # directement {name, photo} apairés
        "debug": {
            "pages": len(doc),
            "total_images": total_images,
        }
    })

"""
VeeGen Web UI — Flask app for the UGC video generator.

Run with:  python app.py
"""

import os
import threading

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_from_directory,
)

from veegen import OUTPUT_DIR, VALID_TONES, VALID_LANGUAGES, VALID_PRODUCT_TYPES, create_video
from lipsync import (
    is_wav2lip_ready,
    get_available_faces,
    create_lipsync_video,
    FACES_DIR,
)

app = Flask(__name__)

# Track jobs: job_id -> {"status": ..., "video": ..., "error": ...}
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template(
        "index.html",
        tones=VALID_TONES,
        languages=VALID_LANGUAGES,
        product_types=VALID_PRODUCT_TYPES,
        lipsync_ready=is_wav2lip_ready(),
        faces=get_available_faces(),
    )


@app.route("/generate", methods=["POST"])
def generate():
    """Start video generation in a background thread."""
    data = request.get_json(silent=True) or {}
    product_name = (data.get("product_name") or "").strip()
    tone = (data.get("tone") or "funny").strip().lower()
    language = (data.get("language") or "english").strip().lower()
    product_type = (data.get("product_type") or "general").strip().lower()

    if not product_name:
        return jsonify({"error": "Product name is required."}), 400
    if tone not in VALID_TONES:
        return jsonify({"error": f"Invalid tone. Choose from: {', '.join(VALID_TONES)}"}), 400
    if language not in VALID_LANGUAGES:
        return jsonify({"error": f"Invalid language. Choose from: {', '.join(VALID_LANGUAGES)}"}), 400
    if product_type != "general" and product_type not in VALID_PRODUCT_TYPES:
        return jsonify({"error": f"Invalid product type. Choose from: general, {', '.join(VALID_PRODUCT_TYPES)}"}), 400

    import uuid
    job_id = uuid.uuid4().hex

    with _jobs_lock:
        _jobs[job_id] = {"status": "processing", "video": None, "error": None}

    thread = threading.Thread(
        target=_run_generation,
        args=(job_id, product_name, tone, language, product_type),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id: str):
    """Poll for job status."""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Unknown job."}), 404
    return jsonify(job)


@app.route("/video/<path:filename>")
def serve_video(filename: str):
    """Serve a generated video file."""
    return send_from_directory(OUTPUT_DIR, filename)


@app.route("/faces")
def list_faces():
    """Return available face images."""
    return jsonify({"faces": get_available_faces(), "ready": is_wav2lip_ready()})


@app.route("/face-image/<path:filename>")
def serve_face(filename: str):
    """Serve a face image from assets/faces/."""
    return send_from_directory(FACES_DIR, filename)


@app.route("/upload-face", methods=["POST"])
def upload_face():
    """Upload a custom face image."""
    if "face" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400
    f = request.files["face"]
    if not f.filename:
        return jsonify({"error": "Empty filename."}), 400
    # Validate extension
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        return jsonify({"error": "Unsupported format. Use .jpg, .png, or .webp"}), 400
    os.makedirs(FACES_DIR, exist_ok=True)
    import uuid as _uuid
    safe_name = f"upload_{_uuid.uuid4().hex[:6]}{ext}"
    dest = os.path.join(FACES_DIR, safe_name)
    f.save(dest)
    return jsonify({"filename": safe_name, "name": "Uploaded Photo"})


@app.route("/generate-lipsync", methods=["POST"])
def generate_lipsync():
    """Start lip-sync video generation in background."""
    if not is_wav2lip_ready():
        return jsonify({"error": "Wav2Lip not set up. Run: python setup_lipsync.py"}), 400

    data = request.get_json(silent=True) or {}
    product_name = (data.get("product_name") or "").strip()
    tone = (data.get("tone") or "funny").strip().lower()
    language = (data.get("language") or "english").strip().lower()
    product_type = (data.get("product_type") or "general").strip().lower()
    face_filename = (data.get("face") or "").strip()
    position = (data.get("position") or "bottom-right").strip()

    if not product_name:
        return jsonify({"error": "Product name is required."}), 400
    if not face_filename:
        return jsonify({"error": "Please select or upload a face image."}), 400
    face_path = os.path.join(FACES_DIR, face_filename)
    if not os.path.isfile(face_path):
        return jsonify({"error": "Face image not found."}), 400
    if tone not in VALID_TONES:
        return jsonify({"error": f"Invalid tone."}), 400
    if language not in VALID_LANGUAGES:
        return jsonify({"error": f"Invalid language."}), 400

    import uuid
    job_id = uuid.uuid4().hex

    with _jobs_lock:
        _jobs[job_id] = {"status": "processing", "video": None, "error": None}

    thread = threading.Thread(
        target=_run_lipsync_generation,
        args=(job_id, product_name, tone, language, product_type,
              face_path, position),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id})


# ── Background worker ────────────────────────────────────────────────────────

def _run_generation(job_id: str, product_name: str, tone: str, language: str, product_type: str = "general") -> None:
    try:
        out_path = create_video(product_name, tone=tone, language=language, product_type=product_type)
        filename = os.path.basename(out_path)
        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["video"] = f"/video/{filename}"
    except Exception as exc:
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(exc)


def _run_lipsync_generation(
    job_id: str, product_name: str, tone: str, language: str,
    product_type: str, face_path: str, position: str,
) -> None:
    try:
        out_path = create_lipsync_video(
            product_name, face_path,
            tone=tone, language=language, product_type=product_type,
            position=position,
        )
        filename = os.path.basename(out_path)
        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["video"] = f"/video/{filename}"
    except Exception as exc:
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(exc)


# ── Entry-point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    app.run(debug=False, host="127.0.0.1", port=5001)

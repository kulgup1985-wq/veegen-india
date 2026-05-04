"""
VeeGen Web UI — Flask app for the UGC video generator.

Run with:  python app.py
"""

import os
import json
import threading
import time
import uuid
from urllib.parse import urljoin

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
    overlay_lipsync,
    _run,
    FACES_DIR,
)

app = Flask(__name__)
RUNPOD_LIPSYNC_URL = os.environ.get("RUNPOD_LIPSYNC_URL", "").strip().rstrip("/")
RUNPOD_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runpod_config.json")

# Track jobs: job_id -> {"status": ..., "video": ..., "error": ...}
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _load_runpod_config() -> dict:
    config = {
        "api_key": os.environ.get("RUNPOD_API_KEY", "").strip(),
        "pod_id": os.environ.get("RUNPOD_POD_ID", "").strip(),
        "url": RUNPOD_LIPSYNC_URL,
        "mode": os.environ.get("RUNPOD_MODE", "").strip().lower() or "pod",
        "endpoint_id": os.environ.get("RUNPOD_ENDPOINT_ID", "").strip(),
    }
    if os.path.isfile(RUNPOD_CONFIG_PATH):
        try:
            with open(RUNPOD_CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for key in ("api_key", "pod_id", "url", "mode", "endpoint_id"):
                if saved.get(key):
                    config[key] = str(saved[key]).strip()
        except Exception:
            pass
    config["url"] = config["url"].rstrip("/")
    if config["mode"] not in ("pod", "serverless"):
        config["mode"] = "pod"
    return config


def _save_runpod_config(config: dict) -> None:
    clean = {
        "api_key": str(config.get("api_key", "")).strip(),
        "pod_id": str(config.get("pod_id", "")).strip(),
        "url": str(config.get("url", "")).strip().rstrip("/"),
        "mode": str(config.get("mode", "pod")).strip().lower() or "pod",
        "endpoint_id": str(config.get("endpoint_id", "")).strip(),
    }
    if clean["mode"] not in ("pod", "serverless"):
        clean["mode"] = "pod"
    with open(RUNPOD_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2)


def _runpod_url() -> str:
    return _load_runpod_config().get("url", "")


def _runpod_ready(url: str) -> bool:
    if not url:
        return False
    try:
        import requests
        response = requests.get(urljoin(url + "/", "faces"), timeout=10)
        return response.status_code == 200
    except Exception:
        return False


def _serverless_api_url(endpoint_id: str, operation: str) -> str:
    return f"https://api.runpod.ai/v2/{endpoint_id}/{operation.lstrip('/')}"


def _serverless_ready(config: dict) -> bool:
    endpoint_id = config.get("endpoint_id", "")
    api_key = config.get("api_key", "")
    if not endpoint_id or not api_key:
        return False
    try:
        import requests
        response = requests.get(
            _serverless_api_url(endpoint_id, "health"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        return response.status_code == 200
    except Exception:
        return False


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    runpod_config = _load_runpod_config()
    return render_template(
        "index.html",
        tones=VALID_TONES,
        languages=VALID_LANGUAGES,
        product_types=VALID_PRODUCT_TYPES,
        lipsync_ready=bool(runpod_config.get("url")) or is_wav2lip_ready(),
        faces=get_available_faces(),
        runpod_config={
            "pod_id": runpod_config.get("pod_id", ""),
            "url": runpod_config.get("url", ""),
            "mode": runpod_config.get("mode", "pod"),
            "endpoint_id": runpod_config.get("endpoint_id", ""),
            "has_api_key": bool(runpod_config.get("api_key")),
        },
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
    runpod_url = _runpod_url()
    return jsonify({
        "faces": get_available_faces(),
        "ready": bool(runpod_url) or is_wav2lip_ready(),
        "runpod": bool(runpod_url),
    })


@app.route("/runpod/config", methods=["GET", "POST"])
def runpod_config():
    """Read or update local RunPod settings."""
    if request.method == "GET":
        config = _load_runpod_config()
        return jsonify({
            "pod_id": config.get("pod_id", ""),
            "url": config.get("url", ""),
            "mode": config.get("mode", "pod"),
            "endpoint_id": config.get("endpoint_id", ""),
            "has_api_key": bool(config.get("api_key")),
        })

    data = request.get_json(silent=True) or {}
    current = _load_runpod_config()
    api_key = str(data.get("api_key", "")).strip()
    updated = {
        "api_key": api_key or current.get("api_key", ""),
        "pod_id": str(data.get("pod_id", current.get("pod_id", ""))).strip(),
        "url": str(data.get("url", current.get("url", ""))).strip().rstrip("/"),
        "mode": str(data.get("mode", current.get("mode", "pod"))).strip().lower(),
        "endpoint_id": str(data.get("endpoint_id", current.get("endpoint_id", ""))).strip(),
    }
    _save_runpod_config(updated)
    return jsonify({
        "ok": True,
        "pod_id": updated["pod_id"],
        "url": updated["url"],
        "mode": updated["mode"],
        "endpoint_id": updated["endpoint_id"],
        "has_api_key": bool(updated["api_key"]),
    })


@app.route("/runpod/status")
def runpod_status():
    config = _load_runpod_config()
    url = config.get("url", "")
    mode = config.get("mode", "pod")
    ready = _serverless_ready(config) if mode == "serverless" else _runpod_ready(url)
    return jsonify({
        "configured": bool(config.get("endpoint_id")) if mode == "serverless" else bool(url),
        "ready": ready,
        "mode": mode,
        "pod_id": config.get("pod_id", ""),
        "url": url,
        "endpoint_id": config.get("endpoint_id", ""),
        "has_api_key": bool(config.get("api_key")),
    })


@app.route("/runpod/start", methods=["POST"])
def runpod_start():
    config = _load_runpod_config()
    if config.get("mode") == "serverless":
        return jsonify({
            "ok": True,
            "message": "Serverless endpoints auto-scale; no pod start is needed.",
        })
    api_key = config.get("api_key", "")
    pod_id = config.get("pod_id", "")
    if not api_key or not pod_id:
        return jsonify({"error": "RunPod API key and Pod ID are required."}), 400
    try:
        import requests
        response = requests.post(
            f"https://rest.runpod.io/v1/pods/{pod_id}/start",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=60,
        )
        response.raise_for_status()
        return jsonify({"ok": True, "message": "Start request sent."})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/runpod/stop", methods=["POST"])
def runpod_stop():
    config = _load_runpod_config()
    if config.get("mode") == "serverless":
        return jsonify({
            "ok": True,
            "message": "Serverless endpoints auto-scale; no pod stop is needed.",
        })
    api_key = config.get("api_key", "")
    pod_id = config.get("pod_id", "")
    if not api_key or not pod_id:
        return jsonify({"error": "RunPod API key and Pod ID are required."}), 400
    try:
        import requests
        response = requests.post(
            f"https://rest.runpod.io/v1/pods/{pod_id}/stop",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=60,
        )
        response.raise_for_status()
        return jsonify({"ok": True, "message": "Stop request sent."})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


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
    runpod_config = _load_runpod_config()
    use_serverless = (
        runpod_config.get("mode") == "serverless"
        and bool(runpod_config.get("endpoint_id"))
        and bool(runpod_config.get("api_key"))
    )
    use_runpod = use_serverless or bool(_runpod_url())
    if not use_runpod and not is_wav2lip_ready():
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

    job_id = uuid.uuid4().hex

    with _jobs_lock:
        _jobs[job_id] = {"status": "processing", "video": None, "error": None}

    if use_serverless:
        target = _run_serverless_lipsync_generation
    elif use_runpod:
        target = _run_remote_lipsync_generation
    else:
        target = _run_lipsync_generation
    thread = threading.Thread(
        target=target,
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


def _remote_url(path: str) -> str:
    return urljoin(_runpod_url() + "/", path.lstrip("/"))


def _run_remote_lipsync_generation(
    job_id: str, product_name: str, tone: str, language: str,
    product_type: str, face_path: str, position: str,
) -> None:
    """Delegate lip-sync generation to the RunPod-hosted VeeGen app."""
    import requests

    try:
        if not _runpod_url():
            raise RuntimeError("RUNPOD_LIPSYNC_URL is not configured.")

        with open(face_path, "rb") as f:
            upload = requests.post(
                _remote_url("/upload-face"),
                files={"face": (os.path.basename(face_path), f)},
                timeout=120,
            )
        upload.raise_for_status()
        remote_face = upload.json()["filename"]

        start = requests.post(
            _remote_url("/generate-lipsync"),
            json={
                "product_name": product_name,
                "tone": tone,
                "language": language,
                "product_type": product_type,
                "face": remote_face,
                "position": position,
            },
            timeout=120,
        )
        start.raise_for_status()
        remote_job_id = start.json()["job_id"]

        deadline = time.time() + 60 * 45
        remote_video = None
        while time.time() < deadline:
            status = requests.get(
                _remote_url(f"/status/{remote_job_id}"),
                timeout=60,
            )
            status.raise_for_status()
            data = status.json()
            if data.get("status") == "done":
                remote_video = data.get("video")
                break
            if data.get("status") == "error":
                raise RuntimeError(data.get("error") or "RunPod lip-sync failed.")
            time.sleep(5)

        if not remote_video:
            raise TimeoutError("RunPod lip-sync timed out after 45 minutes.")

        download = requests.get(_remote_url(remote_video), stream=True, timeout=300)
        download.raise_for_status()

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        safe_name = product_name.replace(" ", "_")
        local_name = f"{safe_name}_runpod_lipsync_{uuid.uuid4().hex[:8]}.mp4"
        local_path = os.path.join(OUTPUT_DIR, local_name)
        with open(local_path, "wb") as out:
            for chunk in download.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    out.write(chunk)

        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["video"] = f"/video/{local_name}"
    except Exception as exc:
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = f"RunPod: {exc}"


def _run_serverless_lipsync_generation(
    job_id: str, product_name: str, tone: str, language: str,
    product_type: str, face_path: str, position: str,
) -> None:
    """Generate the base video locally and use RunPod only for Wav2Lip."""
    import base64
    import requests
    import shutil

    work_dir = None
    try:
        config = _load_runpod_config()
        endpoint_id = config.get("endpoint_id", "")
        api_key = config.get("api_key", "")
        if not endpoint_id or not api_key:
            raise RuntimeError("RunPod Serverless Endpoint ID and API key are required.")

        base_video = create_video(
            product_name,
            tone=tone,
            language=language,
            product_type=product_type,
        )

        work_dir = os.path.join(OUTPUT_DIR, f"_serverless_lipsync_{uuid.uuid4().hex[:8]}")
        os.makedirs(work_dir, exist_ok=True)
        voice_audio = os.path.join(work_dir, "voice.wav")
        lipsync_raw = os.path.join(work_dir, "lipsync_raw.mp4")

        _run([
            "ffmpeg", "-y", "-i", base_video,
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            voice_audio,
        ], "extract local audio")

        with open(face_path, "rb") as f:
            face_b64 = base64.b64encode(f.read()).decode("ascii")
        with open(voice_audio, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode("ascii")

        headers = {"Authorization": f"Bearer {api_key}"}
        start = requests.post(
            _serverless_api_url(endpoint_id, "run"),
            headers=headers,
            json={
                "input": {
                    "mode": "lipsync_only",
                    "face_filename": os.path.basename(face_path),
                    "face_b64": face_b64,
                    "audio_filename": "voice.wav",
                    "audio_b64": audio_b64,
                }
            },
            timeout=120,
        )
        start.raise_for_status()
        start_data = start.json()
        run_id = start_data.get("id") or start_data.get("jobId")
        if not run_id:
            raise RuntimeError(f"RunPod did not return a job id: {start_data}")

        deadline = time.time() + 60 * 60
        output = None
        while time.time() < deadline:
            status = requests.get(
                _serverless_api_url(endpoint_id, f"status/{run_id}"),
                headers=headers,
                timeout=60,
            )
            status.raise_for_status()
            data = status.json()
            state = str(data.get("status", "")).upper()
            if state == "COMPLETED":
                output = data.get("output") or {}
                break
            if state in {"FAILED", "CANCELLED", "TIMED_OUT"}:
                raise RuntimeError(data.get("error") or data.get("output") or f"RunPod job {state}")
            time.sleep(5)

        if not output:
            raise TimeoutError("RunPod Serverless lip-sync timed out after 60 minutes.")

        video_b64 = output.get("video_b64")
        if not video_b64:
            raise RuntimeError(f"RunPod output did not include video_b64: {output}")

        with open(lipsync_raw, "wb") as out:
            out.write(base64.b64decode(video_b64))

        safe_name = product_name.replace(" ", "_")
        local_name = f"{safe_name}_hybrid_lipsync_{uuid.uuid4().hex[:8]}.mp4"
        local_path = os.path.join(OUTPUT_DIR, local_name)
        overlay_lipsync(base_video, lipsync_raw, local_path, position=position, scale=0.48)

        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["video"] = f"/video/{os.path.basename(local_path)}"
    except Exception as exc:
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = f"RunPod Serverless: {exc}"
    finally:
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)


# ── Entry-point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    app.run(debug=False, host="0.0.0.0", port=5001)

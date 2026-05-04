"""
RunPod Serverless worker for VeeGen lip-sync jobs.

Input:
{
  "product_name": "...",
  "tone": "funny",
  "language": "english",
  "product_type": "general",
  "position": "bottom-right",
  "face_filename": "face.jpg",
  "face_b64": "<base64 image bytes>"
}

Output:
{
  "filename": "...mp4",
  "video_b64": "<base64 mp4 bytes>"
}
"""

import base64
import os
import tempfile
import uuid

import runpod

from lipsync import create_lipsync_video
from lipsync import prepare_face_green_screen, run_wav2lip

os.environ.setdefault("VEEGEN_SERVERLESS_FAST", "1")
os.environ.setdefault("VEEGEN_CMD_TIMEOUT", "540")
os.environ.setdefault("VEEGEN_VIDEO_WIDTH", "720")
os.environ.setdefault("VEEGEN_VIDEO_HEIGHT", "1280")
os.environ.setdefault("VEEGEN_VIDEO_FPS", "24")


def handler(job):
    data = job.get("input") or {}

    mode = (data.get("mode") or "full").strip().lower()
    product_name = (data.get("product_name") or "").strip()
    tone = (data.get("tone") or "funny").strip().lower()
    language = (data.get("language") or "english").strip().lower()
    product_type = (data.get("product_type") or "general").strip().lower()
    position = (data.get("position") or "bottom-right").strip()
    face_b64 = data.get("face_b64") or ""
    face_filename = data.get("face_filename") or f"face_{uuid.uuid4().hex}.jpg"
    audio_b64 = data.get("audio_b64") or ""
    audio_filename = data.get("audio_filename") or "voice.wav"

    if mode != "lipsync_only" and not product_name:
        raise ValueError("product_name is required")
    if not face_b64:
        raise ValueError("face_b64 is required")
    if mode == "lipsync_only" and not audio_b64:
        raise ValueError("audio_b64 is required")

    _, ext = os.path.splitext(face_filename)
    if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"

    with tempfile.TemporaryDirectory() as tmp:
        face_path = os.path.join(tmp, f"face{ext}")
        with open(face_path, "wb") as f:
            f.write(base64.b64decode(face_b64))

        if mode == "lipsync_only":
            audio_ext = os.path.splitext(audio_filename)[1].lower()
            if audio_ext not in (".wav", ".mp3", ".m4a", ".aac"):
                audio_ext = ".wav"
            audio_path = os.path.join(tmp, f"audio{audio_ext}")
            green_face = os.path.join(tmp, "face_green.png")
            output_path = os.path.join(tmp, "lipsync_raw.mp4")
            with open(audio_path, "wb") as f:
                f.write(base64.b64decode(audio_b64))
            prepare_face_green_screen(
                face_path,
                green_face,
                canvas_w=512,
                canvas_h=512,
                upper_body_focus=True,
            )
            run_wav2lip(green_face, audio_path, output_path, resize_factor=2)
        else:
            output_path = create_lipsync_video(
                product_name,
                face_path,
                tone=tone,
                language=language,
                product_type=product_type,
                position=position,
            )

        with open(output_path, "rb") as f:
            video_b64 = base64.b64encode(f.read()).decode("ascii")

    return {
        "filename": os.path.basename(output_path),
        "video_b64": video_b64,
    }


runpod.serverless.start({"handler": handler})

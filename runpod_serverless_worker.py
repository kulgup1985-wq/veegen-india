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


def handler(job):
    data = job.get("input") or {}

    product_name = (data.get("product_name") or "").strip()
    tone = (data.get("tone") or "funny").strip().lower()
    language = (data.get("language") or "english").strip().lower()
    product_type = (data.get("product_type") or "general").strip().lower()
    position = (data.get("position") or "bottom-right").strip()
    face_b64 = data.get("face_b64") or ""
    face_filename = data.get("face_filename") or f"face_{uuid.uuid4().hex}.jpg"

    if not product_name:
        raise ValueError("product_name is required")
    if not face_b64:
        raise ValueError("face_b64 is required")

    _, ext = os.path.splitext(face_filename)
    if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"

    with tempfile.TemporaryDirectory() as tmp:
        face_path = os.path.join(tmp, f"face{ext}")
        with open(face_path, "wb") as f:
            f.write(base64.b64decode(face_b64))

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

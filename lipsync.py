"""
VeeGen — Lip Sync Video Module.

Uses Wav2Lip for lip synchronization, rembg for face-image background
removal, and ffmpeg chromakey + overlay for compositing.

Pipeline:
  1. Remove background from face image (rembg → single image, fast).
  2. Paste transparent face onto a solid green (#00b140) canvas.
  3. Generate base UGC promo video (reuses existing veegen pipeline)
     while saving the voice-only audio track.
  4. Run Wav2Lip with green-screen face + voice audio → lip-synced video.
  5. Chromakey the green out and overlay on the UGC promo via ffmpeg.
"""

import os
import shutil
import subprocess
import sys
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FFMPEG_DIR = os.path.join(BASE_DIR, "ffmpeg")
if os.path.isdir(FFMPEG_DIR) and FFMPEG_DIR not in os.environ.get("PATH", ""):
    os.environ["PATH"] = FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")

WAV2LIP_DIR = os.path.join(BASE_DIR, "wav2lip")
CHECKPOINT_PATH = os.path.join(WAV2LIP_DIR, "checkpoints", "wav2lip_gan.pth")
FACES_DIR = os.path.join(BASE_DIR, "assets", "faces")
OUTPUT_DIR = os.path.join(BASE_DIR, "assets", "output")

# Green-screen colour (BGR-ish for PIL, hex for ffmpeg)
GREEN_HEX = "00b140"
GREEN_RGB = (0, 177, 64)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _run(cmd: list[str], label: str = "cmd") -> subprocess.CompletedProcess:
    """Run a subprocess, raise on failure."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"[LipSync] {label} failed:\n{result.stderr[:1500]}")
    return result


def is_wav2lip_ready() -> bool:
    """Return True if Wav2Lip repo + checkpoint are present."""
    return (
        os.path.isdir(WAV2LIP_DIR)
        and os.path.isfile(CHECKPOINT_PATH)
        and os.path.isfile(os.path.join(WAV2LIP_DIR, "inference.py"))
    )


def get_available_faces() -> list[dict]:
    """Return list of available face images in assets/faces/."""
    os.makedirs(FACES_DIR, exist_ok=True)
    faces = []
    for f in sorted(os.listdir(FACES_DIR)):
        if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            faces.append({
                "name": os.path.splitext(f)[0].replace("_", " ").title(),
                "filename": f,
            })
    return faces


# ── Step 1: Prepare face on green screen ──────────────────────────────────────

def prepare_face_green_screen(face_path: str, output_path: str,
                               canvas_w: int = 512, canvas_h: int = 512) -> str:
    """Remove background from face image using rembg, paste onto green canvas."""
    from PIL import Image
    from rembg import remove

    print("[LipSync] Removing background from face image …")
    img = Image.open(face_path).convert("RGBA")
    cutout = remove(img)

    # Resize cutout to fit canvas while keeping aspect ratio
    cutout.thumbnail((canvas_w, canvas_h), Image.LANCZOS)

    # Create green canvas and centre-paste
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (*GREEN_RGB, 255))
    x = (canvas_w - cutout.width) // 2
    y = (canvas_h - cutout.height) // 2
    canvas.paste(cutout, (x, y), cutout)

    canvas.convert("RGB").save(output_path, quality=95)
    print(f"[LipSync] Green-screen face → {output_path}")
    return output_path


# ── Step 2: Run Wav2Lip ──────────────────────────────────────────────────────

def run_wav2lip(face_path: str, audio_path: str, output_path: str,
                resize_factor: int = 1) -> str:
    """Run Wav2Lip inference.  Requires setup_lipsync.py to have been run."""
    if not is_wav2lip_ready():
        raise RuntimeError(
            "Wav2Lip is not set up. Run:  python setup_lipsync.py"
        )
    if not os.path.isfile(face_path):
        raise FileNotFoundError(f"Face image not found: {face_path}")
    if not os.path.isfile(audio_path):
        raise FileNotFoundError(f"Audio not found: {audio_path}")

    print("[LipSync] Running Wav2Lip inference …")
    cmd = [
        sys.executable,
        os.path.join(WAV2LIP_DIR, "inference.py"),
        "--checkpoint_path", CHECKPOINT_PATH,
        "--face", face_path,
        "--audio", audio_path,
        "--outfile", output_path,
        "--resize_factor", str(resize_factor),
        "--nosmooth",
        "--pads", "0", "15", "0", "0",
    ]
    _run(cmd, "Wav2Lip")

    if not os.path.isfile(output_path):
        raise RuntimeError("Wav2Lip produced no output file.")

    print(f"[LipSync] Wav2Lip output → {output_path}")
    return output_path


# ── Step 3: Chromakey + overlay ──────────────────────────────────────────────

def overlay_lipsync(base_video: str, lipsync_video: str, output_path: str,
                    position: str = "bottom-right",
                    scale: float = 0.30) -> str:
    """Overlay the lip-synced green-screen video onto the base UGC promo.

    Uses ffmpeg chromakey to key out the green, then overlay filter.
    """
    # Position expressions for overlay filter
    positions = {
        "bottom-right":  f"x=W-w-40:y=H-h-160",
        "bottom-left":   f"x=40:y=H-h-160",
        "bottom-center": f"x=(W-w)/2:y=H-h-160",
        "top-right":     f"x=W-w-40:y=100",
        "top-left":      f"x=40:y=100",
    }
    pos_expr = positions.get(position, positions["bottom-right"])

    # Complex filter:
    #   [1:v] scale to scale% of base width → chromakey green → overlay on [0:v]
    vf = (
        f"[1:v]scale=iw*{scale}:ih*{scale},"
        f"chromakey=0x{GREEN_HEX}:similarity=0.25:blend=0.08[fg];"
        f"[0:v][fg]overlay={pos_expr}:shortest=1[vout]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", base_video,
        "-i", lipsync_video,
        "-filter_complex", vf,
        "-map", "[vout]",
        "-map", "0:a",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "21",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        output_path,
    ]
    print(f"[LipSync] Overlaying lip-sync on base video …")
    _run(cmd, "overlay")
    print(f"[LipSync] Final output → {output_path}")
    return output_path


# ── Full pipeline ─────────────────────────────────────────────────────────────

def create_lipsync_video(
    product_name: str,
    face_path: str,
    tone: str = "funny",
    language: str = "english",
    product_type: str = "general",
    position: str = "bottom-right",
    face_scale: float = 0.30,
) -> str:
    """End-to-end: generate UGC promo + lip sync overlay.

    Returns the path to the final composited video.
    """
    from veegen import create_video as _create_base, OUTPUT_DIR as _OUT

    vid_id = uuid.uuid4().hex[:8]
    work_dir = os.path.join(_OUT, f"_lipsync_{vid_id}")
    os.makedirs(work_dir, exist_ok=True)

    try:
        # 1 — Generate the base UGC promo (with voice audio saved)
        print("\n[LipSync] ═══ Stage 1: Generating base UGC promo ═══")
        base_video = _create_base(
            product_name, tone=tone, language=language,
            product_type=product_type,
        )

        # 2 — Extract voice-only audio from the base video for lip-sync
        voice_audio = os.path.join(work_dir, "voice.wav")
        _run([
            "ffmpeg", "-y", "-i", base_video,
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            voice_audio,
        ], "extract audio")

        # 3 — Prepare face on green screen
        print("\n[LipSync] ═══ Stage 2: Preparing face ═══")
        green_face = os.path.join(work_dir, "face_green.jpg")
        prepare_face_green_screen(face_path, green_face)

        # 4 — Run Wav2Lip
        print("\n[LipSync] ═══ Stage 3: Lip-sync generation ═══")
        lipsync_raw = os.path.join(work_dir, "lipsync_raw.mp4")
        run_wav2lip(green_face, voice_audio, lipsync_raw)

        # 5 — Overlay onto base video
        print("\n[LipSync] ═══ Stage 4: Compositing final video ═══")
        safe_name = product_name.replace(" ", "_")
        final_name = f"{safe_name}_lipsync_{vid_id}.mp4"
        final_path = os.path.join(_OUT, final_name)
        overlay_lipsync(base_video, lipsync_raw, final_path,
                        position=position, scale=face_scale)

        return final_path

    finally:
        # Clean up working directory
        if os.path.isdir(work_dir):
            shutil.rmtree(work_dir, ignore_errors=True)

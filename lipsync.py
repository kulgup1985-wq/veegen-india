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

# Add bundled ffmpeg bin/ to PATH on Windows.
import glob as _glob
_ffmpeg_bins = _glob.glob(os.path.join(BASE_DIR, "ffmpeg", "*", "bin"))
for _b in _ffmpeg_bins:
    if os.path.isdir(_b) and _b not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _b + os.pathsep + os.environ.get("PATH", "")

WAV2LIP_DIR = os.path.join(BASE_DIR, "wav2lip")
CHECKPOINT_PATH = os.path.join(WAV2LIP_DIR, "checkpoints", "wav2lip_gan.pth")
FACES_DIR = os.path.join(BASE_DIR, "assets", "faces")
OUTPUT_DIR = os.path.join(BASE_DIR, "assets", "output")

# Green-screen colour (BGR-ish for PIL, hex for ffmpeg)
GREEN_HEX = "00b140"
GREEN_RGB = (0, 177, 64)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _run(cmd: list[str], label: str = "cmd", cwd: str = None) -> subprocess.CompletedProcess:
    """Run a subprocess, raise on failure."""
    timeout = int(os.environ.get("VEEGEN_CMD_TIMEOUT", "900"))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr or ""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"[LipSync] {label} timed out after {timeout}s:\n{stderr[-3000:]}")
    if result.stdout:
        print(f"[{label}] stdout: {result.stdout[:500]}")
    if result.stderr:
        print(f"[{label}] stderr: {result.stderr[:500]}")
    if result.returncode != 0:
        raise RuntimeError(f"[LipSync] {label} failed:\n{result.stderr[-3000:]}")
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
                               canvas_w: int = 720, canvas_h: int = 720) -> str:
    """Remove background from face image using rembg, paste onto green canvas."""
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    from rembg import remove

    print("[LipSync] Removing background from face image …")
    img = ImageOps.exif_transpose(Image.open(face_path)).convert("RGBA")

    try:
        cutout = remove(
            img,
            alpha_matting=True,
            alpha_matting_foreground_threshold=240,
            alpha_matting_background_threshold=10,
            alpha_matting_erode_size=8,
            post_process_mask=True,
        )
    except Exception as exc:
        print(f"[LipSync] Alpha matting failed; falling back to standard cutout: {exc}")
        cutout = remove(img, post_process_mask=True)

    cutout = cutout.convert("RGBA")

    # Tighten noisy edges/halos that become visible after the green-screen key.
    alpha = cutout.getchannel("A")
    alpha = alpha.filter(ImageFilter.MedianFilter(size=3))
    alpha = alpha.filter(ImageFilter.GaussianBlur(radius=0.45))
    alpha = ImageEnhance.Contrast(alpha).enhance(1.25)
    alpha = alpha.point(lambda p: 0 if p < 6 else (255 if p > 248 else p))
    cutout.putalpha(alpha)

    bbox = alpha.getbbox()
    if bbox:
        pad = 16
        left = max(0, bbox[0] - pad)
        top = max(0, bbox[1] - pad)
        right = min(cutout.width, bbox[2] + pad)
        bottom = min(cutout.height, bbox[3] + pad)
        cutout = cutout.crop((left, top, right, bottom))

    # Resize cutout to fit canvas while keeping aspect ratio
    cutout.thumbnail((canvas_w, canvas_h), Image.LANCZOS)

    # Create green canvas and centre-paste
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (*GREEN_RGB, 255))
    x = (canvas_w - cutout.width) // 2
    y = (canvas_h - cutout.height) // 2
    canvas.paste(cutout, (x, y), cutout)

    canvas.convert("RGB").save(output_path)
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
    # Ensure temp/ exists inside wav2lip dir (inference.py writes to temp/result.avi)
    os.makedirs(os.path.join(WAV2LIP_DIR, "temp"), exist_ok=True)
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
    _run(cmd, "Wav2Lip", cwd=WAV2LIP_DIR)

    if not os.path.isfile(output_path):
        raise RuntimeError("Wav2Lip produced no output file.")

    print(f"[LipSync] Wav2Lip output → {output_path}")
    return output_path


# ── Step 3: Chromakey + overlay ──────────────────────────────────────────────

def overlay_lipsync(base_video: str, lipsync_video: str, output_path: str,
                    position: str = "bottom-right",
                    scale: float = 0.58) -> str:
    """Overlay the lip-synced green-screen video onto the base UGC promo.

    Uses ffmpeg chromakey to key out the green, then overlay filter.
    """
    # Position expressions for overlay filter
    positions = {
        "bottom-right":  "x=W-w-18:y=H-h",
        "bottom-left":   "x=18:y=H-h",
        "bottom-center": "x=(W-w)/2:y=H-h",
        "top-right":     "x=W-w-18:y=18",
        "top-left":      "x=18:y=18",
    }
    pos_expr = positions.get(position, positions["bottom-right"])

    # Complex filter:
    #   [1:v] scale to scale% of base width → colorkey green → overlay on [0:v]
    #   Using colorkey (not chromakey) for better handling of JPEG/encoding artifacts
    vf = (
        f"[1:v][0:v]scale2ref=w=-2:h=main_h*{scale}[fgs][base];"
        f"[fgs]"
        f"chromakey=0x{GREEN_HEX}:similarity=0.22:blend=0.06[fg];"
        f"[base][fg]overlay={pos_expr}:shortest=1[vout]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", base_video,
        "-i", lipsync_video,
        "-filter_complex", vf,
        "-map", "[vout]",
        "-map", "0:a?",
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
    face_scale: float = 0.58,
) -> str:
    """End-to-end: generate UGC promo + lip sync overlay.

    Returns the path to the final composited video.
    """
    from veegen import create_video as _create_base, OUTPUT_DIR as _OUT

    vid_id = uuid.uuid4().hex[:8]
    work_dir = os.path.join(_OUT, f"_lipsync_{vid_id}")
    os.makedirs(work_dir, exist_ok=True)

    try:
        fast_serverless = os.environ.get("VEEGEN_SERVERLESS_FAST") == "1"
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
        green_face = os.path.join(work_dir, "face_green.png")
        if fast_serverless:
            prepare_face_green_screen(face_path, green_face, canvas_w=256, canvas_h=256)
        else:
            prepare_face_green_screen(face_path, green_face)

        # 4 — Run Wav2Lip
        print("\n[LipSync] ═══ Stage 3: Lip-sync generation ═══")
        lipsync_raw = os.path.join(work_dir, "lipsync_raw.mp4")
        run_wav2lip(
            green_face,
            voice_audio,
            lipsync_raw,
            resize_factor=4 if fast_serverless else 1,
        )

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

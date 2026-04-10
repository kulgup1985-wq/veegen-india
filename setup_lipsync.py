"""
VeeGen — Wav2Lip Setup Script.

Run once to download and configure Wav2Lip for lip-sync video generation.

Usage:
    python setup_lipsync.py
"""

import os
import subprocess
import sys
import urllib.request
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WAV2LIP_DIR = os.path.join(BASE_DIR, "wav2lip")
CHECKPOINTS_DIR = os.path.join(WAV2LIP_DIR, "checkpoints")
FACES_DIR = os.path.join(BASE_DIR, "assets", "faces")

# URLs
WAV2LIP_REPO = "https://github.com/Rudrabha/Wav2Lip.git"
# Wav2Lip GAN checkpoint (best quality)
WAV2LIP_GAN_URL = (
    "https://iiitaphyd-my.sharepoint.com/personal/radrabha_m_research_iiit_ac_in/"
    "_layouts/15/download.aspx?share=EdjI7bZlgApMqsVoEUUXpLsBxqXbn5z8VTmoxp55YNDcIA"
)
# s3fd face detection model
S3FD_URL = (
    "https://www.adrianbulat.com/downloads/python-fan/s3fd-619a316812.pth"
)

REQUIRED_PACKAGES = [
    "torch",
    "torchvision",
    "numpy",
    "opencv-python",
    "librosa",
    "scipy",
    "tqdm",
    "numba",
    "rembg",
    "Pillow",
]


def step(msg: str) -> None:
    print(f"\n{'─' * 50}")
    print(f"  {msg}")
    print(f"{'─' * 50}")


def install_packages() -> None:
    step("Installing required Python packages")
    for pkg in REQUIRED_PACKAGES:
        print(f"  → {pkg}")
    subprocess.check_call([
        sys.executable, "-m", "pip", "install", "--quiet", *REQUIRED_PACKAGES,
    ])
    print("  ✓ All packages installed")


def clone_wav2lip() -> None:
    step("Cloning Wav2Lip repository")
    if os.path.isdir(WAV2LIP_DIR):
        print(f"  Already exists at {WAV2LIP_DIR}")
        return
    subprocess.check_call(["git", "clone", WAV2LIP_REPO, WAV2LIP_DIR])
    print(f"  ✓ Cloned to {WAV2LIP_DIR}")


def download_checkpoint() -> None:
    step("Downloading Wav2Lip GAN checkpoint (~400 MB)")
    os.makedirs(CHECKPOINTS_DIR, exist_ok=True)
    dest = os.path.join(CHECKPOINTS_DIR, "wav2lip_gan.pth")
    if os.path.isfile(dest) and os.path.getsize(dest) > 100_000_000:
        print(f"  Already exists ({os.path.getsize(dest) / 1e6:.0f} MB)")
        return
    print(f"  Downloading to {dest} …")
    urllib.request.urlretrieve(WAV2LIP_GAN_URL, dest)
    size_mb = os.path.getsize(dest) / 1e6
    print(f"  ✓ Downloaded ({size_mb:.0f} MB)")


def download_face_detection() -> None:
    step("Downloading s3fd face detection model")
    det_dir = os.path.join(WAV2LIP_DIR, "face_detection", "detection",
                           "sfd")
    os.makedirs(det_dir, exist_ok=True)
    dest = os.path.join(det_dir, "s3fd.pth")
    if os.path.isfile(dest) and os.path.getsize(dest) > 10_000_000:
        print(f"  Already exists ({os.path.getsize(dest) / 1e6:.0f} MB)")
        return
    print(f"  Downloading to {dest} …")
    urllib.request.urlretrieve(S3FD_URL, dest)
    size_mb = os.path.getsize(dest) / 1e6
    print(f"  ✓ Downloaded ({size_mb:.0f} MB)")


def create_faces_dir() -> None:
    step("Creating assets/faces/ directory")
    os.makedirs(FACES_DIR, exist_ok=True)
    readme = os.path.join(FACES_DIR, "README.txt")
    if not os.path.isfile(readme):
        with open(readme, "w") as f:
            f.write(
                "Place face images (.jpg, .png, .webp) here.\n"
                "These will appear as selectable faces in the VeeGen UI.\n\n"
                "Tips:\n"
                "- Use a clear, front-facing portrait photo\n"
                "- Good lighting and neutral background work best\n"
                "- Minimum resolution: 256×256 pixels\n"
                "- Supported formats: .jpg, .jpeg, .png, .webp\n"
            )
    count = sum(
        1 for f in os.listdir(FACES_DIR)
        if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
    )
    print(f"  ✓ Directory ready — {count} face image(s) found")
    if count == 0:
        print("  ⚠ Add face images to assets/faces/ to use lip sync")


def verify_setup() -> None:
    step("Verifying setup")
    checks = [
        ("Wav2Lip repo", os.path.isfile(os.path.join(WAV2LIP_DIR, "inference.py"))),
        ("GAN checkpoint", os.path.isfile(os.path.join(CHECKPOINTS_DIR, "wav2lip_gan.pth"))),
        ("s3fd model", os.path.isfile(os.path.join(WAV2LIP_DIR, "face_detection",
                                                     "detection", "sfd", "s3fd.pth"))),
    ]
    all_ok = True
    for name, ok in checks:
        status = "✓" if ok else "✗"
        print(f"  {status} {name}")
        if not ok:
            all_ok = False

    # Check torch
    try:
        import torch
        gpu = torch.cuda.is_available()
        device = torch.cuda.get_device_name(0) if gpu else "CPU only"
        print(f"  ✓ PyTorch {torch.__version__} ({device})")
        if not gpu:
            print("  ⚠ No GPU detected — lip sync will be slow on CPU")
    except ImportError:
        print("  ✗ PyTorch not installed")
        all_ok = False

    if all_ok:
        print("\n  ✅ Wav2Lip setup complete! Ready to generate lip-sync videos.")
    else:
        print("\n  ❌ Some components missing. Please fix errors above.")


def main() -> None:
    print("=" * 50)
    print("  VeeGen — Wav2Lip Setup")
    print("=" * 50)

    install_packages()
    clone_wav2lip()
    download_checkpoint()
    download_face_detection()
    create_faces_dir()
    verify_setup()


if __name__ == "__main__":
    main()

# VeeGen — UGC-Style Video Generator

Generates a short UGC-style promotional video from a product name:

1. Creates a 4–5 line spoken script  
2. Converts it to speech (gTTS)  
3. Picks 5 random clips from `assets/clips/`  
4. Merges them and overlays the voiceover  
5. Outputs `assets/output/final.mp4`

---

## Prerequisites

- **Python 3.10+**
- **ffmpeg** — must be on your system PATH

### Install ffmpeg

| OS | Command |
|----|---------|
| Windows (winget) | `winget install FFmpeg` |
| Windows (choco) | `choco install ffmpeg` |
| macOS | `brew install ffmpeg` |
| Ubuntu / Debian | `sudo apt install ffmpeg` |

Verify with: `ffmpeg -version`

---

## Setup

```bash
# Clone / navigate to the project
cd VeeGen

# (Optional) Create a virtual environment
python -m venv venv
venv\Scripts\activate      # Windows
# source venv/bin/activate # macOS / Linux

# Install Python dependencies
pip install -r requirements.txt
```

---

## Add Video Clips

Place at least 5 short video files (`.mp4`, `.mov`, `.avi`, `.mkv`, `.webm`) into:

```
assets/clips/
```

These are the raw clips that will be randomly selected and merged.

---

## Run

```bash
python veegen.py
```

You'll be prompted to enter a product name. The final video will be saved to:

```
assets/output/final.mp4
```

---

## Project Structure

```
VeeGen/
├── assets/
│   ├── clips/          ← put your source video clips here
│   └── output/         ← generated video lands here
├── veegen.py           ← main script
├── requirements.txt
└── README.md
```

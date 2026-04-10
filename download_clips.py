"""
download_clips.py — Fetch free stock clips from Pixabay for each keyword folder.

Usage:
    python download_clips.py            (prompts for API key)
    python download_clips.py YOUR_KEY   (pass key as argument)

Get a free key at https://pixabay.com/api/docs/#api_search_videos
"""

import os
import sys
import time
import urllib.request
import urllib.parse
import json

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLIPS_DIR = os.path.join(BASE_DIR, "assets", "clips")

# How many clips to download per keyword
CLIPS_PER_KEYWORD = 3

# Pixabay search queries mapped to each keyword folder.
KEYWORD_SEARCH_QUERIES: dict[str, str] = {
    "surprise":       "surprised reaction person",
    "attention":      "person talking camera",
    "curiosity":      "curious person looking",
    "excitement":     "excited celebrating happy",
    "frustration":    "frustrated stressed person",
    "doubt":          "thinking confused person",
    "hesitation":     "unsure person pondering",
    "struggle":       "struggling difficulty person",
    "transformation": "transformation glow up beauty",
    "amazement":      "amazed wow reaction",
    "confidence":     "confident smiling person",
    "satisfaction":   "satisfied happy relaxed",
    "loyalty":        "daily routine lifestyle",
    "regret":         "sad regret looking down",
    "fomo":           "friends fun party",
    "social-proof":   "group people positive happy",
    "urgency":        "rushing hurry fast",
    "trust":          "honest sincere speaking",
    "encouragement":  "encouraging motivational person",
    "scarcity":       "exclusive premium product luxury",
    "general":        "aesthetic lifestyle product",
}


def fetch_pixabay_videos(query: str, api_key: str, per_page: int = 5) -> list[dict]:
    """Search Pixabay Videos API and return a list of hit dicts."""
    params = urllib.parse.urlencode({
        "key": api_key,
        "q": query,
        "per_page": max(min(per_page, 200), 3),
    })
    url = f"https://pixabay.com/api/videos/?{params}"

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "VeeGen-Downloader/1.0",
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            return data.get("hits", [])
    except Exception as exc:
        print(f"    [WARN] API request failed for '{query}': {exc}")
        return []


def pick_best_url(hit: dict) -> str | None:
    """Pick the best download URL from a Pixabay video hit.

    Pixabay provides: large (1920), medium (1280), small (960), tiny (640).
    We prefer 'medium' for a good balance of quality and download speed.
    """
    videos = hit.get("videos", {})
    # Preference order
    for size in ("medium", "small", "large", "tiny"):
        entry = videos.get(size, {})
        url = entry.get("url")
        if url:
            return url
    return None


def download_file(url: str, dest: str) -> bool:
    """Download a file from *url* to *dest*. Returns True on success."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "VeeGen-Downloader/1.0",
        })
        with urllib.request.urlopen(req, timeout=120) as resp:
            total = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 64)
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
        if total < 10_000:
            # Suspiciously small — probably an error page
            os.remove(dest)
            return False
        return True
    except Exception as exc:
        print(f"    [WARN] Download failed: {exc}")
        if os.path.exists(dest):
            os.remove(dest)
        return False


def main() -> None:
    # Get API key
    if len(sys.argv) > 1:
        api_key = sys.argv[1].strip()
    else:
        api_key = input("Enter your Pixabay API key: ").strip()

    if not api_key:
        print("[ERROR] API key is required.")
        print("        Get one free at https://pixabay.com/api/docs/")
        sys.exit(1)

    # Test the key
    print("Verifying API key...")
    test = fetch_pixabay_videos("nature", api_key, per_page=1)
    if not test:
        print("[ERROR] API key seems invalid or Pixabay is unreachable.")
        sys.exit(1)
    print("API key OK!\n")

    total_downloaded = 0

    for keyword, query in KEYWORD_SEARCH_QUERIES.items():
        folder = os.path.join(CLIPS_DIR, keyword)
        os.makedirs(folder, exist_ok=True)

        # Count existing clips
        existing = [
            f for f in os.listdir(folder)
            if os.path.splitext(f)[1].lower() in {".mp4", ".mov", ".avi", ".mkv", ".webm"}
        ]
        needed = CLIPS_PER_KEYWORD - len(existing)
        if needed <= 0:
            print(f"[{keyword}] Already has {len(existing)} clip(s) — skipping")
            continue

        print(f"[{keyword}] Searching Pixabay for: \"{query}\"")
        hits = fetch_pixabay_videos(query, api_key, per_page=needed + 3)

        downloaded = 0
        for hit in hits:
            if downloaded >= needed:
                break

            file_url = pick_best_url(hit)
            if not file_url:
                continue

            vid_id = hit.get("id", "unknown")
            filename = f"pixabay_{vid_id}.mp4"
            dest = os.path.join(folder, filename)

            if os.path.exists(dest):
                continue

            print(f"  ↓ Downloading {filename}...")
            if download_file(file_url, dest):
                downloaded += 1
                total_downloaded += 1

        print(f"  ✓ {downloaded} clip(s) downloaded to {keyword}/")

        # Polite rate-limiting
        time.sleep(0.5)

    print(f"\nDone! {total_downloaded} total clips downloaded to {CLIPS_DIR}")


if __name__ == "__main__":
    main()

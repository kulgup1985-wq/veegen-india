"""
VeeGen — UGC-style video generator.

Takes a product name, generates a structured 5-scene plan,
converts it to speech, picks clips, and merges everything
into a final video with subtitles.
"""

import hashlib
import os
import random
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from gtts import gTTS

# ── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Add bundled ffmpeg bin/ to PATH on Windows (the essentials build lives in a
# versioned sub-folder, so we search for the first bin/ directory under ffmpeg/).
import glob as _glob
_ffmpeg_bins = _glob.glob(os.path.join(BASE_DIR, "ffmpeg", "*", "bin"))
for _b in _ffmpeg_bins:
    if os.path.isdir(_b) and _b not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _b + os.pathsep + os.environ.get("PATH", "")
CLIPS_DIR = os.path.join(BASE_DIR, "assets", "clips")
OUTPUT_DIR = os.path.join(BASE_DIR, "assets", "output")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "final.mp4")
CACHE_DIR = os.path.join(BASE_DIR, "assets", "cache")
BGM_DIR = os.path.join(BASE_DIR, "assets", "bgm")

# ── Video settings ───────────────────────────────────────────────────────────
VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
VIDEO_FPS = 30

# ── Settings ─────────────────────────────────────────────────────────────────
NUM_VARIATIONS = 3

# ── Audio settings ───────────────────────────────────────────────────────────
BGM_VOLUME = 0.10  # background music volume (0.0–1.0, relative to voice)

# ── Transition settings ─────────────────────────────────────────────────────
TRANSITION_DUR_MIN = 0.3
TRANSITION_DUR_MAX = 0.6
TRANSITION_TYPES = [
    # xfade / fade family
    "fade", "fadeblack", "fadewhite", "fadegrays",
    # wipe family
    "wipeleft", "wiperight", "wipeup", "wipedown",
    # slide family
    "slideleft", "slideright", "slideup", "slidedown",
    # smooth / push family
    "smoothleft", "smoothright", "smoothup", "smoothdown",
    # zoom / scale family
    "circlecrop", "circleclose", "circleopen",
    "squeezeh", "squeezev",
    # radial / diagonal
    "radial", "diagtl", "diagtr", "diagbl", "diagbr",
    # misc
    "dissolve", "pixelize", "horzclose", "horzopen",
    "vertclose", "vertopen",
]
SUPPORTED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

# Per-scene-type colour grading applied during trim.
# Each value is an ffmpeg filter snippet appended after zoompan.
SCENE_COLOR_FILTERS: dict[str, str] = {
    # hook:     high contrast + vibrant saturation + slight vignette
    "hook":     "eq=contrast=1.15:saturation=1.35:brightness=0.04,vignette=PI/4",
    # problem:  desaturated, cool teal tone + vignette for moodiness
    "problem":  "eq=saturation=0.6:contrast=1.08:brightness=-0.03,colorbalance=rs=-0.04:bs=0.06,vignette=PI/3.5",
    # solution: warm golden tone via colour-balance + slight bloom
    "solution": "eq=saturation=1.15:contrast=1.08:brightness=0.02,colorbalance=rs=0.10:gs=0.04:bs=-0.08",
    # bridge:   subtle warm lift for continuity
    "bridge":   "eq=saturation=1.05:contrast=1.03:brightness=0.01,colorbalance=rs=0.03:bs=-0.02",
    # cta:      punchy bright with slight vignette to draw eye center
    "cta":      "eq=brightness=0.08:contrast=1.12:saturation=1.2,vignette=PI/5",
}


# ── Scene data ───────────────────────────────────────────────────────────────
@dataclass
class Scene:
    """One segment of the video with its script line and metadata."""
    label: str          # e.g. "hook", "problem", ...
    text: str           # the actual spoken line
    keyword: str        # emotion / action tag
    duration: float     # target duration in seconds


# ── Helpers ──────────────────────────────────────────────────────────────────
def check_ffmpeg() -> None:
    """Raise RuntimeError if ffmpeg / ffprobe are missing. Auto-installs on Linux."""
    import platform
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is not None:
            continue
        if platform.system() == "Linux":
            print(f"[VeeGen] {tool} not found — attempting apt-get update && install ffmpeg …")
            subprocess.run(["apt-get", "update"], check=False)
            subprocess.run(
                ["apt-get", "install", "-y", "ffmpeg"],
                check=False,
            )
        if shutil.which(tool) is None:
            raise RuntimeError(
                f"{tool} not found on PATH. "
                "Install it: https://ffmpeg.org/download.html"
            )


def run_ffmpeg(cmd: list[str], step_label: str) -> None:
    """Run an ffmpeg command; on failure raise with stderr details."""
    result = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        err = result.stderr.decode(errors="replace")
        raise RuntimeError(f"ffmpeg failed during '{step_label}': {err[:500]}")


def get_duration(media_path: str) -> float:
    """Return the duration (seconds) of an audio or video file."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        media_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {media_path}: {result.stderr}")
    return float(result.stdout.strip())


# ── 1. Script generation (template-based scene plan) ────────────────────────
# Each entry is (template_text, keyword).  {product} is filled at runtime.
# ── Tone: funny (default, casual / humorous) ────────────────────────────────
HOOKS_FUNNY = [
    ("Okay so I finally tried {product} and I'm honestly shocked.", "surprise"),
    ("Stop scrolling! You need to know about {product}.", "attention"),
    ("I need to talk about {product} real quick.", "curiosity"),
    ("Wait, have you heard about {product} yet?", "curiosity"),
    ("So everyone's been talking about {product} and I had to try it.", "excitement"),
    ("I was today years old when I discovered {product}.", "surprise"),
    ("POV: You just found {product} and your life is about to change.", "excitement"),
    ("No one told me about {product} so I'm telling you.", "attention"),
    ("I can't believe I almost scrolled past {product}.", "surprise"),
    ("This little thing called {product} has the internet going crazy.", "excitement"),
    ("My jaw literally dropped when I tried {product}.", "amazement"),
    ("Okay but why is nobody talking about {product}?", "curiosity"),
    ("Three words: {product} is unreal.", "attention"),
    ("If you only try one thing this month, make it {product}.", "urgency"),
    ("Hot take: {product} is the best thing I've bought all year.", "confidence"),
]
PROBLEMS_FUNNY = [
    ("I've tried so many products before and nothing actually worked.", "frustration"),
    ("I was super skeptical at first because I've been let down before.", "doubt"),
    ("Honestly I almost didn't buy it because the hype seemed too good to be true.", "hesitation"),
    ("I used to struggle with this every single day and it was so frustrating.", "struggle"),
    ("I literally wasted so much money on things that didn't deliver.", "frustration"),
    ("My friends kept telling me to try something new but nothing clicked.", "doubt"),
    ("Every single product I tried was either overpriced or overhyped.", "frustration"),
    ("I was this close to giving up on finding something decent.", "hesitation"),
    ("Imagine throwing money at things that just collect dust, that was me.", "struggle"),
    ("I had zero expectations left because everything disappointed me.", "doubt"),
    ("At this point I was convinced nothing would work for me.", "hesitation"),
    ("I kept falling for marketing tricks and getting burned every time.", "frustration"),
]
SOLUTIONS_FUNNY = [
    ("But {product} literally changed everything for me in just one week.", "transformation"),
    ("Then I started using {product} and the results were insane.", "amazement"),
    ("{product} just works, no gimmicks, no nonsense, just results.", "confidence"),
    ("After one week with {product} my friends keep asking me what changed.", "transformation"),
    ("I've been using {product} daily and I genuinely can't imagine going back.", "satisfaction"),
    ("{product} is hands down the best purchase I've made this year.", "confidence"),
    ("Turns out {product} is the real deal and I have the proof.", "confidence"),
    ("{product} delivered overnight what others couldn't do in months.", "amazement"),
    ("One try of {product} and I knew this was completely different.", "transformation"),
    ("I'm not even joking, {product} fixed what nothing else could.", "amazement"),
    ("Everything clicked the second I started using {product}.", "satisfaction"),
    ("{product} made me a believer in like two days flat.", "transformation"),
]
BRIDGES_FUNNY = [
    ("It's now part of my daily routine and I'm never switching.", "loyalty"),
    ("Seriously, I wish I had found this sooner.", "regret"),
    ("If you haven't tried {product} yet, you're missing out big time.", "fomo"),
    ("Everyone I've showed it to ends up buying their own.", "social-proof"),
    ("The difference is night and day, I'm not even exaggerating.", "amazement"),
    ("I keep recommending it to everyone and they all love it.", "social-proof"),
    ("My roommate stole mine so I had to order another {product}.", "social-proof"),
    ("I'm literally the {product} ambassador in my friend group now.", "loyalty"),
    ("People keep asking me what's different and I just smile and say {product}.", "confidence"),
    ("I've already stocked up because I'm never running out of {product}.", "loyalty"),
    ("Even my mom asked where to get {product} after she saw my results.", "social-proof"),
    ("The before and after speaks for itself honestly.", "transformation"),
]
CTAS_FUNNY = [
    ("Link is in my bio, go grab yours now!", "urgency"),
    ("Trust me on this one, just try it.", "trust"),
    ("Do yourself a favor and check it out!", "encouragement"),
    ("You'll thank me later, link in bio!", "urgency"),
    ("Don't sleep on this, seriously go get it.", "urgency"),
    ("Click the link before it sells out again!", "scarcity"),
    ("Run don't walk, {product} is worth every penny.", "urgency"),
    ("Comment 'NEED' and I'll send you the link!", "encouragement"),
    ("You've been warned, {product} is addictive, link below.", "fomo"),
    ("Save this video and grab {product} tonight.", "scarcity"),
    ("If this video hits a thousand likes I'll do another {product} review.", "social-proof"),
    ("Seriously just get {product} and thank me in the comments.", "trust"),
]

# ── Tone: emotional (heartfelt / inspiring) ──────────────────────────────────
HOOKS_EMOTIONAL = [
    ("This is the story of how {product} changed my life.", "transformation"),
    ("I never thought something so simple could mean so much to me.", "surprise"),
    ("Some things just touch your heart, and {product} is one of them.", "loyalty"),
    ("{product} found me when I needed it most.", "transformation"),
    ("I wasn't looking for {product}, but it was exactly what I needed.", "satisfaction"),
    ("Let me share something personal about my experience with {product}.", "curiosity"),
    ("I still remember the exact moment I found {product}.", "surprise"),
    ("Close your eyes and imagine your life with {product} in it.", "curiosity"),
    ("If {product} didn't exist I honestly don't know where I'd be.", "loyalty"),
    ("This isn't just a review, this is my real story with {product}.", "trust"),
    ("{product} is the one thing I'd save in a fire.", "loyalty"),
    ("I need you to hear this because {product} changed everything for me.", "attention"),
    ("They say some things find you for a reason, that was {product}.", "transformation"),
    ("This one is personal, let me tell you about {product}.", "curiosity"),
]
PROBLEMS_EMOTIONAL = [
    ("For the longest time I felt like nothing could help me.", "struggle"),
    ("I was exhausted from trying and failing over and over again.", "frustration"),
    ("There were moments I almost gave up on finding a solution.", "doubt"),
    ("I carried that weight every single day and it was breaking me.", "struggle"),
    ("I'd lost hope that anything would actually make a difference.", "doubt"),
    ("The disappointment of failed promises was so overwhelming.", "frustration"),
    ("Nobody around me understood what I was going through.", "struggle"),
    ("I cried more times than I can count trying to figure this out.", "frustration"),
    ("Some days I didn't even want to try anymore.", "doubt"),
    ("The hardest part was pretending everything was fine when it wasn't.", "struggle"),
    ("I was running on empty and nothing was filling me back up.", "hesitation"),
    ("I'd smile on the outside but inside I was completely drained.", "frustration"),
]
SOLUTIONS_EMOTIONAL = [
    ("Then {product} came into my life and everything shifted.", "transformation"),
    ("{product} gave me back something I thought I'd lost forever.", "amazement"),
    ("For the first time in years I felt like myself again thanks to {product}.", "satisfaction"),
    ("{product} didn't just solve a problem, it restored my confidence.", "confidence"),
    ("The moment I tried {product} I knew this was different.", "amazement"),
    ("{product} brought back the joy I'd been missing for so long.", "transformation"),
    ("Something inside me clicked the day I started using {product}.", "transformation"),
    ("{product} didn't just help me, it healed a part of me.", "satisfaction"),
    ("I finally felt seen and understood because of {product}.", "confidence"),
    ("The change was so real that even I couldn't deny it.", "amazement"),
    ("{product} gave me permission to believe in myself again.", "confidence"),
    ("For once something actually lived up to its promise, that was {product}.", "satisfaction"),
]
BRIDGES_EMOTIONAL = [
    ("Now every day feels a little brighter because of {product}.", "satisfaction"),
    ("I hold {product} close because it truly changed my journey.", "loyalty"),
    ("If I could go back and find {product} sooner I would in a heartbeat.", "regret"),
    ("The people around me noticed the change before I even said anything.", "social-proof"),
    ("I'm grateful every single day that I found {product}.", "loyalty"),
    ("It's more than a product to me, it's part of who I am now.", "loyalty"),
    ("My family saw the difference in me and they all want {product} now.", "social-proof"),
    ("I wake up grateful because {product} gave me that reason.", "satisfaction"),
    ("Looking back, that one decision to try {product} changed my whole path.", "transformation"),
    ("I tell everyone who'll listen because {product} deserves to be known.", "social-proof"),
    ("The old me wouldn't recognize who I've become thanks to {product}.", "transformation"),
    ("I keep {product} with me everywhere like a little piece of hope.", "loyalty"),
]
CTAS_EMOTIONAL = [
    ("If this resonates with you, give {product} a chance.", "trust"),
    ("You deserve to feel this way too, link in bio.", "encouragement"),
    ("Take that first step for yourself, you won't regret it.", "trust"),
    ("Let {product} do for you what it did for me.", "encouragement"),
    ("Your future self will thank you, try {product} today.", "urgency"),
    ("Don't wait like I did, start your journey now.", "urgency"),
    ("Give yourself the gift of {product}, you've earned it.", "encouragement"),
    ("One small step toward {product} could change your whole story.", "trust"),
    ("You don't have to keep struggling, {product} is right here.", "encouragement"),
    ("I wish someone had told me about {product} sooner, so I'm telling you.", "trust"),
    ("Let this be your sign, try {product} and feel the difference.", "urgency"),
    ("Your journey starts the moment you choose {product}.", "encouragement"),
]

# ── Tone: luxury (premium / sophisticated) ───────────────────────────────────
HOOKS_LUXURY = [
    ("Allow me to introduce you to {product}, a class above the rest.", "confidence"),
    ("If you appreciate the finer things, you need to know about {product}.", "curiosity"),
    ("{product} is not for everyone, and that's exactly the point.", "attention"),
    ("Discover what true quality feels like with {product}.", "curiosity"),
    ("There's premium, and then there's {product}.", "confidence"),
    ("Elevate your standards with {product}.", "excitement"),
    ("Some things are crafted, not manufactured. {product} is one of them.", "confidence"),
    ("I don't settle, and that's exactly how I found {product}.", "attention"),
    ("Luxury isn't a price tag, it's a feeling. Meet {product}.", "curiosity"),
    ("The moment you hold {product} you understand why it exists.", "amazement"),
    ("Only a select few will understand the brilliance of {product}.", "attention"),
    ("Welcome to a new tier of excellence with {product}.", "excitement"),
    ("{product} doesn't compete, it sets the standard.", "confidence"),
    ("Refined taste, refined choice. This is {product}.", "curiosity"),
]
PROBLEMS_LUXURY = [
    ("I'd grown tired of settling for mediocre products.", "frustration"),
    ("Everything I tried felt mass-produced and uninspired.", "doubt"),
    ("Quality has become so rare that I'd almost given up searching.", "hesitation"),
    ("The market is flooded with average offerings that don't deliver.", "frustration"),
    ("I refused to compromise on quality any longer.", "struggle"),
    ("Nothing I found matched the standard I was looking for.", "doubt"),
    ("I was surrounded by options yet nothing felt truly premium.", "hesitation"),
    ("The gap between marketing promises and actual quality was staggering.", "frustration"),
    ("I'd rather have nothing than settle for something ordinary.", "struggle"),
    ("Every so-called luxury product felt like a dressed-up disappointment.", "doubt"),
    ("Craftsmanship seemed like a forgotten art in today's market.", "hesitation"),
    ("I was beginning to think exceptional quality simply didn't exist anymore.", "doubt"),
]
SOLUTIONS_LUXURY = [
    ("{product} delivers an experience that speaks for itself.", "confidence"),
    ("From the moment I tried {product} I knew this was exceptional.", "amazement"),
    ("{product} is crafted with a level of care you can feel instantly.", "satisfaction"),
    ("The attention to detail in {product} is simply unmatched.", "amazement"),
    ("{product} redefines what quality means in this space.", "transformation"),
    ("Every aspect of {product} exudes sophistication and excellence.", "confidence"),
    ("{product} is what happens when vision meets flawless execution.", "transformation"),
    ("You can feel the craftsmanship in every detail of {product}.", "satisfaction"),
    ("{product} isn't just better, it belongs to an entirely different class.", "confidence"),
    ("The moment {product} entered my life, my standards were permanently raised.", "amazement"),
    ("{product} proves that true quality needs no explanation.", "satisfaction"),
    ("Nothing else comes close once you've experienced {product}.", "transformation"),
]
BRIDGES_LUXURY = [
    ("{product} has become an indispensable part of my lifestyle.", "loyalty"),
    ("Once you experience {product}, there truly is no going back.", "fomo"),
    ("The compliments I receive since discovering {product} are endless.", "social-proof"),
    ("I've recommended {product} to my closest circle and they all agree.", "social-proof"),
    ("{product} is the kind of investment you make once and never regret.", "loyalty"),
    ("It sets a new benchmark that nothing else comes close to.", "amazement"),
    ("Those with discerning taste recognize {product} immediately.", "social-proof"),
    ("{product} is the quiet confidence in my daily ritual.", "loyalty"),
    ("I've curated my life carefully, and {product} earned its permanent place.", "loyalty"),
    ("People notice the difference and they always ask about {product}.", "social-proof"),
    ("Every time I use {product} I'm reminded why I never go back to anything else.", "fomo"),
    ("{product} turned an ordinary routine into something I look forward to.", "transformation"),
]
CTAS_LUXURY = [
    ("Experience {product} for yourself, link in bio.", "urgency"),
    ("Elevate your routine, discover {product} today.", "encouragement"),
    ("Treat yourself to something truly exceptional.", "encouragement"),
    ("The finest things are worth pursuing, get {product} now.", "urgency"),
    ("Join those who refuse to settle, link in bio.", "scarcity"),
    ("Indulge in quality you deserve, try {product}.", "trust"),
    ("Redefine your standard. Discover {product} today.", "encouragement"),
    ("This is your invitation to something extraordinary.", "trust"),
    ("Claim the excellence you've been searching for, link below.", "urgency"),
    ("Step into a world where quality reigns, get {product}.", "scarcity"),
    ("{product} is waiting for those bold enough to demand the best.", "trust"),
    ("Don't just buy a product, invest in {product}.", "encouragement"),
]

# ── Backward-compat aliases (default = funny) ───────────────────────────────
HOOKS = HOOKS_FUNNY
PROBLEMS = PROBLEMS_FUNNY
SOLUTIONS = SOLUTIONS_FUNNY
BRIDGES = BRIDGES_FUNNY
CTAS = CTAS_FUNNY

# Tone → scene-category pools
TONE_TEMPLATES: dict[str, list[tuple[str, list]]] = {
    "funny": [
        ("hook", HOOKS_FUNNY), ("problem", PROBLEMS_FUNNY),
        ("solution", SOLUTIONS_FUNNY), ("bridge", BRIDGES_FUNNY),
        ("cta", CTAS_FUNNY),
    ],
    "emotional": [
        ("hook", HOOKS_EMOTIONAL), ("problem", PROBLEMS_EMOTIONAL),
        ("solution", SOLUTIONS_EMOTIONAL), ("bridge", BRIDGES_EMOTIONAL),
        ("cta", CTAS_EMOTIONAL),
    ],
    "luxury": [
        ("hook", HOOKS_LUXURY), ("problem", PROBLEMS_LUXURY),
        ("solution", SOLUTIONS_LUXURY), ("bridge", BRIDGES_LUXURY),
        ("cta", CTAS_LUXURY),
    ],
}

# Duration weights per scene type (relative, normalised at runtime).
# Hook and CTA are snappier; problem/solution/bridge get more time.
SCENE_WEIGHTS = {
    "hook": 1.0,
    "problem": 1.3,
    "solution": 1.4,
    "bridge": 1.2,
    "cta": 1.0,
}

# Per-scene-type duration bounds (seconds).
SCENE_DURATION_BOUNDS: dict[str, tuple[float, float]] = {
    "hook":     (2.0, 4.5),
    "problem":  (3.0, 6.0),
    "solution": (3.5, 7.0),
    "bridge":   (3.0, 5.5),
    "cta":      (2.0, 4.0),
}

SCENE_CATEGORIES = [
    ("hook",     HOOKS),
    ("problem",  PROBLEMS),
    ("solution", SOLUTIONS),
    ("bridge",   BRIDGES),
    ("cta",      CTAS),
]

VALID_TONES = tuple(TONE_TEMPLATES.keys())

SUPPORTED_LANGUAGES = {
    "english":    "en",
    "spanish":    "es",
    "french":     "fr",
    "german":     "de",
    "hindi":      "hi",
    "portuguese": "pt",
    "italian":    "it",
    "japanese":   "ja",
    "korean":     "ko",
    "arabic":     "ar",
    "chinese":    "zh-CN",
    "russian":    "ru",
    "dutch":      "nl",
    "turkish":    "tr",
    "indonesian": "id",
}
VALID_LANGUAGES = tuple(SUPPORTED_LANGUAGES.keys())

# ── Product-type specific script overlays ────────────────────────────────────
# Each product type has 5 templates per scene category.  When a product type
# is selected, scenes are drawn from *these* templates instead of the generic
# tone-based pool.  {product} is still the placeholder.
PRODUCT_TYPE_TEMPLATES: dict[str, dict[str, list[tuple[str, str]]]] = {
    "skincare": {
        "hook": [
            ("My skin has never looked this good and it's all because of {product}.", "transformation"),
            ("I finally found the skincare holy grail and it's called {product}.", "excitement"),
            ("POV: Your skin is glowing and everyone wants to know your secret. It's {product}.", "attention"),
            ("I tried every serum and cream out there until {product} came along.", "curiosity"),
            ("Dermatologists would hate me for how cheap {product} made great skin.", "surprise"),
        ],
        "problem": [
            ("My skin was so dull and breaking out no matter what I tried.", "frustration"),
            ("I spent hundreds on skincare routines that made my face worse.", "struggle"),
            ("Every morning I dreaded looking in the mirror at my skin.", "doubt"),
            ("Acne, dryness, dark circles, I was dealing with it all at once.", "frustration"),
            ("I was layering ten products a day and seeing zero results.", "hesitation"),
        ],
        "solution": [
            ("{product} cleared my skin in just two weeks flat.", "transformation"),
            ("After one bottle of {product} my pores basically vanished.", "amazement"),
            ("{product} gave me the glow I've been chasing for years.", "satisfaction"),
            ("My skin texture completely transformed thanks to {product}.", "confidence"),
            ("{product} replaced my entire skincare shelf and it works better.", "transformation"),
        ],
        "bridge": [
            ("I wake up and my skin just looks airbrushed every single morning.", "satisfaction"),
            ("People keep asking if I got work done and I just show them {product}.", "social-proof"),
            ("I've been using {product} for three months and my skin keeps improving.", "loyalty"),
            ("My makeup goes on so smooth now because {product} fixed my skin base.", "confidence"),
            ("Even my dermatologist noticed the improvement after I started {product}.", "social-proof"),
        ],
        "cta": [
            ("Your skin deserves {product}, link in bio.", "encouragement"),
            ("Start your glow-up today with {product}, you'll see results fast.", "urgency"),
            ("Grab {product} before your next breakout, trust me.", "scarcity"),
            ("Clear skin is one click away, get {product} now.", "urgency"),
            ("Join the glowing skin club, link below for {product}.", "trust"),
        ],
    },
    "tech": {
        "hook": [
            ("This gadget just made every other tech product I own feel ancient.", "surprise"),
            ("{product} is the tech upgrade you didn't know you needed.", "curiosity"),
            ("I'm a tech nerd and {product} genuinely blew my mind.", "excitement"),
            ("Forget everything you know because {product} just changed the game.", "attention"),
            ("The moment I unboxed {product} I knew tech had leveled up.", "amazement"),
        ],
        "problem": [
            ("I was stuck using clunky outdated tech that slowed me down every day.", "frustration"),
            ("Every gadget I bought was either overpriced or underperformed.", "doubt"),
            ("My old setup was crashing, lagging, and ruining my productivity.", "struggle"),
            ("I wasted money on three different products before finding what actually works.", "frustration"),
            ("Technology was supposed to make life easier but nothing delivered.", "hesitation"),
        ],
        "solution": [
            ("{product} is faster, smarter, and smoother than anything I've used.", "confidence"),
            ("The performance of {product} is on a completely different level.", "amazement"),
            ("{product} does in seconds what my old gear took minutes to do.", "transformation"),
            ("Setup took five minutes and {product} has been flawless ever since.", "satisfaction"),
            ("Battery life, speed, design, {product} nails every single category.", "confidence"),
        ],
        "bridge": [
            ("I've been using {product} daily and it hasn't missed once.", "loyalty"),
            ("My entire workflow is faster now thanks to {product}.", "transformation"),
            ("Friends keep borrowing mine so I told them to just get their own {product}.", "social-proof"),
            ("{product} feels like it was designed specifically for how I work.", "satisfaction"),
            ("I've recommended {product} to ten people and they all came back thanking me.", "social-proof"),
        ],
        "cta": [
            ("Level up your tech game with {product}, link in bio.", "urgency"),
            ("Stop settling for slow, get {product} today.", "encouragement"),
            ("{product} is selling fast, grab yours before they're gone.", "scarcity"),
            ("Click the link and upgrade to {product} right now.", "urgency"),
            ("Best tech purchase of the year, trust me. Get {product}.", "trust"),
        ],
    },
    "fitness": {
        "hook": [
            ("{product} turned my workout game completely upside down.", "transformation"),
            ("I've been in the gym for years but {product} was the missing piece.", "curiosity"),
            ("Okay gym people, you need to hear about {product} right now.", "attention"),
            ("The gains I've made since starting {product} are unreal.", "excitement"),
            ("POV: You finally find the one fitness product that actually delivers. It's {product}.", "surprise"),
        ],
        "problem": [
            ("I was training hard but my results had completely plateaued.", "frustration"),
            ("No matter how many supplements I tried nothing moved the needle.", "doubt"),
            ("I was sore all the time, tired, and not seeing any progress.", "struggle"),
            ("My energy was crashing mid-workout and I couldn't push through.", "hesitation"),
            ("I tried every pre-workout and protein out there with zero difference.", "frustration"),
        ],
        "solution": [
            ("{product} gave me the energy and recovery I was missing.", "transformation"),
            ("My endurance doubled in the first week of using {product}.", "amazement"),
            ("{product} helped me break through a plateau I'd been stuck at for months.", "confidence"),
            ("The soreness vanished and my performance skyrocketed with {product}.", "satisfaction"),
            ("{product} made me feel like I just started training for the first time again.", "transformation"),
        ],
        "bridge": [
            ("My trainer asked what changed and I said two words: {product}.", "social-proof"),
            ("I've hit three personal records since I started using {product}.", "confidence"),
            ("{product} is now the first thing I pack in my gym bag.", "loyalty"),
            ("Gym bros in my circle are all switching to {product} after seeing my results.", "social-proof"),
            ("Recovery time went from two days to half a day thanks to {product}.", "satisfaction"),
        ],
        "cta": [
            ("Stop guessing and start gaining with {product}, link in bio.", "urgency"),
            ("Your best workout is waiting, grab {product} now.", "encouragement"),
            ("{product} is the cheat code your gym routine needs.", "trust"),
            ("Fuel your grind with {product}, get it before it sells out.", "scarcity"),
            ("Click the link and let {product} take your fitness to the next level.", "urgency"),
        ],
    },
    "food": {
        "hook": [
            ("I just found the tastiest thing on the internet and it's called {product}.", "excitement"),
            ("{product} hits different and I need everyone to know.", "attention"),
            ("Foodies, stop what you're doing because {product} is insane.", "surprise"),
            ("I wasn't ready for how good {product} actually tastes.", "amazement"),
            ("This is not a drill, {product} just changed snack time forever.", "curiosity"),
        ],
        "problem": [
            ("Everything I was eating was either bland or loaded with junk.", "frustration"),
            ("I wanted something that tasted amazing and wasn't terrible for me.", "doubt"),
            ("Healthy food always tasted like cardboard to me until now.", "hesitation"),
            ("I kept buying snacks that promised flavor and delivered nothing.", "frustration"),
            ("My taste buds were bored and my body was not happy.", "struggle"),
        ],
        "solution": [
            ("{product} tastes like it shouldn't be this good and yet here we are.", "amazement"),
            ("One bite of {product} and I was hooked, no exaggeration.", "satisfaction"),
            ("{product} proved that healthy and delicious can actually coexist.", "confidence"),
            ("The flavor of {product} is chef's kiss, literally perfect.", "satisfaction"),
            ("{product} replaced every guilty pleasure snack in my pantry.", "transformation"),
        ],
        "bridge": [
            ("I've reordered {product} three times already because it goes fast.", "loyalty"),
            ("My whole family fights over {product} now, it's that good.", "social-proof"),
            ("I bring {product} to every gathering and people lose their minds.", "social-proof"),
            ("{product} is the only snack I never feel guilty about eating.", "satisfaction"),
            ("I tried sharing {product} once and now everyone wants their own.", "fomo"),
        ],
        "cta": [
            ("Taste {product} for yourself, your mouth will thank you. Link in bio.", "encouragement"),
            ("Grab {product} before I eat the entire stock myself.", "scarcity"),
            ("Click the link and treat your taste buds to {product}.", "urgency"),
            ("{product} is the snack upgrade you deserve, get it now.", "trust"),
            ("Life's too short for boring food, try {product} today.", "urgency"),
        ],
    },
    "fashion": {
        "hook": [
            ("I just found the piece that upgraded my entire wardrobe. It's {product}.", "excitement"),
            ("{product} has main character energy and I'm here for it.", "attention"),
            ("If you care about style, you need {product} in your closet.", "curiosity"),
            ("POV: You put on {product} and suddenly everyone is staring.", "surprise"),
            ("{product} is giving luxury vibes without the luxury price tag.", "confidence"),
        ],
        "problem": [
            ("My closet was full but I had nothing that actually made me feel good.", "frustration"),
            ("Every outfit I put together felt basic and uninspired.", "doubt"),
            ("I kept buying trendy pieces that fell apart after two washes.", "frustration"),
            ("Nothing I wore made me feel confident or put-together.", "struggle"),
            ("I was spending too much on clothes that didn't last or look good.", "hesitation"),
        ],
        "solution": [
            ("{product} fits like it was custom made for me.", "satisfaction"),
            ("The quality of {product} is insane for the price point.", "amazement"),
            ("{product} goes with literally everything in my wardrobe.", "confidence"),
            ("I put on {product} and instantly felt like a different person.", "transformation"),
            ("{product} is that one piece that makes every outfit look expensive.", "confidence"),
        ],
        "bridge": [
            ("I've worn {product} five days this week and I'm not even sorry.", "loyalty"),
            ("Three people stopped me on the street to ask about {product}.", "social-proof"),
            ("{product} is the most complimented thing I own now.", "social-proof"),
            ("I bought one and immediately went back for two more colors of {product}.", "loyalty"),
            ("My style game went from basic to iconic all because of {product}.", "transformation"),
        ],
        "cta": [
            ("Upgrade your look with {product}, link is in my bio.", "urgency"),
            ("Don't sleep on {product}, these sell out every drop.", "scarcity"),
            ("Click the link and get {product} before everyone else does.", "fomo"),
            ("Your wardrobe needs {product}, trust me on this one.", "trust"),
            ("Look the part, feel the part. Get {product} today.", "encouragement"),
        ],
    },
}
VALID_PRODUCT_TYPES = tuple(PRODUCT_TYPE_TEMPLATES.keys())


def _allocate_durations(labels: list[str], total: float) -> list[float]:
    """Distribute *total* seconds across scenes by weight, clamped to
    per-scene-type bounds from SCENE_DURATION_BOUNDS."""
    raw = [SCENE_WEIGHTS[l] for l in labels]
    wsum = sum(raw)
    durations = [(w / wsum) * total for w in raw]

    # Iteratively clamp to per-scene bounds and redistribute excess
    for _ in range(5):
        excess = 0.0
        free_weight = 0.0
        for i, d in enumerate(durations):
            lo, hi = SCENE_DURATION_BOUNDS[labels[i]]
            if d < lo:
                excess -= (lo - d)
                durations[i] = lo
            elif d > hi:
                excess += (d - hi)
                durations[i] = hi
            else:
                free_weight += raw[i]
        if abs(excess) < 0.01 or free_weight == 0:
            break
        for i, d in enumerate(durations):
            lo, hi = SCENE_DURATION_BOUNDS[labels[i]]
            if lo < d < hi:
                durations[i] += excess * (raw[i] / free_weight)

    return [round(d, 2) for d in durations]


def generate_scene_plan(
    product_name: str,
    exclude_hooks: set[str] | None = None,
    tone: str = "funny",
    product_type: str = "general",
) -> list[Scene]:
    """Pick one template per category and build a 5-scene plan.

    *exclude_hooks* is a set of hook template strings already used by
    other variations — a different hook will be picked when possible.
    *tone* selects the template bank (funny / emotional / luxury).
    *product_type* selects type-specific templates (skincare / tech / etc.).
    When product_type is not 'general', its templates are used instead
    of the generic tone pool.
    Durations are left at 0.0 — call assign_scene_durations() once the
    voiceover duration is known.
    """
    if exclude_hooks is None:
        exclude_hooks = set()

    # Use product-type templates when available, else fall back to tone pool
    type_pool = PRODUCT_TYPE_TEMPLATES.get(product_type)
    if type_pool:
        categories = [
            (label, type_pool.get(label, pool))
            for label, pool in TONE_TEMPLATES.get(tone, SCENE_CATEGORIES)
        ]
    else:
        categories = TONE_TEMPLATES.get(tone, SCENE_CATEGORIES)
    scenes: list[Scene] = []
    for label, pool in categories:
        if label == "hook" and exclude_hooks:
            # Prefer a hook that hasn't been used yet
            available = [h for h in pool if h[0] not in exclude_hooks]
            if not available:
                available = list(pool)
            template, keyword = random.choice(available)
        else:
            template, keyword = random.choice(pool)
        text = template.format(product=product_name)
        scenes.append(Scene(label=label, text=text, keyword=keyword, duration=0.0))
    return scenes


def assign_scene_durations(scenes: list[Scene], total_duration: float) -> None:
    """Fill each scene's duration proportionally to match *total_duration*."""
    labels = [sc.label for sc in scenes]
    durations = _allocate_durations(labels, total_duration)
    for sc, dur in zip(scenes, durations):
        sc.duration = dur


def print_scene_plan(scenes: list[Scene], header: str = "Scene Plan") -> None:
    """Pretty-print the current scene plan."""
    print(f"\n── {header} " + "─" * max(1, 50 - len(header)))
    for i, sc in enumerate(scenes, 1):
        text_preview = sc.text[:50] + ("…" if len(sc.text) > 50 else "")
        print(f"  Scene {i} [{sc.label:<8}]  {sc.duration:>5.1f}s  "
              f"#{sc.keyword:<15}  {text_preview}")
    print(f"  {'Total':<21}  {sum(s.duration for s in scenes):>5.1f}s")
    print("─" * 53 + "\n")


# ── 1b. Translate scenes ─────────────────────────────────────────────────────
def translate_scenes(scenes: list[Scene], lang_code: str) -> None:
    """Translate every scene's text from English to *lang_code* in-place.

    Uses deep_translator's GoogleTranslator (free, no API key).
    Skips translation when *lang_code* is 'en'.
    """
    if lang_code == "en":
        return
    from deep_translator import GoogleTranslator
    translator = GoogleTranslator(source="en", target=lang_code)
    for sc in scenes:
        sc.text = translator.translate(sc.text)
    print(f"[TRANSLATE] Translated {len(scenes)} scenes → {lang_code}")


# ── 2. Text-to-speech (with cache) ───────────────────────────────────────────
def text_to_speech(script: str, output_path: str, lang: str = "en") -> str:
    """Convert *script* to MP3. Reuses a cached file if the same script was
    generated before (matched by SHA-256 of the text + language)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_key = f"{lang}:{script}"
    script_hash = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
    cached_path = os.path.join(CACHE_DIR, f"{script_hash}.mp3")

    if os.path.isfile(cached_path):
        shutil.copy2(cached_path, output_path)
        print(f"[TTS] Cache hit → {output_path}")
        return output_path

    tts = gTTS(text=script, lang=lang)
    tts.save(output_path)
    shutil.copy2(output_path, cached_path)
    print(f"[TTS] Generated & cached ({lang}) → {output_path}")
    return output_path


def normalize_and_process_voice(input_path: str, output_path: str) -> str:
    """Combine loudness normalisation + voice post-processing in one step.

    1. EBU R128 two-pass loudnorm  (measure → apply)
    2. Speed-up (1.15×–1.25×) — gTTS is slow; this brings pacing
       in line with natural UGC narration speed
    3. Compressor + EQ + pink noise bed

    Previously this was two separate ffmpeg invocations; merging them
    into one saves an entire encode/decode round-trip.
    """
    # ── Pass 1 — measure loudness ────────────────────────────────────
    measure_cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-af", "loudnorm=I=-14:TP=-1.0:LRA=11:print_format=json",
        "-f", "null",
        "-",
    ]
    result = subprocess.run(
        measure_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    stderr_text = result.stderr.decode(errors="replace")

    import json as _json
    json_start = stderr_text.rfind("{")
    json_end = stderr_text.rfind("}") + 1

    # Build loudnorm filter (linear mode if measurement succeeded)
    if json_start != -1 and json_end > 0:
        stats = _json.loads(stderr_text[json_start:json_end])
        loudnorm_f = (
            f"loudnorm=I=-14:TP=-1.0:LRA=11:"
            f"measured_I={stats['input_i']}:"
            f"measured_TP={stats['input_tp']}:"
            f"measured_LRA={stats['input_lra']}:"
            f"measured_thresh={stats['input_thresh']}:"
            f"offset={stats['target_offset']}:"
            f"linear=true"
        )
    else:
        print("[WARN] loudnorm measurement failed — using single-pass")
        loudnorm_f = "loudnorm=I=-14:TP=-1.0:LRA=11"

    # ── Pass 2 — normalise + post-process in one call ────────────────
    speed = round(random.uniform(1.15, 1.25), 3)
    dur = get_duration(input_path)
    noise_dur = dur / speed + 1.0

    af = (
        f"[0:a]"
        f"{loudnorm_f},"
        f"atempo={speed},"
        f"acompressor=threshold=-20dB:ratio=6:attack=3:release=80:makeup=3,"
        f"highpass=f=80,"
        f"equalizer=f=2800:t=q:w=1.0:g=3,"
        f"equalizer=f=5500:t=q:w=0.8:g=4,"
        f"equalizer=f=8000:t=q:w=0.6:g=2"
        f"[voice];"
        f"anoisesrc=d={noise_dur:.1f}:c=pink:a=0.004[noise];"
        f"[voice][noise]amix=inputs=2:duration=first:dropout_transition=0[aout]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-filter_complex", af,
        "-map", "[aout]",
        "-ar", "44100",
        "-c:a", "aac",
        "-b:a", "256k",
        output_path,
    ]
    print(f"[VOICE] Normalise + post-process (speed={speed}×, compressor, EQ, ambient noise)")
    run_ffmpeg(cmd, "post-process voice")
    print(f"[VOICE] Processed → {output_path}")
    return output_path


def _find_bgm() -> str | None:
    """Return the path to a random BGM file in assets/bgm/, or None."""
    if not os.path.isdir(BGM_DIR):
        return None
    tracks = [
        os.path.join(BGM_DIR, f)
        for f in os.listdir(BGM_DIR)
        if os.path.splitext(f)[1].lower() in {".mp3", ".wav", ".aac", ".m4a", ".ogg"}
    ]
    return random.choice(tracks) if tracks else None


# ── 3. Keyword-based clip selection ──────────────────────────────────────────
# Fallback chain: if a keyword folder is empty, try related keywords,
# then fall back to "general", then to any clip in the entire clips tree.

KEYWORD_FALLBACKS: dict[str, list[str]] = {
    # hook keywords
    "surprise":       ["excitement", "amazement", "attention", "curiosity"],
    "attention":      ["curiosity", "excitement", "surprise", "urgency"],
    "curiosity":      ["attention", "surprise", "doubt", "excitement"],
    "excitement":     ["surprise", "amazement", "transformation", "attention"],
    # problem keywords
    "frustration":    ["struggle", "doubt", "hesitation", "regret"],
    "doubt":          ["hesitation", "frustration", "struggle", "regret"],
    "hesitation":     ["doubt", "frustration", "struggle", "regret"],
    "struggle":       ["frustration", "doubt", "hesitation", "exhaustion"],
    # solution keywords
    "transformation": ["amazement", "confidence", "satisfaction", "excitement"],
    "amazement":      ["surprise", "transformation", "excitement", "confidence"],
    "confidence":     ["satisfaction", "transformation", "trust", "amazement"],
    "satisfaction":   ["confidence", "loyalty", "transformation", "trust"],
    # bridge keywords
    "loyalty":        ["satisfaction", "confidence", "trust", "social-proof"],
    "regret":         ["doubt", "hesitation", "fomo", "frustration"],
    "fomo":           ["urgency", "scarcity", "regret", "excitement"],
    "social-proof":   ["confidence", "trust", "loyalty", "satisfaction"],
    # cta keywords
    "urgency":        ["scarcity", "fomo", "excitement", "attention"],
    "trust":          ["confidence", "encouragement", "loyalty", "satisfaction"],
    "encouragement":  ["trust", "confidence", "satisfaction", "transformation"],
    "scarcity":       ["urgency", "fomo", "excitement", "attention"],
    "exhaustion":     ["struggle", "frustration", "doubt", "hesitation"],
}


def _list_clips_in(folder: str) -> list[str]:
    """Return all video file paths directly inside *folder*."""
    if not os.path.isdir(folder):
        return []
    return [
        os.path.join(folder, f)
        for f in os.listdir(folder)
        if os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS
    ]


def _collect_all_clips(clips_dir: str) -> list[str]:
    """Recursively collect every video file under *clips_dir*."""
    all_files: list[str] = []
    for root, _dirs, files in os.walk(clips_dir):
        for f in files:
            if os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS:
                all_files.append(os.path.join(root, f))
    return all_files


def _ensure_generated_fallback_clips(clips_dir: str) -> list[str]:
    """Create simple bundled-safe clips when the deployed image has none."""
    existing = _collect_all_clips(clips_dir)
    if existing:
        return existing

    out_dir = os.path.join(clips_dir, "_generated")
    os.makedirs(out_dir, exist_ok=True)

    palettes = [
        ("0x07151d", "0x123241", "0x1f7a77"),
        ("0x10151c", "0x28384a", "0xb38432"),
        ("0x16131b", "0x322b46", "0x8f5cff"),
        ("0x0c1712", "0x263f33", "0x5ccf8f"),
        ("0x1b1510", "0x3c3026", "0xff9b4a"),
        ("0x08161f", "0x1b3647", "0x63c7e6"),
    ]

    for idx, (base, band, accent) in enumerate(palettes, start=1):
        path = os.path.join(out_dir, f"fallback_{idx:02d}.mp4")
        if os.path.isfile(path) and os.path.getsize(path) > 10_000:
            continue

        vf = ",".join([
            f"drawbox=x=0:y=0:w=iw:h=ih:color={base}:t=fill",
            f"drawbox=x=0:y=ih*0.50:w=iw:h=ih*0.50:color={band}:t=fill",
            f"drawbox=x=iw*0.06:y=ih*0.10:w=iw*0.88:h=ih*0.20:color={accent}:t=14",
            f"drawbox=x=iw*0.12:y=ih*0.64:w=iw*0.76:h=ih*0.16:color={accent}:t=10",
            "noise=alls=6:allf=t+u",
            "format=yuv420p",
        ])
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i",
            f"color=c={base}:s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:d=8:r={VIDEO_FPS}",
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
            "-pix_fmt", "yuv420p",
            path,
        ]
        subprocess.run(cmd, check=True)

    generated = _collect_all_clips(out_dir)
    if generated:
        print(f"[CLIPS] Generated {len(generated)} fallback clips in {out_dir}")
    return generated


def _pick_clip_for_keyword(
    clips_dir: str,
    keyword: str,
    exclude: set[str] | None = None,
    prefer_best: bool = False,
) -> tuple[str, str]:
    """Pick one random clip matching *keyword*, walking the fallback chain.

    Clips whose absolute path is in *exclude* are deprioritised (used only
    when no alternative exists).

    When *prefer_best* is True the highest-quality clip (by resolution and
    bitrate) is chosen instead of a random one — used for hook scenes.

    Returns (clip_path, fallback_level) where fallback_level is one of
    'exact', 'related:<kw>', 'general', 'any'.
    """
    if exclude is None:
        exclude = set()

    def _choose(pool: list[str]) -> str | None:
        preferred = [p for p in pool if p not in exclude]
        if preferred:
            return random.choice(preferred)
        return random.choice(pool) if pool else None

    def _choose_best(pool: list[str]) -> str | None:
        """Pick the highest-quality clip from *pool* (by resolution × bitrate)."""
        preferred = [p for p in pool if p not in exclude] or pool
        if not preferred:
            return None
        if len(preferred) == 1:
            return preferred[0]
        scored: list[tuple[float, str]] = []
        for p in preferred:
            try:
                probe = subprocess.run(
                    ["ffprobe", "-v", "error",
                     "-select_streams", "v:0",
                     "-show_entries", "stream=width,height,bit_rate",
                     "-of", "default=noprint_wrappers=1:nokey=1", p],
                    capture_output=True, text=True,
                )
                vals = probe.stdout.strip().splitlines()
                w = int(vals[0]) if len(vals) > 0 and vals[0].isdigit() else 0
                h = int(vals[1]) if len(vals) > 1 and vals[1].isdigit() else 0
                br = int(vals[2]) if len(vals) > 2 and vals[2].isdigit() else 0
                score = (w * h) + br / 1000
            except Exception:
                score = 0.0
            scored.append((score, p))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]

    picker = _choose_best if prefer_best else _choose

    # 1. Try the exact keyword folder
    hit = picker(_list_clips_in(os.path.join(clips_dir, keyword)))
    if hit:
        return hit, "exact"

    # 2. Try each fallback keyword in order
    for fb in KEYWORD_FALLBACKS.get(keyword, []):
        hit = picker(_list_clips_in(os.path.join(clips_dir, fb)))
        if hit:
            return hit, f"related:{fb}"

    # 3. Try the general folder
    hit = picker(_list_clips_in(os.path.join(clips_dir, "general")))
    if hit:
        return hit, "general"

    # 4. Last resort — any clip anywhere under clips_dir
    hit = picker(_collect_all_clips(clips_dir))
    if hit:
        return hit, "any"

    hit = picker(_ensure_generated_fallback_clips(clips_dir))
    if hit:
        return hit, "generated"

    raise RuntimeError(f"No video clips found anywhere in {clips_dir}")


def select_clips_for_scenes(
    clips_dir: str,
    scenes: list[Scene],
    exclude_clips: set[str] | None = None,
) -> list[str]:
    """Return one clip per scene, chosen by keyword with fallback.

    Clips in *exclude_clips* are avoided when alternatives exist.
    """
    if exclude_clips is None:
        exclude_clips = set()
    chosen: list[str] = []
    for sc in scenes:
        is_hook = sc.label == "hook"
        clip, level = _pick_clip_for_keyword(
            clips_dir, sc.keyword, exclude=exclude_clips,
            prefer_best=is_hook,
        )
        chosen.append(clip)
        exclude_clips.add(clip)       # also avoid reuse within this variation
        tag = " ★best" if is_hook else ""
        print(f"  [{sc.label:<8}]  #{sc.keyword:<15}  → "
              f"{os.path.basename(clip)}  ({level}{tag})")
    print(f"[CLIPS] Selected {len(chosen)} clips by keyword")

    # Mild shuffle: randomly swap adjacent non-hook clips so each
    # variation feels different even with the same keyword matches.
    if len(chosen) > 2:
        indices = list(range(1, len(chosen)))  # skip index 0 (hook)
        random.shuffle(indices)
        # Perform up to 2 random adjacent swaps
        swaps = min(2, len(indices) - 1)
        for s in range(swaps):
            i = indices[s]
            j = i + 1 if i + 1 < len(chosen) else i - 1
            if j == 0:
                continue  # don't touch hook
            chosen[i], chosen[j] = chosen[j], chosen[i]
            print(f"  [shuffle] swapped scene {i+1} ↔ {j+1}")

    return chosen


# ── 4. Pre-trim clips to exact scene durations ─────────────────────────────
def trim_clip(
    clip_path: str,
    duration: float,
    output_path: str,
    scene_label: str = "bridge",
    extra_offset: float = 0.0,
) -> str:
    """Cut *clip_path* to exactly *duration* seconds and scale+pad to the
    target resolution (1080×1920).  A random start offset keeps excerpts
    varied.  Short clips are looped first so there are always enough frames.

    *extra_offset* adds a guaranteed additional seek (0–1 s) on top of the
    random start position, ensuring two trims of the same clip look different.

    Every clip gets a subtle motion effect (slow zoom with randomised
    direction + horizontal pan).  Hook clips get a stronger zoom
    (1.05×→1.1×).  A per-scene colour filter from SCENE_COLOR_FILTERS
    is applied on top (contrast, saturation, warmth, etc.).
    """
    is_hook = scene_label == "hook"
    src_dur = get_duration(clip_path)
    total_frames = max(1, int(duration * VIDEO_FPS))

    # ── Build zoompan expression ────────────────────────────────────
    if is_hook:
        # Hook: stronger zoom-in  1.05× → 1.1×, centred
        z_expr = f"1.05+0.05*on/{total_frames}"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"
    else:
        # Other clips: gentle zoom 1.0 → 1.08 (or reverse), with a
        # slight horizontal drift (up to ~3 % of width).
        zoom_in = random.choice([True, False])
        if zoom_in:
            z_expr = f"1.0+0.08*on/{total_frames}"
        else:
            z_expr = f"1.08-0.08*on/{total_frames}"

        # Randomise pan direction: drift left→right or right→left
        pan_dir = random.choice([-1, 1])
        # max x-offset ≈ 3 % of input width at current zoom
        x_expr = (
            f"iw/2-(iw/zoom/2)"
            f"+{pan_dir}*0.03*(iw/zoom)*on/{total_frames}"
        )
        y_expr = "ih/2-(ih/zoom/2)"

    zoom_vf = (
        f"zoompan=z='{z_expr}':"
        f"d={total_frames}:"
        f"x='{x_expr}':y='{y_expr}':"
        f"s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={VIDEO_FPS}"
    )

    if is_hook:
        # Brightness +0.05, contrast 1.1 for a punchier opener
        vf = f"{zoom_vf},eq=brightness=0.05:contrast=1.1"
    else:
        vf = zoom_vf

    # Apply per-scene colour grading
    color_filter = SCENE_COLOR_FILTERS.get(scene_label, "")
    if color_filter:
        vf = f"{vf},{color_filter}"

    if src_dur >= duration:
        # Enough material — pick a random start point + extra offset
        max_start = src_dur - duration
        base_start = random.uniform(0, max(0, max_start - extra_offset))
        start = min(base_start + extra_offset, max_start)
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}",
            "-i", clip_path,
            "-t", f"{duration:.3f}",
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-an",
            "-pix_fmt", "yuv420p",
            output_path,
        ]
    else:
        # Clip is shorter than needed — loop then trim
        loops = int(duration // src_dur) + 1
        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", str(loops),
            "-i", clip_path,
            "-t", f"{duration:.3f}",
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-an",
            "-pix_fmt", "yuv420p",
            output_path,
        ]

    run_ffmpeg(cmd, f"trim {os.path.basename(clip_path)}")
    return output_path


def trim_clips_to_scenes(
    clips: list[str],
    scenes: list[Scene],
    output_dir: str,
) -> list[str]:
    """Pre-trim each clip to its scene's exact duration **in parallel**.

    Returns a list of paths to the trimmed intermediate files.
    """
    os.makedirs(output_dir, exist_ok=True)
    print("[TRIM] Trimming clips to scene durations (parallel) …")

    out_paths = [os.path.join(output_dir, f"trimmed_{i}.mp4")
                 for i in range(len(clips))]
    offsets = [random.uniform(0.0, 1.0) for _ in clips]

    def _trim_one(i: int) -> str:
        trim_clip(clips[i], scenes[i].duration, out_paths[i],
                  scene_label=scenes[i].label, extra_offset=offsets[i])
        return out_paths[i]

    with ThreadPoolExecutor(max_workers=min(len(clips), os.cpu_count() or 4)) as pool:
        list(pool.map(_trim_one, range(len(clips))))

    for i, (clip, sc) in enumerate(zip(clips, scenes)):
        actual = get_duration(out_paths[i])
        print(f"  Scene {i+1} [{sc.label:<8}]  "
              f"target={sc.duration:.2f}s  actual={actual:.2f}s  "
              f"← {os.path.basename(clip)}")
    print(f"[TRIM] All {len(out_paths)} clips trimmed")
    return out_paths


# ── 5. Merge pre-trimmed clips (xfade transitions) ──────────────────────────
def merge_clips(
    trimmed_clips: list[str],
    output_path: str,
    known_durations: list[float] | None = None,
) -> str:
    """Merge pre-trimmed, pre-scaled clips with varied xfade transitions
    (randomised type and duration per cut) and output a single video."""
    n = len(trimmed_clips)

    input_args: list[str] = []
    for clip in trimmed_clips:
        input_args.extend(["-i", clip])

    # Clips are already scaled to target resolution by trim_clip();
    # just normalise fps for a clean xfade.
    filter_parts: list[str] = []
    clip_durations: list[float] = []
    for i in range(n):
        dur = known_durations[i] if known_durations else get_duration(trimmed_clips[i])
        clip_durations.append(dur)
        filter_parts.append(f"[{i}:v]fps={VIDEO_FPS}[v{i}]")

    # Build the xfade chain — each cut gets a random type + random duration
    if n == 1:
        final_label = "v0"
    else:
        transitions = random.choices(TRANSITION_TYPES, k=n - 1)
        trans_durs = [
            round(random.uniform(TRANSITION_DUR_MIN, TRANSITION_DUR_MAX), 2)
            for _ in range(n - 1)
        ]
        prev_label = "v0"
        cumulative = clip_durations[0]
        for i in range(n - 1):
            t = trans_durs[i]
            offset = cumulative - t
            out_label = "vout" if i == n - 2 else f"x{i}"
            filter_parts.append(
                f"[{prev_label}][v{i + 1}]xfade="
                f"transition={transitions[i]}:"
                f"duration={t}:offset={offset:.3f}[{out_label}]"
            )
            prev_label = out_label
            cumulative += clip_durations[i + 1] - t
            print(f"  cut {i+1}: {transitions[i]:<14} {t}s")
        final_label = "vout"

    filter_str = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *input_args,
        "-filter_complex", filter_str,
        "-map", f"[{final_label}]",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        output_path,
    ]
    print("[MERGE] Merging clips with transitions …")
    run_ffmpeg(cmd, "merge clips")
    print(f"[MERGE] Merged video → {output_path}")
    return output_path


# ── 6. Subtitle generation (scene-aware) ────────────────────────────────────

# Alternating highlight colours for the keyword in each chunk (ASS &HAABBGGRR)
_HIGHLIGHT_COLOURS = [
    "&H0000FFFF",   # yellow  (BGR: 00FFFF)
    "&H00FFFF00",   # cyan    (BGR: FFFF00)
    "&H005EF0FF",   # orange  (BGR: 5EF0FF)
    "&H00FF78FF",   # pink    (BGR: FF78FF)
]

# Common short/filler words that should NOT be highlighted
_STOP_WORDS = frozenset(
    "i a an the is am are was were be been being do does did "
    "have has had having will would shall should may might can could "
    "to of in for on with at by from it its it's and or but not "
    "no so if my me we us he she him her they them this that "
    "just very really also too much many some any all each every "
    "up out about then than into over after before".split()
)


def _format_ass_time(seconds: float) -> str:
    """Format seconds → ASS timestamp  H:MM:SS.CC"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = min(99, int(round((seconds % 1) * 100)))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _chunk_words(text: str, size_min: int = 2, size_max: int = 4) -> list[str]:
    """Split *text* into chunks of *size_min*–*size_max* words."""
    words = text.split()
    chunks: list[str] = []
    i = 0
    while i < len(words):
        remaining = len(words) - i
        if remaining <= size_max:
            size = remaining
        else:
            size = random.randint(size_min, size_max)
        chunks.append(" ".join(words[i : i + size]))
        i += size
    return chunks


def _pick_keyword(chunk: str) -> str | None:
    """Return the most 'important' word in *chunk* for highlighting.

    Picks the longest non-stop-word.  Returns None if no good candidate.
    """
    words = chunk.split()
    candidates = [
        w for w in words
        if w.lower().strip(".,!?'\"") not in _STOP_WORDS and len(w) > 1
    ]
    if not candidates:
        return None
    # Longest word is a reasonable proxy for importance
    return max(candidates, key=len)


def _ass_escape(text: str) -> str:
    """Escape special ASS characters in *text*."""
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def generate_subtitles(scenes: list[Scene], output_path: str) -> str:
    """Create an ASS subtitle file with bold, centred 2–4 word chunks.

    Enhancements:
    • One keyword per chunk is highlighted in a cycling accent colour.
    • Each chunk uses a pop-in scale animation (120 % → 100 %).
    • Alternating highlight colours give visual rhythm.

    Chunk timing respects scene boundaries and is weighted by character
    length so longer phrases get more reading time.
    """
    all_chunks: list[tuple[float, float, str]] = []
    cursor = 0.0
    for sc in scenes:
        chunks = _chunk_words(sc.text)
        # Weight by character length for more natural reading rhythm
        lengths = [max(len(c), 1) for c in chunks]
        total_len = sum(lengths)
        cursor_inner = cursor
        for j, chunk in enumerate(chunks):
            chunk_dur = (lengths[j] / total_len) * sc.duration
            start = cursor_inner
            end = start + chunk_dur
            all_chunks.append((start, end, chunk))
            cursor_inner = end
        cursor += sc.duration

    ass_header = (
        "[Script Info]\n"
        "Title: VeeGen Subtitles\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {VIDEO_WIDTH}\n"
        f"PlayResY: {VIDEO_HEIGHT}\n"
        "WrapStyle: 0\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,90,&H00FFFFFF,&H000000FF,&H00000000,"
        "&HA0000000,-1,0,0,0,100,100,2,0,3,6,3,5,50,50,80,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
    )

    lines: list[str] = []
    colour_idx = 0
    for start, end, chunk in all_chunks:
        words = chunk.split()
        keyword = _pick_keyword(chunk)
        colour = _HIGHLIGHT_COLOURS[colour_idx % len(_HIGHLIGHT_COLOURS)]

        # Bounce-in: start at 130% scale, ease to 95% then settle at 100%
        pop_dur = min(180, int((end - start) * 500))  # ms, cap at half duration
        settle_dur = min(80, pop_dur // 2)
        pop_tag = (
            "{\\fscx130\\fscy130"
            f"\\t(0,{pop_dur},\\fscx95\\fscy95)"
            f"\\t({pop_dur},{pop_dur + settle_dur},\\fscx100\\fscy100)"
            "}"
        )

        # Build the text with the keyword highlighted
        styled_words: list[str] = []
        for w in words:
            safe = _ass_escape(w).upper()
            if keyword and w.upper() == keyword.upper():
                # Highlight: accent colour + slightly larger
                styled_words.append(
                    f"{{\\c{colour}\\fscx110\\fscy110}}"
                    f"{safe}"
                    "{\\c&H00FFFFFF&\\fscx100\\fscy100}"
                )
            else:
                styled_words.append(safe)

        text_body = " ".join(styled_words)
        lines.append(
            f"Dialogue: 0,{_format_ass_time(start)},{_format_ass_time(end)},"
            f"Default,,0,0,0,,{pop_tag}{text_body}"
        )
        if keyword:
            colour_idx += 1

    with open(output_path, "w", encoding="utf-8-sig") as fh:
        fh.write(ass_header)
        fh.write("\n".join(lines))
        fh.write("\n")

    print(f"[SUBS] Generated {len(all_chunks)} subtitle chunks → {output_path}")
    return output_path


# ── 7. Add voiceover + optional BGM + burn subtitles ─────────────────────────
def add_voiceover(
    video_path: str,
    audio_path: str,
    subtitle_path: str,
    output_path: str,
    bgm_path: str | None = None,
) -> str:
    """Mux normalised voiceover (+ optional BGM), burn subtitles, produce
    the final 1080×1920 MP4."""
    ass_escaped = subtitle_path.replace("\\", "/").replace(":", "\\:")

    input_args = ["-i", video_path, "-i", audio_path]

    if bgm_path:
        input_args.extend(["-i", bgm_path])
        # voice = input 1, bgm = input 2 — mix with volume control
        af = (
            f"[1:a]aresample=44100[voice];"
            f"[2:a]aresample=44100,volume={BGM_VOLUME}[bgm];"
            f"[voice][bgm]amix=inputs=2:duration=first:dropout_transition=2[aout]"
        )
        map_audio = ["-map", "[aout]"]
        filter_complex_audio = ["-filter_complex", af]
    else:
        map_audio = ["-map", "1:a"]
        filter_complex_audio = []

    cmd = [
        "ffmpeg", "-y",
        *input_args,
        *filter_complex_audio,
        "-vf", f"ass='{ass_escaped}'",
        "-map", "0:v",
        *map_audio,
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-s", f"{VIDEO_WIDTH}x{VIDEO_HEIGHT}",
        "-c:a", "aac",
        "-b:a", "256k",
        "-shortest",
        "-movflags", "+faststart",
        output_path,
    ]
    bgm_tag = " + BGM" if bgm_path else ""
    print(f"[FINAL] Adding voiceover{bgm_tag} + subtitles …")
    run_ffmpeg(cmd, "add voiceover + subtitles")
    print(f"[FINAL] Final video → {output_path}")
    return output_path


# ── Main pipeline ────────────────────────────────────────────────────────────
def build_one_video(
    product_name: str,
    variant: int,
    output_path: str,
    exclude_hooks: set[str],
    exclude_clips: set[str],
    tone: str = "funny",
    lang: str = "en",
    product_type: str = "general",
) -> tuple[str, set[str], set[str]]:
    """Build a single video variation.

    Returns (output_path, updated_exclude_hooks, updated_exclude_clips).
    """
    tag = f"[V{variant}]"
    print(f"\n{'═' * 55}")
    print(f"  {tag}  Building variation {variant} of {NUM_VARIATIONS}")
    print(f"{'═' * 55}")

    var_dir = os.path.join(OUTPUT_DIR, f"_var{variant}")
    os.makedirs(var_dir, exist_ok=True)

    voiceover_path = os.path.join(var_dir, "voiceover.mp3")
    voiceover_post = os.path.join(var_dir, "voiceover_post.m4a")
    merged_video_path = os.path.join(var_dir, "merged_no_audio.mp4")
    trim_dir = os.path.join(var_dir, "trimmed")

    # Step 1 — Build scene plan (different hook each variation)
    scenes = generate_scene_plan(product_name, exclude_hooks=exclude_hooks,
                                   tone=tone, product_type=product_type)
    hook_template = next(
        (t for t, _kw in HOOKS if t.format(product=product_name) == scenes[0].text),
        scenes[0].text,
    )
    exclude_hooks.add(hook_template)

    # Step 1b — Translate scenes (if non-English)
    translate_scenes(scenes, lang)

    script_text = "\n".join(sc.text for sc in scenes)
    print_scene_plan(scenes, f"{tag} Script (durations pending)")

    # Step 2 — Text-to-speech
    text_to_speech(script_text, voiceover_path, lang=lang)

    # Step 3 — Normalize + post-process voice in one pass
    normalize_and_process_voice(voiceover_path, voiceover_post)

    # Step 4 — Measure voice & assign per-scene durations
    #   Add transition-overlap time so the merged video is long enough
    #   for the full voiceover (xfade eats time from adjacent clips).
    voice_dur = get_duration(voiceover_post)
    n_trans = len(scenes) - 1
    avg_trans = (TRANSITION_DUR_MIN + TRANSITION_DUR_MAX) / 2
    transition_pad = n_trans * avg_trans
    print(f"{tag} Voiceover duration: {voice_dur:.1f}s  (adding {transition_pad:.1f}s for transitions)")
    assign_scene_durations(scenes, voice_dur + transition_pad)
    print_scene_plan(scenes, f"{tag} Final Scene Timing")

    # Step 5 — Select clips by keyword (different from other variations)
    clips = select_clips_for_scenes(CLIPS_DIR, scenes, exclude_clips=exclude_clips)
    for c in clips:
        exclude_clips.add(c)

    # Step 6 — Pre-trim each clip to exact scene duration (parallel)
    trimmed = trim_clips_to_scenes(clips, scenes, trim_dir)

    # Step 7 — Merge trimmed clips with transitions (pass known durations)
    scene_durs = [sc.duration for sc in scenes]
    merge_clips(trimmed, merged_video_path, known_durations=scene_durs)

    # Step 8 — Generate subtitles (scene-aware timing)
    subtitle_path = os.path.join(var_dir, "subtitles.ass")
    generate_subtitles(scenes, subtitle_path)

    # Step 9 — Find optional background music
    bgm = _find_bgm()
    if bgm:
        print(f"{tag} BGM: {os.path.basename(bgm)}")
    else:
        print(f"{tag} No BGM tracks found — skipping")

    # Step 10 — Add voiceover + optional BGM + burn subtitles
    add_voiceover(
        merged_video_path, voiceover_post, subtitle_path, output_path,
        bgm_path=bgm,
    )

    # Clean up variation intermediates
    if os.path.isdir(var_dir):
        shutil.rmtree(var_dir)

    final_dur = get_duration(output_path)
    print(f"\n✅  {tag} Variation {variant} ({final_dur:.1f}s) → {output_path}")
    return output_path, exclude_hooks, exclude_clips


def main() -> None:
    check_ffmpeg()

    product_name = input("Enter the product name: ").strip()
    if not product_name:
        print("[ERROR] Product name cannot be empty.")
        sys.exit(1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    exclude_hooks: set[str] = set()
    exclude_clips: set[str] = set()

    outputs: list[str] = []
    for v in range(1, NUM_VARIATIONS + 1):
        out_name = f"final_v{v}.mp4" if NUM_VARIATIONS > 1 else "final.mp4"
        out_path = os.path.join(OUTPUT_DIR, out_name)
        build_one_video(
            product_name, v, out_path,
            exclude_hooks, exclude_clips,
        )
        outputs.append(out_path)

    print(f"\n{'═' * 55}")
    print(f"  All {NUM_VARIATIONS} variations complete!")
    for i, p in enumerate(outputs, 1):
        dur = get_duration(p)
        print(f"    V{i}: {p}  ({dur:.1f}s)")
    print(f"{'═' * 55}")


def create_video(product_name: str, tone: str = "funny", language: str = "english", product_type: str = "general") -> str:
    """Programmatic entry-point used by the web UI.

    Generates a single video variation and returns the absolute path
    to the output .mp4 file.
    """
    check_ffmpeg()
    tone = tone.lower().strip()
    if tone not in VALID_TONES:
        tone = "funny"
    language = language.lower().strip()
    lang_code = SUPPORTED_LANGUAGES.get(language, "en")
    product_type = product_type.lower().strip()
    if product_type not in PRODUCT_TYPE_TEMPLATES and product_type != "general":
        product_type = "general"
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    import uuid
    video_id = uuid.uuid4().hex[:8]
    out_name = f"{product_name.replace(' ', '_')}_{tone}_{language}_{video_id}.mp4"
    out_path = os.path.join(OUTPUT_DIR, out_name)

    build_one_video(
        product_name, 1, out_path,
        exclude_hooks=set(), exclude_clips=set(),
        tone=tone,
        lang=lang_code,
        product_type=product_type,
    )
    return out_path


if __name__ == "__main__":
    main()

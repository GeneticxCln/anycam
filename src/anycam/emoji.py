"""The bundled emoji pack: a curated subset of Google's Noto emoji (Apache-2.0).

The pictures are real, colourful artwork, so frames, falling animations and face
props can use them instead of simple shapes.  ``scripts/fetch-emoji.py`` rebuilds
``assets/emoji/`` from the pinned upstream commit; see ``assets/emoji/NOTICE.md``.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import Image

from . import config

# (codepoint(s), name, category, extra search words)
CATALOGUE: tuple[tuple[str, str, str, str], ...] = (
    # --- faces ---------------------------------------------------------------
    ("1f600", "Grin", "Faces", "happy smile"), ("1f602", "Joy", "Faces", "laugh lol tears"),
    ("1f60d", "Heart eyes", "Faces", "love"), ("1f60e", "Cool", "Faces", "sunglasses"),
    ("1f929", "Star-struck", "Faces", "wow amazed"), ("1f973", "Party face", "Faces", "celebrate"),
    ("1f914", "Thinking", "Faces", "hmm"), ("1f631", "Scream", "Faces", "scared shock"),
    ("1f608", "Devil", "Faces", "evil purple"), ("1f921", "Clown", "Faces", "funny"),
    ("1f47b", "Ghost", "Faces", "halloween boo"), ("1f480", "Skull", "Faces", "halloween dead"),
    ("1f47d", "Alien", "Faces", "space ufo"), ("1f916", "Robot", "Faces", "cyber machine"),
    ("1f4a9", "Poo", "Faces", "funny"), ("1f975", "Hot face", "Faces", "sweat heat"),
    ("1f976", "Cold face", "Faces", "freeze ice"), ("1f970", "Smitten", "Faces", "love hearts"),
    ("1f60b", "Yum", "Faces", "tongue tasty"), ("1f92f", "Mind blown", "Faces", "explode"),
    # --- hearts --------------------------------------------------------------
    ("2764", "Red heart", "Hearts", "love valentine"), ("1f9e1", "Orange heart", "Hearts", ""),
    ("1f49b", "Yellow heart", "Hearts", ""), ("1f49a", "Green heart", "Hearts", ""),
    ("1f499", "Blue heart", "Hearts", ""), ("1f49c", "Purple heart", "Hearts", ""),
    ("1f90d", "White heart", "Hearts", ""), ("1f5a4", "Black heart", "Hearts", ""),
    ("1f496", "Sparkling heart", "Hearts", "love"), ("1f498", "Heart arrow", "Hearts", "cupid"),
    ("1f495", "Two hearts", "Hearts", "love"), ("1f494", "Broken heart", "Hearts", "sad"),
    ("1f48c", "Love letter", "Hearts", "valentine mail"), ("1f339", "Rose", "Hearts", "flower love"),
    # --- sparkle and weather ---------------------------------------------------
    ("2728", "Sparkles", "Sparkle", "magic glitter"), ("2b50", "Star", "Sparkle", "gold"),
    ("1f31f", "Glowing star", "Sparkle", "shine"), ("1f4ab", "Dizzy star", "Sparkle", "swirl"),
    ("1f320", "Shooting star", "Sparkle", "wish night"), ("1f4a5", "Boom", "Sparkle", "explosion bang"),
    ("1f525", "Fire", "Sparkle", "hot flame"), ("26a1", "Lightning", "Sparkle", "zap electric"),
    ("1f308", "Rainbow", "Sparkle", "pride colour"), ("1f4a7", "Droplet", "Sparkle", "water"),
    ("2744", "Snowflake", "Sparkle", "winter snow ice"), ("1f4a8", "Dash", "Sparkle", "speed wind"),
    ("2600", "Sun", "Sparkle", "sunny day"), ("1f319", "Crescent moon", "Sparkle", "night"),
    ("2601", "Cloud", "Sparkle", "sky"), ("1f30a", "Wave", "Sparkle", "ocean sea"),
    # --- party ---------------------------------------------------------------
    ("1f389", "Party popper", "Party", "celebrate confetti"), ("1f38a", "Confetti ball", "Party", ""),
    ("1f388", "Balloon", "Party", "birthday"), ("1f381", "Gift", "Party", "present"),
    ("1f382", "Cake", "Party", "birthday"), ("1f380", "Ribbon", "Party", "bow"),
    ("1f386", "Fireworks", "Party", "new year"), ("1f387", "Sparkler", "Party", ""),
    ("1f3c6", "Trophy", "Party", "win champion"), ("1f451", "Crown", "Party", "king queen royal"),
    ("1f947", "Gold medal", "Party", "win first"), ("1f37e", "Champagne", "Party", "toast"),
    ("1f942", "Cheers", "Party", "drink toast"), ("1f3a9", "Top hat", "Party", "magic fancy"),
    # --- christmas and winter ----------------------------------------------------
    ("1f384", "Christmas tree", "Christmas", "xmas holiday"), ("1f385", "Santa", "Christmas", "xmas"),
    ("1f936", "Mrs Claus", "Christmas", "xmas"), ("26c4", "Snowman", "Christmas", "winter"),
    ("2603", "Snowman in snow", "Christmas", "winter"), ("1f98c", "Reindeer", "Christmas", "deer xmas"),
    ("1f514", "Bell", "Christmas", "jingle"), ("1f56f", "Candle", "Christmas", "light"),
    ("1f9e6", "Socks", "Christmas", "stocking"), ("1f9e3", "Scarf", "Christmas", "winter"),
    ("1f36c", "Candy", "Christmas", "sweet"), ("1f36d", "Lollipop", "Christmas", "sweet"),
    ("1f9ca", "Ice cube", "Christmas", "cold"),
    # --- halloween ---------------------------------------------------------------
    ("1f383", "Pumpkin", "Halloween", "jack-o-lantern"), ("1f987", "Bat", "Halloween", ""),
    ("1f577", "Spider", "Halloween", "insect"), ("1f578", "Spider web", "Halloween", "cobweb"),
    ("1f9db", "Vampire", "Halloween", "dracula"), ("1f9df", "Zombie", "Halloween", ""),
    ("1f9d9", "Mage", "Halloween", "wizard witch"), ("1f52e", "Crystal ball", "Halloween", "magic"),
    ("1f9de", "Genie", "Halloween", "magic"), ("1f40d", "Snake", "Halloween", ""),
    # --- nature ------------------------------------------------------------------
    ("1f338", "Cherry blossom", "Nature", "sakura spring flower"), ("1f33a", "Hibiscus", "Nature", "flower"),
    ("1f337", "Tulip", "Nature", "flower spring"), ("1f33b", "Sunflower", "Nature", "flower summer"),
    ("1f340", "Four-leaf clover", "Nature", "luck"), ("1f341", "Maple leaf", "Nature", "autumn fall"),
    ("1f342", "Fallen leaf", "Nature", "autumn fall"), ("1f343", "Leaf", "Nature", "wind"),
    ("1f33f", "Herb", "Nature", "plant"), ("1f335", "Cactus", "Nature", "desert"),
    ("1f332", "Evergreen", "Nature", "pine tree"), ("1f334", "Palm tree", "Nature", "beach summer"),
    ("1f33c", "Blossom", "Nature", "flower"), ("1f33e", "Rice", "Nature", ""),
    ("1f344", "Mushroom", "Nature", ""), ("1f41a", "Shell", "Nature", "beach"),
    # --- animals -----------------------------------------------------------------
    ("1f431", "Cat", "Animals", "kitten"), ("1f436", "Dog", "Animals", "puppy"),
    ("1f430", "Rabbit", "Animals", "bunny"), ("1f98a", "Fox", "Animals", ""),
    ("1f43b", "Bear", "Animals", ""), ("1f43c", "Panda", "Animals", ""),
    ("1f981", "Lion", "Animals", ""), ("1f42f", "Tiger", "Animals", ""),
    ("1f984", "Unicorn", "Animals", "magic"), ("1f98b", "Butterfly", "Animals", ""),
    ("1f41d", "Bee", "Animals", "honey"), ("1f41e", "Ladybug", "Animals", ""),
    ("1f419", "Octopus", "Animals", ""), ("1f420", "Tropical fish", "Animals", ""),
    ("1f988", "Shark", "Animals", ""), ("1f989", "Owl", "Animals", ""),
    ("1f427", "Penguin", "Animals", ""), ("1f438", "Frog", "Animals", ""),
    ("1f435", "Monkey", "Animals", ""), ("1f432", "Dragon", "Animals", ""),
    ("1f43e", "Paw prints", "Animals", ""), ("1f995", "Dinosaur", "Animals", ""),
    # --- food --------------------------------------------------------------------
    ("1f355", "Pizza", "Food", ""), ("1f354", "Burger", "Food", ""), ("1f35f", "Fries", "Food", ""),
    ("1f366", "Ice cream", "Food", ""), ("1f369", "Donut", "Food", ""), ("1f36a", "Cookie", "Food", ""),
    ("1f353", "Strawberry", "Food", ""), ("1f349", "Watermelon", "Food", ""),
    ("1f34e", "Apple", "Food", ""), ("1f34c", "Banana", "Food", ""), ("2615", "Coffee", "Food", ""),
    ("1f37f", "Popcorn", "Food", "movie"), ("1f9c1", "Cupcake", "Food", ""), ("1f36b", "Chocolate", "Food", ""),
    # --- objects -----------------------------------------------------------------
    ("1f453", "Glasses", "Objects", "spectacles"), ("1f576", "Sunglasses", "Objects", "shades cool"),
    ("1f3b5", "Music note", "Objects", ""), ("1f3b6", "Notes", "Objects", "music"),
    ("1f3a4", "Microphone", "Objects", "sing"), ("1f3a7", "Headphones", "Objects", "music"),
    ("1f4a1", "Light bulb", "Objects", "idea"), ("1f48e", "Gem", "Objects", "diamond"),
    ("1f4b0", "Money bag", "Objects", ""), ("1f680", "Rocket", "Objects", "space launch"),
    ("1f6f8", "UFO", "Objects", "space alien"), ("1f3ae", "Game controller", "Objects", "gaming"),
    ("1f3b8", "Guitar", "Objects", "rock"), ("1f4f7", "Camera", "Objects", "photo"),
    ("1f3ac", "Clapper", "Objects", "movie film"), ("1f52b", "Water pistol", "Objects", ""),
    # --- symbols and hands -------------------------------------------------------
    ("1f4af", "100", "Symbols", "perfect score"), ("2705", "Check", "Symbols", "yes done"),
    ("274c", "Cross", "Symbols", "no wrong"), ("2757", "Exclamation", "Symbols", ""),
    ("2753", "Question", "Symbols", ""), ("1f4a4", "Zzz", "Symbols", "sleep"),
    ("1f4ac", "Speech bubble", "Symbols", "chat"), ("1f44d", "Thumbs up", "Symbols", "like"),
    ("1f44f", "Clap", "Symbols", "applause"), ("1f64c", "Raised hands", "Symbols", "praise"),
    ("1f4aa", "Muscle", "Symbols", "strong"), ("270c", "Victory", "Symbols", "peace"),
    ("1f91f", "Love-you", "Symbols", "hand"), ("1f44b", "Wave hand", "Symbols", "hello"),
    ("1f6a8", "Siren", "Symbols", "alert emergency"), ("1f3af", "Bullseye", "Symbols", "target"),
)

CATEGORIES: tuple[str, ...] = tuple(dict.fromkeys(item[2] for item in CATALOGUE))
_BY_NAME = {item[1].lower(): item for item in CATALOGUE}


def emoji_dir() -> Path:
    return config.assets_dir() / "emoji"


def available() -> list[tuple[str, str, str, str]]:
    """Catalogue entries whose picture is actually bundled."""
    folder = emoji_dir()
    return [item for item in CATALOGUE if (folder / f"{item[0]}.png").exists()]


def search(text: str, category: str = "") -> list[tuple[str, str, str, str]]:
    needle = text.strip().lower().split()
    found = []
    for item in available():
        if category and item[2] != category:
            continue
        haystack = f"{item[1]} {item[2]} {item[3]}".lower()
        if all(word in haystack for word in needle):
            found.append(item)
    return found


@lru_cache(maxsize=512)
def load(code: str) -> Image.Image | None:
    """The 128 px RGBA picture for ``code`` (e.g. ``"1f384"``), or None."""
    path = emoji_dir() / f"{code}.png"
    try:
        with Image.open(path) as opened:
            return opened.convert("RGBA")
    except OSError:
        return None


def by_name(name: str) -> Image.Image | None:
    item = _BY_NAME.get(name.strip().lower())
    return load(item[0]) if item else None

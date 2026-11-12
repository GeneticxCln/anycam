# Effect packs

An effect pack adds picture effects to the Effects tab (category **Packs**) without changing AnyCam.

## Layout

```
~/.local/share/anycam/effects/
  film-grain/
    pack.json
    grain.py
```

`pack.json`:

```json
{
  "name": "film-grain",
  "version": "1.0",
  "author": "A. Person",
  "license": "MIT",
  "effects": [
    {"key": "grain", "label": "Film grain", "description": "Noise that moves",
     "file": "grain.py", "default": 0.4}
  ]
}
```

Names and keys are lowercase letters, digits, `.`, `_` and `-`. Each `file` is a `.py` inside the pack
folder, at most 1 MB.

## The effect

```python
from PIL import Image

class Effect:
    def __init__(self, amount=0.5):          # 0..1, the Strength slider
        self.amount = amount

    def set_amount(self, amount):            # optional
        self.amount = amount

    def process(self, image, now):           # Pillow RGBA in, Pillow RGBA out
        return image

    def close(self):                         # optional
        pass
```

`now` is seconds since the effect started. A returned picture of another size is scaled back.

## Safety

Pack code never runs inside AnyCam's main process. It runs in an effect worker: frames pass through shared
memory, a crash or a hang there (more than 1.5 s on one frame) costs the effect, the picture passes through
unchanged, and the worker is restarted after 1, 2, 4 ... 10 seconds. The same isolation is available for
the built-in AI effects under Settings -> Output -> AI effects.

Problems with a pack (unreadable `pack.json`, a bad name, a missing file) are listed in the same place.

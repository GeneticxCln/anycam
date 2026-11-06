# Bundled emoji

The pictures in this folder are a curated selection of Google's **Noto Emoji**
(`2D/png/128`), licensed under the **Apache License 2.0**.

- Source: https://github.com/googlefonts/noto-emoji
- Pinned commit: `99701e07d3d1fe1bdbcdb14964547f0fd592966f`
- Files are named by Unicode code point (`1f384.png` is the Christmas tree).
- `SHA256.json` holds the hash of every file; `scripts/fetch-emoji.py` rebuilds
  the folder from the pinned commit and rewrites it.
- Only the image files are used. The repository's fonts are SIL OFL and flags
  are under a separate licence; neither is bundled.

The Apache License 2.0 text is at https://www.apache.org/licenses/LICENSE-2.0.

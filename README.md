# Deutschstudio Lesson Library

Ready-made German lessons for the [Deutschstudio](https://github.com/asem87042-jpg/Deutschstudio-Final) app, served as static JSON.

## Layout
- `manifest.json` - index the app downloads (version, day list, sha256 per file)
- `days/<id>.json` - one lesson per file, same shape as the app's bundled lessons.json
- `review/<id>.txt` - plain-text summary of each generated day for quick human review
- `generate_lessons.py` - the generator (see its header for usage)

## Adding lessons
```
export GEMINI_API_KEY="your-free-aistudio-key"
python3 generate_lessons.py --level A1.1 --start 1 --count 3
```
Skim `review/*.txt`, delete any bad `days/<id>.json`, rerun, then commit and push.

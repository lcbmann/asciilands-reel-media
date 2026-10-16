# Smithing reel: editing code and data (v2 and v3)

This folder holds the code, timing data and footage log for the smithing reel. Raw recordings and renders stay out of Git.

## Setup

1. Download `claude-smithing-handoff.zip` from the `smithing-handoff-v1` release.
2. Copy its `media/`, `assets/` and `reference/` folders into this folder.
3. Install the dependencies: `pip install -r requirements.txt`, plus ffmpeg with libx264.

## Rebuild

```
python project/edit_reel_v3.py            # v3: full render, cached per shot
python project/edit_reel_v3.py --prepare  # timing only
python project/update_library_v3.py       # append v3 notes to library/
python project/build_review_v3.py         # review-v3.html
```

## Outputs

- `project/smithing-v3.mp4` is the current cut, and `project/smithing-v3-review.mp4` is its review copy.
- v3 also needs the pickups and stills listed in `project/HANDOFF-v3.txt`: `mapzoomout.mkv`, `oneclickstonepickaxe.mkv`, `rarematerials.wav`, the John_Smith screenshot and the game's `cp437_24x24.png` font.
- The v2 scripts are kept for comparison.

## More detail

- `project/HANDOFF-v3.txt` lists the v3 changes, inputs and caveats.
- `project/post-caption-v3.txt` has the post caption and the cover suggestion.

## Notes

`project/edit-v3/*.json` (and `edit-v2/` for v2) records the timeline, captions, retimed words, audio cuts and the measured audio report. The source paths in `timeline.json` point to the cloud session where v2 was rendered. The renderer regenerates them from `media/video/` on every run.

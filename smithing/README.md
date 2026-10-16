# Smithing reel v2: editing code and data

This folder holds the code, timing data and footage log for the smithing reel. Raw recordings and renders stay out of Git.

## Setup

1. Download `claude-smithing-handoff.zip` from the `smithing-handoff-v1` release.
2. Copy its `media/`, `assets/` and `reference/` folders into this folder.
3. Install the dependencies: `pip install -r requirements.txt`, plus ffmpeg with libx264.

## Rebuild

```
python project/edit_reel_v2.py            # full render, cached per shot
python project/edit_reel_v2.py --prepare  # timing only
python project/update_library_v2.py       # append v2 notes to library/
python project/build_review_v2.py         # review-v2.html
```

## Outputs

- `project/smithing-v2.mp4` is the final cut.
- `project/smithing-v2-review.mp4` is the review copy. It shows PROVISIONAL tags.

## More detail

- `project/HANDOFF-v2.txt` lists the missing pickups (`mapzoomout.mkv`, `oneclickarrows.mkv`) and explains how to drop them in.
- `project/post-caption-v2.txt` has the post caption and the cover suggestion.

## Notes

`project/edit-v2/*.json` records the timeline, captions, retimed words, audio cuts and the measured audio report. The source paths in `timeline.json` point to the cloud session where v2 was rendered. The renderer regenerates them from `media/video/` on every run.

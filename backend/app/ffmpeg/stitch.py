"""Concatenates Veo scene clips into one final reel.

Each Veo scene is generated independently (separate generate_videos call per
8s clip, see llm/video_provider.py), including its own ambient/narration
audio track with no coordination between scenes - a straight hard-cut concat
previously jumped loudness/tone at every cut and never blended shot to shot.
stitch_clips now (a) loudness-normalizes each clip's audio to a common
target before mixing and (b) crossfades consecutive clips (video `xfade` +
audio `acrossfade`) instead of an instant cut, via _stitch_with_crossfade.
Falls back to the old hard-cut concat (_stitch_hard_cut - stream-copy, or a
plain re-encode if codecs don't match) if the crossfade/loudnorm
filter_complex build fails for any reason - a working reel with hard cuts
beats no reel at all.
"""

import logging
import subprocess
import tempfile
from pathlib import Path

from backend.app.llm.video_provider import CLIP_DURATION_SECONDS

logger = logging.getLogger(__name__)

# Short enough not to eat into the ~8s scenes' own content, long enough to
# read as a real blend rather than a slightly-softened cut.
CROSSFADE_SECONDS = 0.4
# Standard streaming loudness target (same figures broadcasters/Spotify/
# YouTube converge on) - not tuned specifically for Veo's output, just a
# sane common target so consecutive independently-generated clips stop
# jumping in perceived loudness at each cut.
_LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11"


def stitch_clips(clip_bytes_list: list[bytes]) -> bytes:
    if not clip_bytes_list:
        raise ValueError("No clips to stitch")
    if len(clip_bytes_list) == 1:
        return clip_bytes_list[0]

    try:
        return _stitch_with_crossfade(clip_bytes_list)
    except Exception:
        logger.exception(
            "Crossfade/loudnorm stitch failed for %d clips, falling back to a hard-cut concat",
            len(clip_bytes_list),
        )
        return _stitch_hard_cut(clip_bytes_list)


def _stitch_with_crossfade(clip_bytes_list: list[bytes]) -> bytes:
    """Builds one ffmpeg filter_complex graph chaining xfade (video) +
    acrossfade (audio) across all N clips. All clips share the same fixed
    duration (CLIP_DURATION_SECONDS - every scene is generated with the same
    Veo config), so each transition's offset is a closed form rather than
    needing to probe each clip's real length: the i-th transition (1-indexed)
    starts at i*(D-C) seconds into the merged-so-far stream, where D is the
    per-clip duration and C is the crossfade duration - derived from xfade's
    "offset is relative to the first input's own timeline" semantics, where
    after the first merge that "first input" is already the merged stream,
    not a raw clip (worked out against a real 3-clip example: offsets 4, 8
    for 5s clips with a 1s transition = 1*(5-1), 2*(5-1))."""
    n = len(clip_bytes_list)
    duration = CLIP_DURATION_SECONDS
    crossfade = CROSSFADE_SECONDS

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        clip_paths = []
        for i, clip_bytes in enumerate(clip_bytes_list):
            path = tmp_path / f"clip_{i:03d}.mp4"
            path.write_bytes(clip_bytes)
            clip_paths.append(path)

        inputs: list[str] = []
        for p in clip_paths:
            inputs += ["-i", str(p)]

        filters: list[str] = []
        # settb=AVTB/asettb=AVTB - Veo clips are separate files from
        # separate API calls, and xfade/acrossfade both assume a common
        # timebase across their inputs; without normalizing it first, clips
        # with even a slightly different timebase can desync or make ffmpeg
        # reject the filter graph outright. format=yuv420p guards against a
        # pixel-format mismatch the same way.
        for i in range(n):
            filters.append(f"[{i}:v]settb=AVTB,format=yuv420p[v{i}n]")
            filters.append(f"[{i}:a]{_LOUDNORM_FILTER},asettb=AVTB[a{i}n]")

        v_label, a_label = "v0n", "a0n"
        for i in range(1, n):
            offset = i * (duration - crossfade)
            is_last = i == n - 1
            out_v = "vout" if is_last else f"vx{i}"
            out_a = "aout" if is_last else f"ax{i}"
            filters.append(
                f"[{v_label}][v{i}n]xfade=transition=fade:duration={crossfade}:offset={offset}[{out_v}]"
            )
            filters.append(f"[{a_label}][a{i}n]acrossfade=d={crossfade}[{out_a}]")
            v_label, a_label = out_v, out_a

        output_path = tmp_path / "output.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", *inputs,
                "-filter_complex", ";".join(filters),
                "-map", f"[{v_label}]", "-map", f"[{a_label}]",
                "-c:v", "libx264", "-c:a", "aac",
                str(output_path),
            ],
            check=True,
            capture_output=True,
            timeout=60 * max(3, n),
            cwd=tmp_path,
        )
        return output_path.read_bytes()


def _stitch_hard_cut(clip_bytes_list: list[bytes]) -> bytes:
    """Original behavior - plain concat, no crossfade/loudnorm. All clips
    come from the same model/config, so their codec/resolution/fps usually
    match and the concat demuxer can stream-copy without re-encoding; falls
    back to a re-encode if that assumption doesn't hold for some reason."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        clip_paths = []
        for i, clip_bytes in enumerate(clip_bytes_list):
            path = tmp_path / f"clip_{i:03d}.mp4"
            path.write_bytes(clip_bytes)
            clip_paths.append(path)

        filelist_path = tmp_path / "filelist.txt"
        filelist_path.write_text("\n".join(f"file '{p.name}'" for p in clip_paths))

        output_path = tmp_path / "output.mp4"
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(filelist_path), "-c", "copy", str(output_path),
                ],
                check=True,
                capture_output=True,
                timeout=60,
                cwd=tmp_path,
            )
        except subprocess.CalledProcessError:
            # Stream copy needs identical codec params; fall back to a
            # re-encode if the clips don't quite match for any reason.
            subprocess.run(
                [
                    "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(filelist_path),
                    "-c:v", "libx264", "-c:a", "aac", str(output_path),
                ],
                check=True,
                capture_output=True,
                timeout=180,
                cwd=tmp_path,
            )

        return output_path.read_bytes()

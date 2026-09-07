"""Concatenates Veo scene clips into one final reel. All clips come from the
same model/config (see llm/video_provider.py), so their codec/resolution/fps
match and the concat demuxer can stream-copy without re-encoding - fast, and
avoids a lossy re-encode generation for every reel produced.
"""

import subprocess
import tempfile
from pathlib import Path


def stitch_clips(clip_bytes_list: list[bytes]) -> bytes:
    if not clip_bytes_list:
        raise ValueError("No clips to stitch")
    if len(clip_bytes_list) == 1:
        return clip_bytes_list[0]

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

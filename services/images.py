"""Customer photo handling. Photos end up attached to emails sent to third parties, so we never keep the original bytes."""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD_BYTES = 8_000_000
MAX_DIMENSION = 2000
ACCEPTED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF", "MPO"}  # MPO = some phones' JPEG variant


class BadImage(ValueError):
    pass


def sanitise_image(data: bytes) -> bytes:
    """Decode, fix orientation, downscale and re-encode as JPEG.

    Re-encoding drops all metadata (EXIF GPS location, device info) and anything smuggled inside the file.
    Raises BadImage if the bytes aren't a real, reasonably sized image.
    """
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise BadImage("Image is empty or larger than 8MB")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.format not in ACCEPTED_FORMATS:
                raise BadImage(f"Unsupported image type: {probe.format}")
            probe.verify()
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")  # also flattens transparency and takes the first frame of a GIF
            img.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=85, optimize=True)  # no exif= argument: nothing is carried over
            return out.getvalue()
    except BadImage:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise BadImage("That file isn't a readable image") from exc


def save_sanitised(data: bytes, dest_dir: Path, stem: str) -> Path:
    clean = sanitise_image(data)
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{stem}.jpg"
    path.write_bytes(clean)
    return path

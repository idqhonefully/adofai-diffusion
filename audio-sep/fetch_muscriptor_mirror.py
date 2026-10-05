#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Download MuScriptor weights from the community mirror AEmotionStudio/muscriptor-models.
This mirror claims the weights are byte-identical to the official MuScriptor repos
and provides SHA256 checksums.  It may let you skip the official HuggingFace gating.

Usage:
    mus_env\\Scripts\\python.exe fetch_muscriptor_mirror.py --size medium
    mus_env\\Scripts\\python.exe fetch_muscriptor_mirror.py --size large --out-dir D:\\models

The script respects HF_ENDPOINT / HF_TOKEN if set, and defaults endpoint to
https://hf-mirror.com for users in mainland China.
"""
import argparse
import hashlib
import os
import sys
from pathlib import Path

from huggingface_hub import hf_hub_download
from huggingface_hub.errors import GatedRepoError, RepositoryNotFoundError, HfHubHTTPError

MIRROR_REPO = "AEmotionStudio/muscriptor-models"

# SHA256 of model.safetensors, copied from the mirror's NOTICE file.
_CHECKSUMS = {
    "small/model.safetensors": "bbd482c786b895cf7d8f44185073d951adae2ebb8a66f82ca84cd1f84569549c",
    "medium/model.safetensors": "ac80adbdf85d87231735fd948af7013441c0afced316c4e9067fd5d8a7fb97ec",
    "large/model.safetensors": "ac4eb6ea87dfc26b6ca6b954c6b967ab87ad4c7d08e078b25214f13ed051f397",
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(8 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def download(size: str, out_dir: Path) -> Path:
    out_dir = out_dir.resolve()
    target = out_dir / size
    target.mkdir(parents=True, exist_ok=True)

    weights_rel = f"{size}/model.safetensors"
    config_rel = f"{size}/config.json"

    for rel_path in (weights_rel, config_rel):
        print(f"[mirror] {rel_path}: fetching ...", flush=True)
        downloaded = hf_hub_download(
            repo_id=MIRROR_REPO,
            filename=Path(rel_path).name,
            subfolder=size,
            local_dir=str(out_dir),
            local_dir_use_symlinks=False,
        )
        local = Path(downloaded).resolve()
        print(f"[mirror] {rel_path}: saved to {local}", flush=True)

        if rel_path.endswith("model.safetensors"):
            expected = _CHECKSUMS.get(rel_path)
            if expected:
                actual = _sha256(local)
                if actual != expected:
                    raise RuntimeError(
                        f"checksum mismatch for {rel_path}\n"
                        f"  expected: {expected}\n"
                        f"  actual:   {actual}\n"
                        f"File may be corrupted; delete it and retry."
                    )
                print(f"[mirror] {rel_path}: checksum OK ({actual[:16]}...)")

    return target / "model.safetensors"


def main():
    parser = argparse.ArgumentParser(description="Fetch MuScriptor weights from community mirror")
    parser.add_argument("--size", required=True, choices=["small", "medium", "large"],
                        help="Model variant to download")
    parser.add_argument("--out-dir", default=str(Path(__file__).parent / "models" / "muscriptor"),
                        help="Where to save the weights (subdirectory <size> will be created)")
    args = parser.parse_args()

    if not os.environ.get("HF_ENDPOINT"):
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

    try:
        weights_path = download(args.size, Path(args.out_dir))
    except GatedRepoError as e:
        print(f"[mirror] ERROR: the mirror repo is also gated. {e}", file=sys.stderr)
        print("[mirror] You still need to accept the license and provide HF_TOKEN.", file=sys.stderr)
        sys.exit(1)
    except RepositoryNotFoundError as e:
        print(f"[mirror] ERROR: mirror repo not found. {e}", file=sys.stderr)
        sys.exit(1)
    except HfHubHTTPError as e:
        print(f"[mirror] ERROR: HF HTTP error ({e}).", file=sys.stderr)
        print("[mirror] Check HF_ENDPOINT / network / token.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[mirror] ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[mirror] Weights ready: {weights_path}")


if __name__ == "__main__":
    main()

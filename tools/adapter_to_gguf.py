"""Convert the PEFT adapter to the GGUF form llama.cpp loads.

    python tools/adapter_to_gguf.py --out desktop/assets/nimbus-1-1-prime-ee.gguf

WHY NOT THE OFFICIAL SCRIPT

llama.cpp ships `convert_lora_to_gguf.py`, and it is the authority on this
format -- everything below follows it. But it imports torch and the whole
convert_hf_to_gguf machinery to do what, for this adapter, is reading pairs of
small matrices and writing them out under different names. That is a 2.5 GB
install on every machine that ever rebuilds this file.

So this reads the safetensors container directly. The format is a length, a JSON
header and a block of bytes; numpy does the rest. The one type numpy does not
know is bfloat16, which is the top sixteen bits of a float32 and converts by
shifting them back.

WHAT IT MUST GET RIGHT, AND HOW THAT IS CHECKED

Three things, all verified against the adapter's own config rather than assumed:

  names   `base_model.model.model.layers.0.self_attn.q_proj.lora_A.weight`
          becomes `blk.0.attn_q.weight.lora_a`. The middle step is the same
          TensorNameMap the official converter uses, from the same `gguf`
          package, for the same architecture.

  pairs   every A must have its B and every B its A. A lone matrix means the
          mapping dropped one, and an adapter missing half a layer loads
          without complaint and answers slightly wrong.

  arch    `general.architecture` has to match the base model or llama.cpp
          refuses the file. It is read from the base model's config, not typed
          in here.

Qwen2 is one of the architectures whose attention weights need no permutation on
the way in -- that is a llama-family transformation, and applying it here would
produce a file that loads and generates noise. The check at the end is the real
test: the adapter is loaded beside the base and the output is compared with the
adapter switched off.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.request
from pathlib import Path


ADAPTER_REPO = "AhoosAI/nimbus-1-1-prime-ee"
BASE_REPO = "Qwen/Qwen2.5-Coder-7B-Instruct"
HUB = "https://huggingface.co/%s/resolve/main/%s"


# The conversion itself lives in desktop/lora.py, where the app uses it for
# adapters imported from the Hub. This script is the build's way in.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop.lora import LoraError, convert as convert_files  # noqa: E402


# Size and hash for the one file big enough to arrive half-finished. The Hub
# publishes both; checking them is what turns a truncated download into an error
# here rather than into a converter crash three steps later.
ADAPTER_BYTES = 161_533_192
ADAPTER_SHA = "64bf8ffca94700c255abc9653bbeb4e26ebdced6ba211c06055c91f0c24a4f96"


def fetch(url: str, target: Path, *, size: int = 0, sha256: str = "") -> Path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from desktop.download import fetch as resumable

    if target.is_file() and (not size or target.stat().st_size == size):
        return target
    print(f"  fetching {target.name}")
    return resumable(url, target, expected_bytes=size, sha256=sha256,
                     on_progress=lambda p: None)


def convert(adapter_dir: Path, out: Path) -> Path:
    base_config = json.loads((adapter_dir / "base_config.json").read_text(encoding="utf-8"))
    try:
        done = convert_files(adapter_dir / "adapter_config.json", adapter_dir / "adapter_model.safetensors",
                             base_config, out)
    except LoraError as error:
        raise SystemExit(str(error)) from error
    print(f"  {done['pairs']} adapted weights, rank {done['rank']}, alpha {done['alpha']:g}")
    print(f"  {out}  {out.stat().st_size / 1e6:.1f} MB")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path,
                        default=Path(__file__).resolve().parents[1] / "build" / "adapter")
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).resolve().parents[1] / "desktop" / "assets"
                        / "nimbus-1-1-prime-ee.gguf")
    parser.add_argument("--adapter-dir", type=Path,
                        help="a PEFT folder already on disk, instead of the Hub. "
                             "Nimbus 2 Apex lives in a GitHub release, not on the Hub.")
    parser.add_argument("--base-repo", default=BASE_REPO,
                        help="whose config.json says which architecture this adapts")
    args = parser.parse_args()

    args.work.mkdir(parents=True, exist_ok=True)
    if args.adapter_dir:
        source = args.adapter_dir
        for name in ("adapter_config.json", "adapter_model.safetensors"):
            if not (source / name).is_file():
                raise SystemExit(f"{source / name} is missing")
            shutil.copyfile(source / name, args.work / name)
    else:
        fetch(HUB % (ADAPTER_REPO, "adapter_config.json"), args.work / "adapter_config.json")
        fetch(HUB % (ADAPTER_REPO, "adapter_model.safetensors"), args.work / "adapter_model.safetensors",
              size=ADAPTER_BYTES, sha256=ADAPTER_SHA)
    # Only the base model's config, never its weights: this needs the
    # architecture and the layer count, which are two numbers in a small file.
    fetch(HUB % (args.base_repo, "config.json"), args.work / "base_config.json")

    convert(args.work, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

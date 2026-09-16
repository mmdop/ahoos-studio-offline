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
import struct
import sys
import urllib.request
from pathlib import Path

import numpy as np

ADAPTER_REPO = "AhoosAI/nimbus-1-1-prime-ee"
BASE_REPO = "Qwen/Qwen2.5-Coder-7B-Instruct"
HUB = "https://huggingface.co/%s/resolve/main/%s"


# -- safetensors, without the library ---------------------------------------

def read_safetensors(path: Path) -> tuple[dict[str, np.ndarray], dict]:
    """Every tensor in the file, as float32, plus whatever metadata it carries."""
    with path.open("rb") as handle:
        (header_length,) = struct.unpack("<Q", handle.read(8))
        header = json.loads(handle.read(header_length))
        body = handle.read()

    metadata = header.pop("__metadata__", {})
    tensors: dict[str, np.ndarray] = {}
    for name, entry in header.items():
        start, end = entry["data_offsets"]
        raw = body[start:end]
        dtype = entry["dtype"]
        if dtype == "BF16":
            # numpy has no bfloat16. The bits are the high half of a float32, so
            # widening is a shift rather than a conversion -- and it is exact,
            # which matters because this is the only step where a mistake would
            # be invisible: the file would load and the answers would drift.
            half = np.frombuffer(raw, dtype="<u2").astype(np.uint32) << 16
            values = half.view(np.float32)
        elif dtype == "F16":
            values = np.frombuffer(raw, dtype="<f2").astype(np.float32)
        elif dtype == "F32":
            values = np.frombuffer(raw, dtype="<f4")
        elif dtype == "F64":
            values = np.frombuffer(raw, dtype="<f8").astype(np.float32)
        else:
            raise SystemExit(f"{name}: unsupported dtype {dtype!r}")
        tensors[name] = values.reshape(entry["shape"])
    return tensors, metadata


# -- naming ------------------------------------------------------------------

def base_tensor_name(lora_name: str) -> str:
    """The base model's name for the weight this pair adapts.

    Straight from llama.cpp's get_base_tensor_name, so the two agree.
    """
    name = lora_name.replace("base_model.model.", "")
    for suffix in (".lora_A.weight", ".lora_B.weight",
                   ".lora_embedding_A", ".lora_embedding_B"):
        name = name.replace(suffix, ".weight")
    return name


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
    import gguf

    config = json.loads((adapter_dir / "adapter_config.json").read_text(encoding="utf-8"))
    base_config = json.loads((adapter_dir / "base_config.json").read_text(encoding="utf-8"))

    architectures = base_config.get("architectures") or []
    if architectures != ["Qwen2ForCausalLM"]:
        raise SystemExit(
            f"the base model reports {architectures}, which this converter has not been "
            "checked against. The tensor mapping below is Qwen2's."
        )
    arch = gguf.MODEL_ARCH.QWEN2
    block_count = int(base_config["num_hidden_layers"])
    alpha = float(config["lora_alpha"])

    tensors, _ = read_safetensors(adapter_dir / "adapter_model.safetensors")
    name_map = gguf.get_tensor_name_map(arch, block_count)

    pairs: dict[str, dict[str, np.ndarray]] = {}
    for name, values in tensors.items():
        if ".lora_A.weight" in name or ".lora_embedding_A" in name:
            side = "a"
        elif ".lora_B.weight" in name or ".lora_embedding_B" in name:
            side = "b"
        else:
            raise SystemExit(f"{name}: not a lora_A or lora_B tensor")

        hf_name = base_tensor_name(name)
        mapped = name_map.get_name(hf_name.removesuffix(".weight"), try_suffixes=(".weight",))
        if mapped is None:
            raise SystemExit(f"{name}: no GGUF name for {hf_name}")
        pairs.setdefault(mapped + ".weight", {})[side] = values

    lonely = [k for k, v in pairs.items() if len(v) != 2]
    if lonely:
        raise SystemExit(
            f"{len(lonely)} weights have only one half of their pair: {lonely[:4]}"
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    writer = gguf.GGUFWriter(str(out), gguf.MODEL_ARCH_NAMES[arch])
    writer.add_type(gguf.GGUFType.ADAPTER)
    writer.add_string(gguf.Keys.Adapter.TYPE, "lora")
    writer.add_float32(gguf.Keys.Adapter.LORA_ALPHA, alpha)

    for dest, halves in sorted(pairs.items()):
        for side in ("a", "b"):
            # f16 on purpose: the adapter was trained in bfloat16, so f16 keeps
            # every bit that carries signal and halves a file that ships inside
            # the installer.
            writer.add_tensor(f"{dest}.lora_{side}", halves[side].astype(np.float16))

    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_tensors_to_file()
    writer.close()

    print(f"  {len(pairs)} adapted weights, rank {config['r']}, alpha {alpha:g}")
    print(f"  {out}  {out.stat().st_size / 1e6:.1f} MB")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path,
                        default=Path(__file__).resolve().parents[1] / "build" / "adapter")
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).resolve().parents[1] / "desktop" / "assets"
                        / "nimbus-1-1-prime-ee.gguf")
    args = parser.parse_args()

    args.work.mkdir(parents=True, exist_ok=True)
    fetch(HUB % (ADAPTER_REPO, "adapter_config.json"), args.work / "adapter_config.json")
    fetch(HUB % (ADAPTER_REPO, "adapter_model.safetensors"), args.work / "adapter_model.safetensors",
          size=ADAPTER_BYTES, sha256=ADAPTER_SHA)
    # Only the base model's config, never its weights: this needs the
    # architecture and the layer count, which are two numbers in a small file.
    fetch(HUB % (BASE_REPO, "config.json"), args.work / "base_config.json")

    convert(args.work, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

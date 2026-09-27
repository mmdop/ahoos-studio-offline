"""A PEFT adapter, converted to the GGUF form llama.cpp loads.

Moved here from tools/adapter_to_gguf.py so the app can do it too: an adapter
imported from the Hub in PEFT form is converted on the person's computer, after
it arrives, with nothing but numpy and the `gguf` package.

WHY NOT THE OFFICIAL SCRIPT

llama.cpp ships `convert_lora_to_gguf.py`, and it is the authority on this
format -- everything below follows it. But it imports torch and the whole
convert_hf_to_gguf machinery to do what, for an adapter, is reading pairs of
small matrices and writing them out under different names.

WHAT IT MUST GET RIGHT

  names   `base_model.model.model.layers.0.self_attn.q_proj.lora_A.weight`
          becomes `blk.0.attn_q.weight.lora_a`, through the same TensorNameMap
          the official converter uses.
  pairs   every A must have its B. A lone matrix means the mapping dropped one,
          and an adapter missing half a layer loads without complaint and
          answers slightly wrong.
  arch    `general.architecture` must match the base model, read from the
          base's config rather than typed in.

Only architectures whose attention weights need no permutation on the way in
are accepted -- Qwen2 and Qwen3, both checked. A llama-family base permutes its
q and k weights, and an adapter converted without the same permutation loads
and generates noise; that is refused with a message rather than guessed at.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path


class LoraError(RuntimeError):
    """The adapter cannot be converted, and the message says why."""


SUPPORTED = ("Qwen2ForCausalLM", "Qwen3ForCausalLM")


def read_safetensors(path: Path):
    """Every tensor in the file, as float32, plus whatever metadata it carries."""
    import numpy as np

    with path.open("rb") as handle:
        (header_length,) = struct.unpack("<Q", handle.read(8))
        header = json.loads(handle.read(header_length))
        body = handle.read()

    metadata = header.pop("__metadata__", {})
    tensors = {}
    for name, entry in header.items():
        start, end = entry["data_offsets"]
        raw = body[start:end]
        dtype = entry["dtype"]
        if dtype == "BF16":
            # numpy has no bfloat16. The bits are the high half of a float32, so
            # widening is a shift, and exact.
            half = np.frombuffer(raw, dtype="<u2").astype(np.uint32) << 16
            values = half.view(np.float32)
        elif dtype == "F16":
            values = np.frombuffer(raw, dtype="<f2").astype(np.float32)
        elif dtype == "F32":
            values = np.frombuffer(raw, dtype="<f4")
        elif dtype == "F64":
            values = np.frombuffer(raw, dtype="<f8").astype(np.float32)
        else:
            raise LoraError(f"{name}: unsupported dtype {dtype!r}")
        tensors[name] = values.reshape(entry["shape"])
    return tensors, metadata


def base_tensor_name(lora_name: str) -> str:
    """The base model's name for the weight this pair adapts (llama.cpp's get_base_tensor_name)."""
    name = lora_name.replace("base_model.model.", "")
    for suffix in (".lora_A.weight", ".lora_B.weight", ".lora_embedding_A", ".lora_embedding_B"):
        name = name.replace(suffix, ".weight")
    return name


def convert(config_path: Path, weights_path: Path, base_config: dict, out: Path) -> dict:
    """Write `out`; return what was converted."""
    try:
        import gguf
        import numpy as np
    except ImportError as error:
        raise LoraError("converting a PEFT adapter needs numpy and gguf, which this build lacks") from error

    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    arches = {"Qwen2ForCausalLM": gguf.MODEL_ARCH.QWEN2, "Qwen3ForCausalLM": gguf.MODEL_ARCH.QWEN3}
    architectures = base_config.get("architectures") or []
    if len(architectures) != 1 or architectures[0] not in arches:
        raise LoraError(
            f"the base model is {', '.join(architectures) or 'of an unknown architecture'}; this app converts "
            f"adapters for {' and '.join(SUPPORTED)} only. Convert it with llama.cpp's convert_lora_to_gguf.py, "
            "or look for a GGUF version of the adapter.")
    arch = arches[architectures[0]]
    block_count = int(base_config["num_hidden_layers"])
    alpha = float(config["lora_alpha"])

    tensors, _ = read_safetensors(Path(weights_path))
    name_map = gguf.get_tensor_name_map(arch, block_count)

    pairs: dict[str, dict] = {}
    for name, values in tensors.items():
        if ".lora_A.weight" in name or ".lora_embedding_A" in name:
            side = "a"
        elif ".lora_B.weight" in name or ".lora_embedding_B" in name:
            side = "b"
        else:
            raise LoraError(f"{name}: not a lora_A or lora_B tensor")
        hf_name = base_tensor_name(name)
        mapped = name_map.get_name(hf_name.removesuffix(".weight"), try_suffixes=(".weight",))
        if mapped is None:
            raise LoraError(f"{name}: no GGUF name for {hf_name}")
        pairs.setdefault(mapped + ".weight", {})[side] = values

    lonely = [k for k, v in pairs.items() if len(v) != 2]
    if lonely:
        raise LoraError(f"{len(lonely)} weights have only one half of their pair: {lonely[:4]}")

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    writer = gguf.GGUFWriter(str(out), gguf.MODEL_ARCH_NAMES[arch])
    writer.add_type(gguf.GGUFType.ADAPTER)
    writer.add_string(gguf.Keys.Adapter.TYPE, "lora")
    writer.add_float32(gguf.Keys.Adapter.LORA_ALPHA, alpha)
    for dest, halves in sorted(pairs.items()):
        for side in ("a", "b"):
            # f16: the adapters are trained in bfloat16, so f16 keeps every bit
            # that carries signal and halves the file.
            writer.add_tensor(f"{dest}.lora_{side}", halves[side].astype(np.float16))
    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_tensors_to_file()
    writer.close()
    return {"pairs": len(pairs), "rank": config.get("r"), "alpha": alpha,
            "architecture": gguf.MODEL_ARCH_NAMES[arch], "bytes": out.stat().st_size}

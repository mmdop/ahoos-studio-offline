"""What the offline build downloads, and where from.

Two files make a working model: the base, and our adapter.

WHY THE BASE IS SOMEBODY ELSE'S FILE

Qwen publish GGUF builds of Qwen2.5-Coder-7B-Instruct themselves, ungated, in
every size. Re-hosting those would mean uploading fifteen gigabytes to add
nothing -- the same weights, further from their source, with us in the middle of
every download. The app fetches them from Qwen and our own repository holds only
what is ours, which is the adapter.

WHY THREE SIZES AND NOT ONE

The difference between q3 and q5 is about a gigabyte and a half of download and
roughly the same again in memory while it runs. On a machine with 8 GB that is
the difference between working and swapping, and nobody can pick for them from
here. The default is q4_k_m because it is the one that fits the most machines
without being visibly worse than the base.

The sizes below are exact byte counts from the Hub, not estimates, and the
downloader checks against them -- a file that arrives short is a file that will
fail to load later with a message about tensors instead of about the network.
"""

from __future__ import annotations

from dataclasses import dataclass

BASE_REPO = "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF"

# The adapter, converted from the PEFT weights by tools/adapter_to_gguf.py. It
# ships inside the app rather than being fetched: it is 80 MB, it is ours, and
# the Hugging Face repository stays exactly as it was published. One download
# for the user instead of two, and nothing on the Hub to keep in step.
ADAPTER_FILE = "nimbus-1-1-prime-ee.gguf"


def adapter_path():
    from .paths import assets_dir

    return assets_dir() / ADAPTER_FILE


def hub_url(repo: str, filename: str) -> str:
    return f"https://huggingface.co/{repo}/resolve/main/{filename}"


@dataclass(frozen=True)
class Build:
    """One quantisation of the base."""

    key: str
    filename: str
    bytes: int
    sha256: str
    ram_hint_gb: int
    label_en: str
    label_fa: str

    @property
    def url(self) -> str:
        return hub_url(BASE_REPO, self.filename)

    @property
    def gigabytes(self) -> float:
        return self.bytes / 1e9


# Sizes and hashes are the Hub's own, read from its paths-info API rather than
# rounded from a listing. The first set written here was off by a few hundred
# bytes apiece, which would have made the downloader reject three perfectly good
# files -- so these are checked in as data, and re-read with `python -m desktop
# verify-catalogue` when the upstream repository changes.
BUILDS: tuple[Build, ...] = (
    Build("q3_k_m", "qwen2.5-coder-7b-instruct-q3_k_m.gguf", 3_808_391_104,
          "ff5c64615cf8a44651d208e9d8da1f753feefc21605e6c1ee67957aa77257c3c", 8,
          "Smallest — for 8 GB of memory",
          "کوچک‌ترین — برای ۸ گیگابایت حافظه"),
    Build("q4_k_m", "qwen2.5-coder-7b-instruct-q4_k_m.gguf", 4_683_073_536,
          "509287f78cb4d4cf6b3843734733b914b2c158e43e22a7f4bf5e963800894d3c", 12,
          "Recommended — the best fit for most machines",
          "پیشنهادی — مناسب بیشتر دستگاه‌ها"),
    Build("q5_k_m", "qwen2.5-coder-7b-instruct-q5_k_m.gguf", 5_444_831_232,
          "586844eac4d6d6321689f0192c8aa8e69cd8625974a5cc2d925b1a03366e4d16", 16,
          "Largest — closest to the full-precision model",
          "بزرگ‌ترین — نزدیک‌ترین به مدل با دقت کامل"),
)

DEFAULT_BUILD = "q4_k_m"


def build(key: str) -> Build:
    for item in BUILDS:
        if item.key == key:
            return item
    raise KeyError(f"no such build: {key!r}. have: {', '.join(b.key for b in BUILDS)}")

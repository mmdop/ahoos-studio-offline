"""What the offline build downloads, and where from.

Two files make a working model: the base, and our adapter.

WHY THE BASE IS SOMEBODY ELSE'S FILE

Qwen publish GGUF builds of their own models, ungated, in every size.
Re-hosting those would mean uploading fifteen gigabytes to add nothing -- the
same weights, further from their source, with us in the middle of every
download. The app fetches them from Qwen and our own repository holds only what
is ours, which is the adapter.

WHY MORE THAN ONE SIZE

The difference between one quantisation and the next is about a gigabyte of
download and roughly the same again in memory while it runs. On a small machine
that is the difference between working and swapping, and nobody can pick for
them from here.

WHY TWO MODELS

Nimbus 1.1 Prime-EE and Nimbus 2 Apex are not versions of each other. They sit
on different bases (Qwen2.5-Coder-7B against Qwen3-8B), they were trained for
different things -- one for the habits a good programmer gets wrong, one for
doing as it is told -- and they answer to different controls. Both are kept
because a person on an eight-gigabyte machine can run the first and not the
second.

The sizes below are exact byte counts from the Hub, not estimates, and the
downloader checks against them -- a file that arrives short is a file that will
fail to load later with a message about tensors instead of about the network.
"""

from __future__ import annotations

from dataclasses import dataclass

# The adapters, converted from the PEFT weights by tools/adapter_to_gguf.py.
# They ship inside the app rather than being fetched: they are ours, they are
# small beside the base, and the Hugging Face repositories stay exactly as they
# were published. One download for the user instead of two, and nothing on the
# Hub to keep in step.
ADAPTER_FILE = "nimbus-1-1-prime-ee.gguf"
APEX_ADAPTER_FILE = "nimbus-2-apex.gguf"


@dataclass(frozen=True)
class Model:
    """One of ours: a base to fetch, an adapter that ships, and a personality."""

    id: str
    name: str
    base_repo: str
    adapter_file: str
    engine: str                 # "studio" (the 1.1 family) | "apex" (the two dials)
    summary_en: str
    summary_fa: str


MODELS: tuple[Model, ...] = (
    Model("nimbus-1.1-prime-ee", "Nimbus 1.1 Prime-EE",
          "Qwen/Qwen2.5-Coder-7B-Instruct-GGUF", ADAPTER_FILE, "studio",
          "Writes code that works for everyone — right-to-left layouts, labelled "
          "controls, containers that do not run as root.",
          "کدی می‌نویسد که برای همه کار کند — چیدمان راست‌به‌چپ، کنترل‌های برچسب‌دار، "
          "و کانتینری که با کاربر ریشه اجرا نمی‌شود."),
    Model("nimbus-2-apex", "Nimbus 2 Apex",
          "Qwen/Qwen3-8B-GGUF", APEX_ADAPTER_FILE, "apex",
          "Does as it is told. A thinking level from 1 to 20 and a temperature "
          "from 1 to 10, both of them real dials rather than labels.",
          "آنچه گفته می‌شود انجام می‌دهد. سطح تفکر ۱ تا ۲۰ و دمای ۱ تا ۱۰ — "
          "هر دو دستگیره‌ی واقعی، نه برچسب."),
)

DEFAULT_MODEL = "nimbus-2-apex"


def model(model_id: str) -> Model:
    for item in MODELS:
        if item.id == model_id:
            return item
    raise KeyError(f"no such model: {model_id!r}. have: {', '.join(m.id for m in MODELS)}")


def adapter_path(model_id: str = "nimbus-1.1-prime-ee"):
    from .paths import assets_dir

    return assets_dir() / model(model_id).adapter_file


def hub_url(repo: str, filename: str) -> str:
    return f"https://huggingface.co/{repo}/resolve/main/{filename}"


@dataclass(frozen=True)
class Build:
    """One quantisation of one model's base."""

    key: str
    model: str
    filename: str
    bytes: int
    sha256: str
    ram_hint_gb: int
    label_en: str
    label_fa: str

    @property
    def url(self) -> str:
        return hub_url(model(self.model).base_repo, self.filename)

    @property
    def gigabytes(self) -> float:
        return self.bytes / 1e9


# Sizes and hashes are the Hub's own, read from its paths-info API rather than
# rounded from a listing. The first set written here was off by a few hundred
# bytes apiece, which would have made the downloader reject three perfectly good
# files -- so these are checked in as data, and re-read with `python -m desktop
# verify-catalogue` when an upstream repository changes.
#
# Qwen's own Qwen3 repository publishes no q3, so Nimbus 2 Apex starts at q4 and
# asks for more memory than 1.1 does. Saying so is better than re-hosting a
# smaller build of somebody else's weights to fill the gap.
BUILDS: tuple[Build, ...] = (
    Build("q3_k_m", "nimbus-1.1-prime-ee", "qwen2.5-coder-7b-instruct-q3_k_m.gguf", 3_808_391_104,
          "ff5c64615cf8a44651d208e9d8da1f753feefc21605e6c1ee67957aa77257c3c", 8,
          "Smallest — for 8 GB of memory",
          "کوچک‌ترین — برای ۸ گیگابایت حافظه"),
    Build("q4_k_m", "nimbus-1.1-prime-ee", "qwen2.5-coder-7b-instruct-q4_k_m.gguf", 4_683_073_536,
          "509287f78cb4d4cf6b3843734733b914b2c158e43e22a7f4bf5e963800894d3c", 12,
          "Recommended — the best fit for most machines",
          "پیشنهادی — مناسب بیشتر دستگاه‌ها"),
    Build("q5_k_m", "nimbus-1.1-prime-ee", "qwen2.5-coder-7b-instruct-q5_k_m.gguf", 5_444_831_232,
          "586844eac4d6d6321689f0192c8aa8e69cd8625974a5cc2d925b1a03366e4d16", 16,
          "Largest — closest to the full-precision model",
          "بزرگ‌ترین — نزدیک‌ترین به مدل با دقت کامل"),
    Build("apex_q4_k_m", "nimbus-2-apex", "Qwen3-8B-Q4_K_M.gguf", 5_027_783_488,
          "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785", 12,
          "Recommended — the smallest build Qwen publish of this base",
          "پیشنهادی — کوچک‌ترین نسخه‌ای که Qwen از این پایه منتشر می‌کند"),
    Build("apex_q5_k_m", "nimbus-2-apex", "Qwen3-8B-Q5_K_M.gguf", 5_851_112_224,
          "068bae163faa96ad48032daf4e071a6a28fe67d8dcc95367609c2ff165e52738", 16,
          "Larger — a little closer to the full-precision model",
          "بزرگ‌تر — کمی نزدیک‌تر به مدل با دقت کامل"),
    Build("apex_q6_k", "nimbus-2-apex", "Qwen3-8B-Q6_K.gguf", 6_725_899_040,
          "cb042ccd76795a8830d6be6bd4165245847cc68e41797b13bd61aed4c2cfbce6", 20,
          "Largest — for a machine with memory to spare",
          "بزرگ‌ترین — برای دستگاهی که حافظه‌اش جا دارد"),
)

DEFAULT_BUILD = "apex_q4_k_m"


def build(key: str) -> Build:
    for item in BUILDS:
        if item.key == key:
            return item
    raise KeyError(f"no such build: {key!r}. have: {', '.join(b.key for b in BUILDS)}")


def builds_for(model_id: str) -> tuple[Build, ...]:
    return tuple(b for b in BUILDS if b.model == model_id)


def default_build_for(model_id: str) -> str:
    return builds_for(model_id)[0].key

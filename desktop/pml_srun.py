"""PML-sRUN: run a model bigger than the machine -- in pieces, each where it fits.

PML-sRUN was designed and built by AhoosAI (https://ahoos-ai.site).
Copyright (c) 2026 AhoosAI. MIT License (LICENSE): use it freely, and keep this notice.

The model is cut into pieces the way llama.cpp can place them: each layer's attention and router,
its feed-forward (dense) or its experts (MoE), the embeddings and the output. Each piece goes to the
fastest place it fits -- the graphics card's memory, the computer's memory, or the disk, read
through mmap when the computer's memory runs out -- and what the card cannot hold runs on the
processor. For a MoE model only a few experts work for each token, so the pieces that stay on the
disk are rarely all needed at once, and the operating system keeps the busy ones in memory by itself.
For a dense model every piece is needed for every token, so a small model of the same family
proposes several tokens and the big one checks them in one pass (speculative decoding): the pieces
are read once for several tokens.

    python srun.py setup [--kind auto|vulkan|cuda|cpu]   llama.cpp for this machine, into engine/
    python srun.py plan  MODEL.gguf [--ctx 4096] [--draft SMALL.gguf]
    python projects/pml-srun/srun.py bench MODEL.gguf [--ctx 4096] [--draft SMALL.gguf] [--tokens 128]
    python projects/pml-srun/srun.py serve MODEL.gguf [--port 8080]

`plan` reads the model and the machine and says where each piece would go and why; `bench` starts
llama.cpp's server with the plan and its neighbours, measures each, and keeps the fastest beside the
model (MODEL.gguf.srun.json); `serve` starts the kept one. The engine is llama.cpp's server: the one
`setup` put in engine/ beside this file, the AhoosAI studio's own (%LOCALAPPDATA%/AhoosAI Studio/bin),
or the one LLAMA_SERVER names. `python panel.py` opens the same in a browser (panl.html).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import struct
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

GIB = 2 ** 30
MIB = 2 ** 20

NAME = "PML-sRUN"
MAKER = "AhoosAI"
HOME = "https://ahoos-ai.site"
VERSION = "0.3"
BANNER = f"{NAME} {VERSION} -- by {MAKER} ({HOME})"


# -- the model, read -----------------------------------------------------------------------------------

@dataclass
class Model:
    path: Path
    meta: dict
    tensors: dict[str, int]                         # name -> bytes in the file

    @property
    def arch(self) -> str:
        return str(self.meta.get("general.architecture", "?"))

    def get(self, key: str, default=None):
        return self.meta.get(f"{self.arch}.{key}", default)

    @property
    def layers(self) -> int:
        return int(self.get("block_count", 0))

    @property
    def experts(self) -> int:
        return int(self.get("expert_count", 0) or 0)

    @property
    def experts_used(self) -> int:
        return int(self.get("expert_used_count", 0) or 0)

    @property
    def size(self) -> int:
        return sum(self.tensors.values())

    def piece(self, layer: int, kind: str) -> int:
        """Bytes of a layer's piece: 'experts' (MoE feed-forward), or 'rest' (everything else of it)."""
        out = 0
        for name, size in self.tensors.items():
            m = re.match(r"blk\.(\d+)\.(.+)", name)
            if not m or int(m.group(1)) != layer:
                continue
            is_expert = "_exps" in m.group(2)
            if (kind == "experts") == is_expert:
                out += size
        return out

    def outside_layers(self) -> dict[str, int]:
        return {n: s for n, s in self.tensors.items() if not n.startswith("blk.")}

    def kv_bytes_per_token(self, cache_bytes: float = 2.0) -> float:
        heads_kv = self.get("attention.head_count_kv", self.get("attention.head_count", 1))
        if isinstance(heads_kv, list):
            heads_kv = max(heads_kv)
        width = self.get("embedding_length", 0)
        heads = self.get("attention.head_count", 1)
        key = self.get("attention.key_length", width // max(1, heads))
        value = self.get("attention.value_length", key)
        return self.layers * int(heads_kv) * (int(key) + int(value)) * cache_bytes


def _read_str(f) -> str:
    (n,) = struct.unpack("<Q", f.read(8))
    return f.read(n).decode("utf-8", "replace")


_SCALAR = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}


def _read_value(f, kind: int):
    if kind in _SCALAR:
        fmt = _SCALAR[kind]
        return struct.unpack(fmt, f.read(struct.calcsize(fmt)))[0]
    if kind == 8:
        return _read_str(f)
    if kind == 9:
        inner, count = struct.unpack("<IQ", f.read(12))
        if inner in _SCALAR and count > 64:          # a long array (the vocabulary's scores): skipped
            f.seek(struct.calcsize(_SCALAR[inner]) * count, 1)
            return f"<{count} values>"
        if inner == 8 and count > 64:
            for _ in range(count):
                (n,) = struct.unpack("<Q", f.read(8))
                f.seek(n, 1)
            return f"<{count} strings>"
        return [_read_value(f, inner) for _ in range(count)]
    raise ValueError(f"GGUF value type {kind} not understood")


def read_gguf(path: Path) -> Model:
    """The metadata and each tensor's size (from the offsets: no table of quantization types needed)."""
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            raise ValueError(f"{path} is not a GGUF file")
        version, n_tensors, n_kv = struct.unpack("<IQQ", f.read(20))
        meta = {}
        for _ in range(n_kv):
            key = _read_str(f)
            (kind,) = struct.unpack("<I", f.read(4))
            meta[key] = _read_value(f, kind)
        infos = []
        for _ in range(n_tensors):
            name = _read_str(f)
            (dims,) = struct.unpack("<I", f.read(4))
            f.seek(8 * dims, 1)
            _, offset = struct.unpack("<IQ", f.read(12))
            infos.append((offset, name))
        align = int(meta.get("general.alignment", 32))
        start = (f.tell() + align - 1) // align * align
    end = path.stat().st_size - start
    infos.sort()
    sizes = {name: (infos[i + 1][0] if i + 1 < len(infos) else end) - offset for i, (offset, name) in enumerate(infos)}
    return Model(path, meta, sizes)


# -- the machine, measured ---------------------------------------------------------------------------

@dataclass
class Device:
    name: str
    label: str
    total: int
    free: int
    integrated: bool


@dataclass
class Machine:
    ram_total: int
    ram_free: int
    cores: int
    devices: list[Device] = field(default_factory=list)
    disk_read: float = 0.0                          # bytes a second, the model's own file

    @property
    def card(self) -> Device | None:
        """The graphics card the pieces go to: the discrete one with the most free memory -- or, on a Mac,
        Apple's own GPU (Metal), which shares the computer's memory and is still the fast way."""
        discrete = [d for d in self.devices if not d.integrated]
        if discrete:
            return max(discrete, key=lambda d: d.free)
        apple = [d for d in self.devices if re.search(r"(?i)apple|metal|mtl", d.name + " " + d.label)]
        return max(apple, key=lambda d: d.free) if apple else None


def memory() -> tuple[int, int]:
    if os.name == "nt":
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        status = Status()
        status.dwLength = ctypes.sizeof(Status)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        return status.ullTotalPhys, status.ullAvailPhys
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        info[key] = int(value.split()[0]) * 1024
    return info["MemTotal"], info.get("MemAvailable", info["MemFree"])


HERE = Path(__file__).resolve().parent
EXE = "llama-server.exe" if os.name == "nt" else "llama-server"


def server_binary() -> Path:
    """llama.cpp's server: LLAMA_SERVER, else the one `setup` put in engine/, else the AhoosAI studio's."""
    if os.environ.get("LLAMA_SERVER"):
        return Path(os.environ["LLAMA_SERVER"])
    found = sorted((HERE / "engine").rglob(EXE)) if (HERE / "engine").is_dir() else []
    if found:
        return found[0]
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "AhoosAI Studio" / "bin"
    if (base / EXE).is_file():
        return base / EXE
    raise SystemExit("llama-server was not found: run `python srun.py setup`, install the AhoosAI studio, "
                     "or set LLAMA_SERVER to its path")


# -- setup: llama.cpp for this machine -------------------------------------------------------------------

LLAMA_TAG = "b10991"                                # the build PML-sRUN was measured with
RELEASES = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_TAG}"


def engine_assets(kind: str = "auto") -> list[str]:
    """The release files for this system: Vulkan by default on Windows and Linux (any graphics card),
    Metal on macOS (built in), CUDA for an NVIDIA card when asked for, the processor alone when asked."""
    system, machine_ = platform.system(), platform.machine().lower()
    arm = machine_ in ("arm64", "aarch64")
    if system == "Darwin":
        return [f"llama-{LLAMA_TAG}-bin-macos-{'arm64' if arm else 'x64'}.tar.gz"]
    if kind == "auto":
        kind = "vulkan"
    if system == "Windows":
        if kind == "cuda":
            return [f"llama-{LLAMA_TAG}-bin-win-cuda-12.4-x64.zip", f"cudart-llama-bin-win-cuda-12.4-x64.zip"]
        return [f"llama-{LLAMA_TAG}-bin-win-{'cpu' if kind == 'cpu' else 'vulkan'}-{'arm64' if arm and kind == 'cpu' else 'x64'}.zip"]
    if kind == "cuda":
        return [f"llama-{LLAMA_TAG}-bin-ubuntu-cuda-12.8-x64.tar.gz", f"cudart-llama-{LLAMA_TAG}-bin-ubuntu-cuda-12.8-x64.tar.gz"]
    if kind == "cpu":
        return [f"llama-{LLAMA_TAG}-bin-ubuntu-{'arm64' if arm else 'x64'}.tar.gz"]
    return [f"llama-{LLAMA_TAG}-bin-ubuntu-vulkan-{'arm64' if arm else 'x64'}.tar.gz"]


def setup(kind: str = "auto", say=print) -> Path:
    """Download llama.cpp's release for this machine into engine/ and unpack it; the server's path."""
    import shutil
    import tarfile
    import zipfile

    target = HERE / "engine"
    target.mkdir(exist_ok=True)
    for name in engine_assets(kind):
        archive = target / name
        if not archive.exists():
            say(f"downloading {name}")
            request = urllib.request.Request(f"{RELEASES}/{name}", headers={"User-Agent": "PML-sRUN"})
            with urllib.request.urlopen(request, timeout=120) as response, open(archive.with_suffix(".part"), "wb") as out:
                total, done, last = int(response.headers.get("Content-Length") or 0), 0, 0.0
                while chunk := response.read(1 << 20):
                    out.write(chunk)
                    done += len(chunk)
                    if total and time.time() - last > 1:
                        last = time.time()
                        say(f"  {done / MIB:.0f} of {total / MIB:.0f} MB")
            archive.with_suffix(".part").replace(archive)
        say(f"unpacking {name}")
        root = target.resolve()
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.namelist():
                    if not (root / member).resolve().is_relative_to(root):
                        raise SystemExit(f"{name}: {member} would land outside engine/")
                bundle.extractall(root)
        else:
            with tarfile.open(archive) as bundle:
                for member in bundle.getmembers():
                    if not (root / member.name).resolve().is_relative_to(root) or member.islnk():
                        raise SystemExit(f"{name}: {member.name} would land outside engine/")
                try:
                    bundle.extractall(root, filter="data")  # no paths outside, no links out
                except TypeError:                           # a Python without extraction filters
                    bundle.extractall(root)
        archive.unlink()
    found = sorted(target.rglob(EXE))
    if not found:
        raise SystemExit("the download has no llama-server in it")
    if os.name != "nt":
        for f in found[0].parent.iterdir():
            if f.is_file() and not f.suffix:
                f.chmod(f.stat().st_mode | 0o111)
    say(f"llama-server: {found[0]}")
    return found[0]


INTEGRATED = re.compile(r"(?i)intel.*(hd|uhd|iris)|radeon\(tm\) graphics|vega \d+ graphics|apple")


def devices(binary: Path) -> list[Device]:
    """llama.cpp's own list (any backend), with each device's free memory."""
    out = subprocess.run([str(binary), "--list-devices"], capture_output=True, text=True, timeout=120,
                         env=engine_env()).stdout
    found = []
    for line in out.splitlines():
        m = re.match(r"\s*(\S+): (.+?) \((\d+) MiB, (\d+) MiB free\)", line)
        if m:
            found.append(Device(m.group(1), m.group(2), int(m.group(3)) * MIB, int(m.group(4)) * MIB,
                                bool(INTEGRATED.search(m.group(2)))))
    return found


def disk_read_speed(path: Path, chunk: int = 256 * MIB) -> float:
    """Bytes a second reading the model's own file from three places in it (an upper bound if the
    system already holds them in memory)."""
    size = path.stat().st_size
    total, started = 0, time.perf_counter()
    with open(path, "rb", buffering=0) as f:
        for frac in (0.31, 0.57, 0.83):
            f.seek(int(size * frac) // 4096 * 4096)
            left = chunk
            while left > 0:
                data = f.read(min(left, 16 * MIB))
                if not data:
                    break
                left -= len(data)
                total += len(data)
    return total / max(1e-6, time.perf_counter() - started)


def machine(model: Path | None = None, card_mib: int | None = None) -> Machine:
    total, free = memory()
    m = Machine(total, free, os.cpu_count() or 4)
    try:
        m.devices = devices(server_binary())
    except (OSError, subprocess.SubprocessError, SystemExit):
        m.devices = []
    if card_mib and m.card:                           # a smaller card than this one (to try a plan for it)
        m.card.free = min(m.card.free, card_mib * MIB)
    if model is not None:
        m.disk_read = disk_read_speed(model)
    return m


def physical_cores() -> int:
    try:
        if os.name == "nt":
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "(Get-CimInstance Win32_Processor | Measure-Object NumberOfCores -Sum).Sum"],
                                 capture_output=True, text=True, timeout=60).stdout
            return max(1, int(out.strip()))
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return max(1, (os.cpu_count() or 2) // 2)


# -- the plan ------------------------------------------------------------------------------------------

# The card's compute buffers and a margin, beside the pieces and the cache. Measured 2026-10-16 (GTX 1050 Ti,
# Vulkan, a 7B model, 16k tokens): 24 layers loaded with 605 MiB left, 25 did not with 499 -- 900 had kept
# three layers off the card, and the plan wrote at 6.5 tokens a second where 24 layers wrote at 7.4.
CARD_RESERVE = 600 * MIB
RAM_RESERVE = 2 * GIB           # what the system and the server keep for themselves


@dataclass
class Plan:
    argv: list[str]
    why: list[str]
    card_bytes: int
    ram_bytes: int
    disk_bytes: int
    estimate: float                                 # tokens a second, a first guess
    name: str = "plan"

    def describe(self) -> str:
        return "\n".join(self.why)


def plan(model: Model, box: Machine, ctx: int = 4096, draft: Path | None = None,
         experts_on_cpu: int | None = None, layers_on_card: int | None = None, draft_max: int = 8,
         ngram: bool = True) -> Plan:
    why = []
    card = box.card
    kv = model.kv_bytes_per_token(1.0625) * ctx                     # q8_0 cache
    budget = max(0, (card.free if card else 0) - CARD_RESERVE - int(kv))
    outside = model.outside_layers()
    output = sum(s for n, s in outside.items() if n.startswith("output"))
    argv = ["--ctx-size", str(ctx), "--flash-attn", "on", "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
            "--threads", str(physical_cores())]
    if card:
        argv += ["--device", card.name]
        why.append(f"card: {card.label}, {card.free / GIB:.1f} GB free; {kv / GIB:.2f} GB of it for a {ctx}-token cache "
                   f"(q8_0), {CARD_RESERVE / GIB:.1f} GB for its work -- {budget / GIB:.1f} GB for pieces")
    else:
        argv += ["--device", "none"]
        why.append("no graphics card to use: every piece on the processor")
    L = model.layers
    rest = [model.piece(i, "rest") for i in range(L)]
    experts = [model.piece(i, "experts") for i in range(L)]
    on_card = 0
    if model.experts and card:
        # MoE: every layer's attention, router and shared parts on the card -- read for every token --
        # and the experts of the last layers too, as far as the card goes; the others on the processor.
        base = sum(rest) + output
        if experts_on_cpu is None:
            experts_on_cpu = L
            room = budget - base
            while experts_on_cpu > 0 and room >= experts[experts_on_cpu - 1]:
                room -= experts[experts_on_cpu - 1]
                experts_on_cpu -= 1
        on_card = base + sum(experts[experts_on_cpu:])
        argv += ["--n-gpu-layers", "999", "--n-cpu-moe", str(experts_on_cpu)]
        why.append(f"MoE: {model.experts} experts a layer, {model.experts_used} at work for a token -- "
                   f"the {L} layers' shared pieces ({base / GIB:.1f} GB) on the card, the experts of "
                   f"{L - experts_on_cpu} layers there too, of {experts_on_cpu} on the processor")
        if base > budget:
            why.append(f"  (the shared pieces alone are {base / GIB:.1f} GB: more than the card's room -- llama.cpp will fail;"
                       " a smaller --ctx, or no card)")
    elif card:
        # Dense: the last layers on the card (llama.cpp offloads from the end), as many as fit.
        if layers_on_card is None:
            layers_on_card, room = 0, budget
            while layers_on_card < L and room >= rest[L - 1 - layers_on_card] + experts[L - 1 - layers_on_card]:
                room -= rest[L - 1 - layers_on_card] + experts[L - 1 - layers_on_card]
                layers_on_card += 1
            if layers_on_card == L and room >= output:
                layers_on_card += 1                 # the output too
        on_card = sum(rest[L - min(layers_on_card, L):]) + sum(experts[L - min(layers_on_card, L):]) + \
            (output if layers_on_card > L else 0)
        argv += ["--n-gpu-layers", str(layers_on_card)]
        why.append(f"dense: {min(layers_on_card, L)} of {L} layers on the card ({on_card / GIB:.1f} GB), the rest on the processor")
    cpu = model.size - on_card
    room_ram = max(0, box.ram_free - RAM_RESERVE)
    in_ram = min(cpu, room_ram)
    on_disk = cpu - in_ram
    why.append(f"processor's pieces: {cpu / GIB:.1f} GB, read from the file through mmap; the computer has "
               f"{box.ram_free / GIB:.1f} GB free -- " + (f"all of it stays in memory" if not on_disk else
               f"{on_disk / GIB:.1f} GB more than memory holds: those pieces come from the disk"))
    # A first guess of the speed: each token reads its pieces once, from where they are.
    ram_bw, card_bw = 18e9, 100e9
    disk_bw = box.disk_read or 2e9
    if model.experts:
        share = model.experts_used / max(1, model.experts)
        expert_cpu = sum(experts[:experts_on_cpu]) if experts_on_cpu is not None else 0
        per_token_cpu = (cpu - expert_cpu) + expert_cpu * share
        hot = min(1.0, room_ram / max(1, cpu))                     # the experts the system keeps in memory
        seconds = on_card * share / card_bw + per_token_cpu * hot / ram_bw + per_token_cpu * (1 - hot) / disk_bw
    else:
        seconds = on_card / card_bw + in_ram / ram_bw + on_disk / disk_bw
    estimate = 1 / max(1e-6, seconds)
    why.append(f"first guess: {estimate:.1f} tokens a second (before measuring)")
    if draft:
        # --spec-type: llama.cpp's default is none, and a draft model given without it is loaded and
        # never asked (b10991, docs/speculative.md); draft-simple is the plain small-model draft.
        argv += ["--spec-type", "draft-simple", "--spec-draft-model", str(draft), "--spec-draft-ngl", "all",
                 "--spec-draft-n-max", str(draft_max), "--spec-draft-n-min", "1"]
        why.append(f"speculative: {draft.name} proposes up to {draft_max} tokens, the model checks them in one pass")
    elif ngram and not model.experts:
        # No draft model: guesses from the text already there (llama.cpp's n-gram drafts). An answer that
        # repeats what it was shown -- an agent's edit -- came out at 10.5-10.9 tokens a second against
        # 7.4 without them (2026-10-16, results/), short answers within the measurement's noise.
        argv += ["--spec-type", "ngram-simple"]
        why.append("speculative: n-gram drafts from the text so far (no draft model needed)")
    return Plan(argv, why, on_card, in_ram, on_disk, estimate)


# -- measured --------------------------------------------------------------------------------------------

PROMPTS = [
    "Explain, step by step, how a hash map handles collisions, and when it should grow.",
    "Write a Python function that merges overlapping intervals, with a short docstring and two examples.",
    "توضیح بده چرا آسمان آبی است، به زبان ساده و در سه بند.",
]

ENGINE_ENV = {"GGML_VK_SUBALLOCATION_BLOCK_SIZE": str(512 * MIB)}     # the studio's: old drivers lose 1 GB blocks


def engine_env() -> dict:
    env = dict(os.environ)
    for k, v in ENGINE_ENV.items():
        env.setdefault(k, v)
    return env


def post(url: str, body: dict, timeout: float = 1800) -> dict:
    request = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def measure(model: Path, p: Plan, port: int = 8791, tokens: int = 128, log: Path | None = None,
            load_timeout: float = 3600, prompts: list[str] | None = None) -> dict:
    """Start llama-server with the plan, ask the prompts, and say how fast it wrote."""
    argv = [str(server_binary()), "--model", str(model), "--host", "127.0.0.1", "--port", str(port),
            "--no-webui", "--parallel", "1"] + p.argv
    log = log or model.with_suffix(".srun.log")
    started = time.time()
    with open(log, "a", encoding="utf-8") as out:
        out.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {p.name}: {' '.join(argv)}\n")
        out.flush()
        proc = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT, env=engine_env(),
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        while True:
            if proc.poll() is not None:
                return {"ok": False, "why": f"the server stopped while loading (exit {proc.returncode}); see {log.name}"}
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
                    if json.loads(r.read() or b"{}").get("status") == "ok":
                        break
            except (OSError, ValueError):
                pass
            if time.time() - started > load_timeout:
                return {"ok": False, "why": "the server did not load in time"}
            time.sleep(2)
        load = time.time() - started
        url = f"http://127.0.0.1:{port}/completion"
        post(url, {"prompt": "Hello", "n_predict": 8, "temperature": 0, "cache_prompt": False})     # warm
        runs = []
        for prompt in prompts or PROMPTS:
            # ignore_eos: every answer the same length, so a model that stops early does not look fast or slow
            answer = post(url, {"prompt": prompt, "n_predict": tokens, "temperature": 0, "cache_prompt": False,
                                "ignore_eos": True})
            t = answer.get("timings") or {}
            runs.append({"tok_s": t.get("predicted_per_second"), "prompt_tok_s": t.get("prompt_per_second"),
                         "tokens": t.get("predicted_n"), "draft": t.get("draft_n"),
                         "accepted": t.get("draft_n_accepted")})
        speeds = sorted(r["tok_s"] or 0 for r in runs)
        return {"ok": True, "load_s": round(load), "tok_s": round(speeds[len(speeds) // 2], 2), "runs": runs}
    except (OSError, ValueError) as error:
        return {"ok": False, "why": f"{type(error).__name__}: {error}"}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()


def candidates(model: Model, box: Machine, ctx: int, draft: Path | None) -> list[Plan]:
    """llama.cpp left to itself (what a launcher does today), the plan, its neighbours -- a piece more
    or less on the card, the guess of the card's room can be off -- and with the draft, a few lengths."""
    first = plan(model, box, ctx)
    default = Plan(["--ctx-size", str(ctx), "--device", box.card.name if box.card else "none"], ["llama.cpp's own fit"],
                   0, 0, 0, 0.0, name="llama.cpp alone")
    out = [default, first]
    if model.experts and box.card:
        n = int(first.argv[first.argv.index("--n-cpu-moe") + 1])
        for m in (n - 1, n - 2, n + 2):                # fewer experts on the processor first: the margin is tight
            if 0 <= m <= model.layers:
                q = plan(model, box, ctx, experts_on_cpu=m)
                q.name = f"--n-cpu-moe {m}"
                out.append(q)
    elif box.card:
        n = int(first.argv[first.argv.index("--n-gpu-layers") + 1])
        for m in (n + 1, n + 2, n - 2):                # more on the card first: a load that fails costs seconds
            if 0 <= m <= model.layers + 1:
                q = plan(model, box, ctx, layers_on_card=m)
                q.name = f"--n-gpu-layers {m}"
                out.append(q)
    first.name = "plan"
    if not draft and not model.experts:
        q = plan(model, box, ctx, ngram=False)
        q.name = "plan without n-gram drafts"
        out.append(q)
    if draft:
        for k in (4, 8, 16):
            q = plan(model, box, ctx, draft=draft, draft_max=k)
            q.name = f"plan + draft {k}"
            out.append(q)
    return out


# -- the commands ----------------------------------------------------------------------------------------

def profile_path(model_path: Path) -> Path:
    """Where bench keeps the fastest arrangement for a model: beside it."""
    return model_path.with_name(model_path.name + ".srun.json")


def kept_or_planned(model_path: Path, ctx: int, draft: Path | None = None, card_mib: int | None = None) -> list[str]:
    """The arguments serve uses: bench's fastest when there is one, else the plan."""
    kept = profile_path(model_path)
    if kept.exists():
        return json.loads(kept.read_text())["argv"]
    return plan(read_gguf(model_path), machine(model_path, card_mib), ctx, draft).argv


def bench(model_path: Path, ctx: int, draft: Path | None = None, tokens: int = 128, only: str | None = None,
          card_mib: int | None = None, say=print, stop=None) -> dict:
    """Each candidate started and timed; the fastest kept beside the model. `stop()` true ends early."""
    model = read_gguf(model_path)
    box = machine(model_path, card_mib)
    results = []
    for p in candidates(model, box, ctx, draft):
        if only and only not in p.name:
            continue
        if stop and stop():
            break
        say(f"-- {p.name}: {' '.join(p.argv)}")
        r = measure(model_path, p, tokens=tokens)
        say(f"   {r.get('tok_s', '-') if r.get('ok') else 'failed: ' + r.get('why', '')} "
            + ("tokens a second" if r.get("ok") else ""))
        results.append((p, r))
    good = [(p, r) for p, r in results if r.get("ok")]
    if not good:
        raise SystemExit("no arrangement ran: see the .srun.log beside the model")
    best, r = max(good, key=lambda pr: pr[1]["tok_s"])
    kept = {"argv": best.argv, "name": best.name, "measured": r,
            "all": [{"name": p.name, **{k: v for k, v in r.items() if k != "runs"}} for p, r in results],
            "machine": {"ram_gb": round(box.ram_total / GIB, 1), "card": box.card.label if box.card else None},
            "made": time.strftime("%Y-%m-%d %H:%M:%S"), "by": BANNER}
    profile_path(model_path).write_text(json.dumps(kept, indent=1))
    say(f"fastest: {best.name}, {r['tok_s']} tokens a second -- kept in {profile_path(model_path).name}")
    return kept


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], epilog=f"{NAME} is made by {MAKER}: {HOME}")
    ap.add_argument("command", choices=["setup", "plan", "bench", "serve"])
    ap.add_argument("model", type=Path, nargs="?")
    ap.add_argument("--kind", default="auto", choices=["auto", "vulkan", "cuda", "cpu"],
                    help="setup: which llama.cpp build (auto: Vulkan, or Metal on macOS)")
    ap.add_argument("--ctx", type=int, default=4096)
    ap.add_argument("--draft", type=Path)
    ap.add_argument("--tokens", type=int, default=128)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--only", help="bench only the candidates whose name holds this")
    ap.add_argument("--card-mib", type=int, help="plan for a card with this much free memory (MiB) at most")
    a = ap.parse_args()
    print(BANNER, flush=True)
    if a.command == "setup":
        setup(a.kind)
        return
    if a.model is None:
        ap.error("a model (.gguf) is needed")
    model = read_gguf(a.model)
    if a.command == "serve":
        argv = kept_or_planned(a.model, a.ctx, a.draft, a.card_mib)
        cmd = [str(server_binary()), "--model", str(a.model), "--host", "127.0.0.1", "--port", str(a.port)] + argv
        print(" ".join(cmd), flush=True)
        sys.exit(subprocess.call(cmd, env=engine_env()))
    box = machine(a.model, a.card_mib)
    print(f"model: {a.model.name}: {model.arch}, {model.layers} layers, {model.size / GIB:.1f} GB"
          + (f", MoE {model.experts} experts ({model.experts_used} a token)" if model.experts else ", dense"))
    print(f"machine: {box.ram_total / GIB:.1f} GB memory ({box.ram_free / GIB:.1f} free), {physical_cores()} cores, "
          f"the model's file read at {box.disk_read / GIB:.1f} GB/s; devices: "
          + ", ".join(f"{d.label} ({d.free / GIB:.1f} GB free{', shares memory' if d.integrated else ''})" for d in box.devices))
    if a.command == "plan":
        p = plan(model, box, a.ctx, a.draft)
        print(p.describe())
        print("llama-server " + " ".join(p.argv))
        return
    bench(a.model, a.ctx, a.draft, a.tokens, a.only, a.card_mib, say=lambda line: print(line, flush=True))


if __name__ == "__main__":
    main()

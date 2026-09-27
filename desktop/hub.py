"""A Hugging Face link, turned into files the app can download and run.

The person pastes whatever they have: a repository page, a file's page, a
download link, `owner/name`, or `owner/name:Q4_K_M` as llama.cpp and Ollama
write it. This finds the repository, asks the Hub what is in it, and says which
files are usable:

  a model     GGUF files, each quantisation one choice; a model split into
              `-00001-of-00003.gguf` parts is one choice made of three files
  an adapter  a GGUF LoRA, used as it is, or a PEFT adapter
              (adapter_config.json + adapter_model.safetensors), which is
              converted to GGUF on this computer after it arrives

Nothing is downloaded here except the Hub's listing and, for a PEFT adapter,
its small config files. The chosen files go to the download queue (jobs.py).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

HUB = "https://huggingface.co"
QUANT = re.compile(r"(?i)(?:^|[-_.])(i?q[1-8](?:_[0-9a-z]+)*|bf16|f16|f32|fp16|mxfp4)(?=$|[-_.])")
SHARD = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.I)


class HubError(RuntimeError):
    """The link is not a repository, or the Hub would not say what is in it."""


def parse(link: str) -> dict:
    """{repo, revision, path, quant} from anything that names a repository."""
    text = str(link or "").strip().strip("'\"<>")
    if not text:
        raise HubError("paste a Hugging Face link or an owner/name")
    quant = ""
    if "://" not in text and not text.startswith(("huggingface.co", "hf.co")):
        if ":" in text:
            text, quant = text.rsplit(":", 1)
        parts = [p for p in text.split("/") if p]
        if len(parts) != 2:
            raise HubError(f"'{link}' is not a link or an owner/name")
        return {"repo": "/".join(parts), "revision": "main", "path": "", "quant": quant}
    if "://" not in text:
        text = "https://" + text
    url = urllib.parse.urlsplit(text)
    if url.netloc.lower() not in ("huggingface.co", "www.huggingface.co", "hf.co"):
        raise HubError(f"{url.netloc} is not Hugging Face")
    parts = [urllib.parse.unquote(p) for p in url.path.split("/") if p]
    if parts and parts[0] in ("models",):
        parts = parts[1:]
    if parts and parts[0] in ("datasets", "spaces"):
        raise HubError("that is a dataset or a Space, not a model")
    if len(parts) < 2:
        raise HubError("the link does not name a repository")
    repo = f"{parts[0]}/{parts[1]}"
    if ":" in repo:
        repo, quant = repo.rsplit(":", 1)
    revision, path = "main", ""
    if len(parts) >= 4 and parts[2] in ("blob", "resolve", "tree"):
        revision = parts[3]
        path = "/".join(parts[4:]) if parts[2] != "tree" else ""
    return {"repo": repo, "revision": revision, "path": path, "quant": quant}


def _get(url: str, token: str = "") -> bytes:
    headers = {"User-Agent": "AhoosAI-Studio", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise HubError("the repository is private or does not exist; if it is yours, add a "
                           "Hugging Face token in Settings") from error
        if error.code == 403:
            raise HubError("the repository is gated: accept its terms on the Hub, then add a "
                           "Hugging Face token in Settings") from error
        if error.code == 404:
            raise HubError("there is no such repository or file on Hugging Face") from error
        raise HubError(f"Hugging Face answered {error.code}") from error
    except (urllib.error.URLError, OSError) as error:
        raise HubError(f"could not reach Hugging Face: {getattr(error, 'reason', error)}") from error


def file_url(repo: str, revision: str, path: str) -> str:
    return f"{HUB}/{repo}/resolve/{urllib.parse.quote(revision, safe='')}/{urllib.parse.quote(path)}"


def quant_of(name: str) -> str:
    stem = SHARD.sub("", name.rsplit("/", 1)[-1])
    stem = re.sub(r"\.gguf$", "", stem, flags=re.I)
    found = QUANT.findall(stem)
    return found[-1].upper() if found else ""


def info(repo: str, revision: str = "main", token: str = "") -> dict:
    path = f"/api/models/{repo}" + (f"/revision/{urllib.parse.quote(revision, safe='')}" if revision != "main" else "")
    return json.loads(_get(HUB + path + "?blobs=true", token))


def _files(meta: dict) -> list[dict]:
    files = []
    for sibling in meta.get("siblings") or []:
        name = sibling.get("rfilename") or ""
        lfs = sibling.get("lfs") or {}
        files.append({"path": name, "bytes": int(sibling.get("size") or lfs.get("size") or 0),
                      "sha256": lfs.get("sha256") or ""})
    return files


def _ggufs(files: list[dict]) -> list[dict]:
    """GGUF files as choices: shards of one model grouped into one choice."""
    groups: dict[str, dict] = {}
    for item in files:
        name = item["path"]
        if not name.lower().endswith(".gguf"):
            continue
        shard = SHARD.search(name)
        key = SHARD.sub("", name) if shard else name
        group = groups.setdefault(key, {"id": key, "name": key.rsplit("/", 1)[-1], "files": [], "bytes": 0,
                                        "quant": quant_of(name)})
        group["files"].append(item)
        group["bytes"] += item["bytes"]
    for group in groups.values():
        group["files"].sort(key=lambda f: f["path"])
        lowered = group["name"].lower()
        group["role"] = ("projector" if "mmproj" in lowered else
                         "adapter" if "lora" in lowered or "adapter" in lowered else "model")
        shard = SHARD.search(group["files"][0]["path"])
        if shard and len(group["files"]) != int(shard.group(2)):
            group["incomplete"] = True
    return sorted(groups.values(), key=lambda g: g["bytes"])


def _suggest(repo: str, token: str) -> list[str]:
    """GGUF versions of a repository that has none, as the Hub's search finds them."""
    name = repo.split("/")[-1]
    try:
        found = json.loads(_get(f"{HUB}/api/models?" + urllib.parse.urlencode(
            {"search": name, "filter": "gguf", "sort": "downloads", "limit": 6}), token))
    except (HubError, ValueError):
        return []
    return [m.get("id") or m.get("modelId") for m in found if m.get("id") or m.get("modelId")][:6]


def inspect(link: str, kind: str = "model", token: str = "") -> dict:
    """What at this link can be downloaded as a model (or as an adapter)."""
    ref = parse(link)
    meta = info(ref["repo"], ref["revision"], token)
    files = _files(meta)
    card = meta.get("cardData") or {}
    base_model = card.get("base_model") or ""
    if isinstance(base_model, list):
        base_model = base_model[0] if base_model else ""
    out = {"repo": meta.get("id") or ref["repo"], "revision": ref["revision"], "kind": kind,
           "gated": bool(meta.get("gated")), "private": bool(meta.get("private")),
           "base_model": base_model, "choices": [], "note": "", "suggestions": []}
    gguf_meta = meta.get("gguf") or {}
    if gguf_meta.get("architecture"):
        out["architecture"] = gguf_meta["architecture"]

    groups = _ggufs(files)
    if ref["path"]:
        wanted = SHARD.sub("", ref["path"])
        picked = [g for g in groups if g["id"] == wanted]
        groups = picked or groups
    if kind == "model":
        choices = [g for g in groups if g["role"] == "model"] or [g for g in groups if g["role"] != "projector"]
        for group in choices:
            group["format"] = "gguf"
        out["choices"] = choices
        if not choices:
            has_weights = any(f["path"].endswith((".safetensors", ".bin")) for f in files)
            out["note"] = ("This repository has no GGUF files, only weights in their training format, "
                           "which llama.cpp does not run. A GGUF version usually exists under another "
                           "name:" if has_weights else "There are no GGUF files in this repository.")
            out["suggestions"] = _suggest(out["repo"], token)
        _recommend(out["choices"], ref["quant"])
        return out

    # An adapter: a GGUF LoRA if there is one, a PEFT folder otherwise.
    ggufs = [g for g in groups if g["role"] != "projector"]
    for group in ggufs:
        group["format"] = "gguf"
    names = {f["path"]: f for f in files}
    peft = [p for p in names if p.endswith("adapter_config.json")]
    for config_path in peft:
        folder = config_path[: -len("adapter_config.json")]
        weights = names.get(folder + "adapter_model.safetensors")
        if not weights:
            continue
        choice = {"id": folder + "adapter_model.safetensors", "name": (folder.rstrip("/") or out["repo"].split("/")[-1]),
                  "format": "peft", "bytes": weights["bytes"], "quant": "",
                  "files": [names[config_path], weights]}
        try:
            config = json.loads(_get(file_url(out["repo"], ref["revision"], config_path), token))
            choice["rank"] = config.get("r")
            choice["alpha"] = config.get("lora_alpha")
            choice["base_model"] = config.get("base_model_name_or_path") or out["base_model"]
            out["base_model"] = out["base_model"] or choice["base_model"]
        except (HubError, ValueError):
            pass
        ggufs.append(choice)
    out["choices"] = ggufs
    if not ggufs:
        out["note"] = ("No adapter here: neither a GGUF LoRA nor adapter_config.json with "
                       "adapter_model.safetensors.")
    _recommend(out["choices"], ref["quant"])
    return out


def _recommend(choices: list[dict], wanted: str) -> None:
    """Mark the one most people should take: the one the link named, else Q4_K_M."""
    if not choices:
        return
    order = [wanted.upper()] if wanted else []
    order += ["Q4_K_M", "Q4_K_S", "Q5_K_M", "Q4_0", "IQ4_XS", "Q6_K", "Q8_0"]
    for quant in order:
        for choice in choices:
            if choice.get("quant") == quant:
                choice["recommended"] = True
                return
    choices[0]["recommended"] = True


def base_config(repo: str, token: str = "") -> dict:
    """A base model's config.json -- two numbers the adapter converter needs."""
    return json.loads(_get(file_url(repo, "main", "config.json"), token))

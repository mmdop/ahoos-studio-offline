<p align="center">
  <b>AhoosAI Studio 3.2.1 — a faster first answer</b><br>
  Still on your own computer, still no account.
</p>

<p align="center">
  <a href="https://ahoos-ai.site/int-offline-studio">About the app</a> ·
  <a href="https://huggingface.co/AhoosAI/nimbus-2-apex">Model on Hugging Face</a>
</p>

---

## What's new in 3.2.1

**The first answer after loading is no longer the slow one.** A graphics card builds part of its work the first time it is used — CUDA cards compile their kernels, Vulkan cards build their pipelines — and that used to happen inside your first message. On AhoosAI's own online engine it made the first answer crawl at 1.5 words a second while the next one ran at over a hundred. Now the studio gives the model one short task of its own as soon as it has loaded, so that work is done while the studio still says it is loading, and your first message is answered at full speed.

**CUDA builds keep what they compiled.** With the CUDA engine, the compiled kernels are kept in the studio's own folder, with room enough for all of them, so a later start does not compile them again. On the online engine the same change took loading from 53 seconds to 7.

The same fix is live on AhoosAI's online engine: after a quiet spell, the first word of an answer now comes in about 20 seconds instead of two minutes.

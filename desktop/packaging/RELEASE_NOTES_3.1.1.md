<p align="center">
  <b>AhoosAI Studio 3.1 — twice as fast on a small graphics card, and any model you have a key for</b><br>
  Still on your own computer, still no account. Now with installers for Windows and Linux.
</p>

<p align="center">
  <a href="https://ahoos-ai.site/int-offline-studio">About the app</a> ·
  <a href="https://huggingface.co/AhoosAI/nimbus-2-apex">Model on Hugging Face</a>
</p>

---

## What's new in 3.1

**PML-sRUN places the model.** A model that does not fit on your graphics card used to be split the way llama.cpp guessed. Now PML-sRUN — AhoosAI's own placement — reads the model and your machine and puts each piece where it runs fastest: as many layers as the card really holds, the rest on the processor, the conversation's memory compressed. On a laptop with a 4 GB GTX 1050 Ti and a 7B coding model: **7.4 tokens a second, against 3.6** for llama.cpp's own placement and 4.9 for Ollama on the same file. When an answer repeats what it was shown — editing a file — it guesses ahead from the text already there and checks the guesses in one pass: **10.2 tokens a second** on edits, against 2.8. If the plan does not load on your card, the studio tries it with two layers fewer, then llama.cpp's own placement, then the processor alone. Settings shows the plan and can turn it off.

**Online models, with every tool.** Settings › Online models takes an API key — AhoosAI's own (Nimbus 2 Apex), OpenAI, OpenRouter, Groq, Google Gemini, DeepSeek, or any service that speaks OpenAI's API — and the model picker lists them beside the local ones. An online model works exactly as a local one does: it plans, writes whole files, edits, runs commands, searches the web, checks its work, and every change can be undone. Your key stays on your computer, and requests go through your own connection (or the proxy you set).

**A composer that groups.** The tools, the mode (normal or plan) and the two dials sit in one row; a glass beside them shows how much of the model's memory the conversation fills. **Compact** summarises the conversation for the model itself, so a long session keeps going; it happens on its own near the end.

**Answers that show more than text.** Tables can be read across or down and copied as CSV. A model can draw bar, line, area, pie and horizontal-bar charts from its numbers. Long answers come in sections, numbered steps, notes, tips and warnings, and parts most readers can skip fold away.

**Thinking in steps.** Nimbus 2 Apex thinks before it answers and thinks again after every file it reads and every search, and you can open each thought.

**It knows today.** The model is told the date — in both calendars — and a question about now (news, prices, today) begins with a web search when the internet is on.

**Also:**
- On a Mac, PML-sRUN puts the model on Apple's own GPU (Metal), which shares the computer's memory and is still the fast way.
- The chat follows a long answer as it grows; scroll up to read and it stays where you are, scroll back to the end and it follows again.
- Installers: a Windows installer (for your user, no administrator needed) and a `.deb` for Debian and Ubuntu, beside the `.zip`, `.tar.gz` and macOS `.dmg`.

## Downloads

| | |
|---|---|
| Windows (installer) | `AhoosAI-Studio-3.1.1-windows-x64-setup.exe` |
| Windows (portable) | `AhoosAI-Studio-3.1.1-windows-x64.zip` |
| macOS, Apple silicon | `AhoosAI-Studio-3.1.1-macos-arm64.dmg` |
| macOS, Intel | `AhoosAI-Studio-3.1.1-macos-x64.dmg` |
| Linux, Debian/Ubuntu | `AhoosAI-Studio-3.1.1-linux-x64.deb` |
| Linux, other | `AhoosAI-Studio-3.1.1-linux-x64.tar.gz` |

Check your download against `SHA256SUMS.txt`.

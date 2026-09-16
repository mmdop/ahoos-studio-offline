<p align="center">
  <img src="desktop/assets/icon.png" alt="AhoosAI" width="120">
</p>

<h1 align="center">AhoosAI Studio — Offline</h1>

<p align="center">
  Run AhoosAI models on your own computer.<br>
  No account, no sign-in, and no internet after a one-time model download.
</p>

<p align="center">
  <a href="../../releases/latest"><b>Download</b></a> ·
  <a href="https://ahoos-ai.site">ahoos-ai.site</a> ·
  <a href="https://ahoos-ai.site/nimbus-1-1-prime-benchmarks.html">Benchmarks</a> ·
  <a href="https://huggingface.co/AhoosAI/nimbus-1-1-prime-ee">Model on Hugging Face</a>
</p>

---

This is the offline edition of AhoosAI Studio, for using AhoosAI's models without
a connection. The model runs on your machine through
[llama.cpp](https://github.com/ggml-org/llama.cpp); your conversations and the
files it writes never leave it.

The model is **Nimbus 1.1 Prime-EE** — the first model of AhoosAI's own, a
behaviour adapter over Qwen2.5-Coder-7B-Instruct. It answers in the language you
write in, is at home in Persian and right-to-left layouts, and was trained out of
habits like hard-coding `dir="ltr"`, form controls with no label, and containers
that run as root.

## Download

From [Releases](../../releases/latest):

| System | File |
|---|---|
| Windows 10 / 11 (64-bit) | `AhoosAI-Studio-…-windows-x64.zip` |
| macOS, Apple silicon | `AhoosAI-Studio-…-macos-arm64.dmg` |
| macOS, Intel | `AhoosAI-Studio-…-macos-x64.dmg` |
| Linux (64-bit) | `AhoosAI-Studio-…-linux-x64.tar.gz` |

Unpack it and run **AhoosAI Studio**. On first start it asks which size of model
to download:

| Size | Download | Memory |
|---|---|---|
| q3_k_m | 3.8 GB | 8 GB |
| **q4_k_m — recommended** | 4.7 GB | 12 GB |
| q5_k_m | 5.4 GB | 16 GB |

The model comes directly from Qwen's repository on Hugging Face, resumes if the
connection drops, and is checked against its published checksum. After that the
app needs no internet.

## Security warnings on first run

AhoosAI Studio is not yet code-signed, so Windows and macOS may warn you before
it runs. The warning is about the missing signature, not about the app. The app
is safe to use: its complete source is in this repository, it talks only to your
own computer and — for the one-time model download — to Hugging Face, and every
release lists the SHA-256 checksum of its files so you can confirm yours is the
one published here.

- **Windows — "Windows protected your PC":** click **More info**, then **Run anyway**.
- **Windows 11 with Smart App Control on:** the app may be blocked outright, with
  no option to continue. This is Windows refusing any unsigned program; it will
  not run until the app is signed.
- **macOS — "cannot be opened because the developer cannot be verified":**
  right-click the app, choose **Open**, then **Open** again. Or allow it under
  System Settings → Privacy & Security.
- **Linux:** mark the program as executable if your file manager asks
  (`chmod +x ahoos-studio`). Without WebKitGTK the studio opens in your browser
  instead of its own window, and works the same.

To check a download on Windows: `certutil -hashfile <file> SHA256`; on macOS or
Linux: `shasum -a 256 <file>`. Compare with the checksum in the release notes.

## What it does

- Conversations, with history kept on your computer
- Files the model writes, saved to disk and never expired
- Effort levels, from Low to Max
- `@CR-file`, to get complete files rather than code in the chat
- Skill chips — create, import and switch them
- English and Persian, left-to-right and right-to-left

**Not supported:** images, web search and team mode. Every message is answered
by the one model on your computer.

Everything is stored in your user data folder: `%LOCALAPPDATA%\AhoosAI Studio` on
Windows, `~/Library/Application Support/AhoosAI Studio` on macOS,
`~/.local/share/AhoosAI Studio` on Linux. To keep it beside the program instead —
on a USB drive, say — put an empty file named `portable.txt` next to it.

---

<div dir="rtl">

## فارسی

این نسخه‌ی آفلاین اهوس‌ای‌آی استودیو است، برای استفاده از مدل‌های اهوس‌ای‌آی بدون
اینترنت. مدل روی همان کامپیوتر اجرا می‌شود؛ گفتگوها و فایل‌هایی که می‌نویسد از
دستگاه شما خارج نمی‌شوند. نه حساب کاربری لازم است و نه ورود.

مدل آن **نیمبوس ۱.۱ پرایم** است — نخستین مدل اختصاصی اهوس‌ای‌آی. به همان زبانی جواب
می‌دهد که نوشته‌اید و در فارسی و چیدمان راست‌به‌چپ راحت است.

**نصب:** فایل سیستم‌عامل خود را از بخش [Releases](../../releases/latest) بگیرید، باز
کنید و **AhoosAI Studio** را اجرا کنید. بار اول می‌پرسد کدام اندازه‌ی مدل دانلود شود
(۳٫۸ تا ۵٫۴ گیگابایت). یک بار دانلود می‌شود و بعد از آن اینترنت لازم نیست.

**هشدار امنیتی هنگام اجرا:** برنامه هنوز امضای دیجیتال ندارد، برای همین ویندوز
یا مک ممکن است قبل از اجرا هشدار امنیتی بدهند. این هشدار به‌خاطر نداشتن امضاست،
نه به‌خاطر خود برنامه، و می‌توانید ردش کنید. برنامه امن است: کد کاملش در همین
مخزن است، فقط با کامپیوتر خودتان و — برای دانلود یک‌باره‌ی مدل — با هاگینگ‌فیس
ارتباط دارد، و چک‌سام SHA-256 هر فایل در توضیح ریلیز آمده تا مطمئن شوید فایلتان
همان است که اینجا منتشر شده.

- **ویندوز («Windows protected your PC»):** روی **More info** و بعد **Run anyway** بزنید.
- **ویندوز ۱۱ با Smart App Control روشن:** ممکن است برنامه بدون گزینه‌ی ادامه
  بلاک شود. ویندوز هر برنامه‌ی بدون امضا را این‌طور رد می‌کند و تا امضا نشود اجرا نمی‌شود.
- **مک:** روی برنامه راست‌کلیک کنید، **Open** و دوباره **Open** را بزنید.

**پشتیبانی نمی‌شود:** تصویر، جست‌وجوی وب و حالت تیمی. همه‌ی پیام‌ها را همان یک مدل
روی کامپیوتر شما جواب می‌دهد.

</div>

---

## Building from source

```bash
pip install -r desktop/packaging/requirements.txt
python desktop/packaging/build.py
```

builds the package for the machine it runs on. The adapter is converted from the
published weights on Hugging Face during the build. `.github/workflows/desktop.yml`
builds all four packages; pushing a tag like `desktop-v1.1.0` attaches them to a
draft release.

To work on the app without building it:

```bash
python -m desktop fetch-runtime            # llama.cpp for this machine
python tools/adapter_to_gguf.py            # the adapter
python -m desktop run --browser            # the studio
python -m desktop run --engine echo --browser   # the interface, with no model
```

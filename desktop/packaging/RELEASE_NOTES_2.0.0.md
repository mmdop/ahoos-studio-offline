<p align="center">
  <b>AhoosAI Studio 2.0 — Nimbus 2 Apex, on your own computer</b><br>
  No account, no sign-in, and no internet after a one-time model download.
</p>

<p align="center">
  <a href="https://ahoos-ai.site/int-offline-studio">About the app</a> ·
  <a href="https://ahoos-ai.site/nimbus-2-apex-benchmarks.html">How the model was measured</a> ·
  <a href="https://huggingface.co/AhoosAI/nimbus-2-apex">Model on Hugging Face</a>
</p>

---

## What's new in 2.0

**A second model: Nimbus 2 Apex.** An adapter over Qwen3-8B, trained to do exactly as it is told. Against the weights it was trained on — question by question, in the same session — instruction-following (IFEval) rises from 73.2 to **81.9**, and nothing else moves in a way that survives a test of significance. That is the whole claim, at its real size. Nimbus 1.1 Prime-EE stays beside it, because it runs in 8 GB of memory and Apex does not. Switching between them is one click.

**Two dials the model was trained under.**
- **Thinking level, 1 to 20** — a word target for the reasoning, with a ceiling that closes the thought when the budget runs out. The model learned these targets, so the level is a control, not a hint.
- **Temperature, 1 to 10** — each step is a whole sampling profile (temperature, top-p, top-k, repetition penalty), not one number moved.

**Memory between messages.** Off, four turns, or twelve. Off is the default: every message starts clean unless you ask it to remember.

**A working folder, and a gate in front of it.** Choose a folder from your system's own dialog. Before the model writes a file or runs a command, you see exactly what will happen, and nothing happens until you allow it. Any path that climbs out of the folder is refused, not repaired. There is no list of forbidden commands — such a list looks like safety and is not.

**A terminal** inside the app, in that folder, and a button that opens your system's own terminal there.

**Plan or Normal.** In plan mode the model writes down what it would do and waits for you.

**Pictures, without vision weights.** An image is turned into an XML description of the scene — shapes, positions, colours, and the text in it — and the model reads that as text. It reads about half the charts put in front of it correctly and does not reliably know when it has the other half wrong, so check anything that matters.

**Builds for every platform.** Windows, macOS (Apple silicon and Intel) and Linux, all built by the public workflow in this repository from the code you can read here.

## Download

| System | File | Size |
|---|---|---|
| Windows 10 / 11, 64-bit | `AhoosAI-Studio-2.0.0-windows-x64.zip` | 282 MB |
| macOS, Apple silicon (M1 and later) | `AhoosAI-Studio-2.0.0-macos-arm64.dmg` | 290 MB |
| macOS, Intel | `AhoosAI-Studio-2.0.0-macos-x64.dmg` | 293 MB |
| Linux, x86-64 (glibc 2.31+) | `AhoosAI-Studio-2.0.0-linux-x64.tar.gz` | 341 MB |

Unpack it and run **AhoosAI Studio**. On first start it asks which model and which size to download. The base weights come straight from Qwen's own repository on Hugging Face, resume if the connection drops, and are checked against their published checksum. Only our adapters ship inside the package. After that one download, the app never touches the network again.

| Model | Size | Download | Memory |
|---|---|---|---|
| **Nimbus 2 Apex** | **q4_k_m — recommended** | 5.0 GB | 12 GB |
| | q5_k_m | 5.9 GB | 16 GB |
| | q6_k | 6.7 GB | 20 GB |
| **Nimbus 1.1 Prime-EE** | q3_k_m | 3.8 GB | 8 GB |
| | **q4_k_m — recommended** | 4.7 GB | 12 GB |
| | q5_k_m | 5.4 GB | 16 GB |

A graphics card is optional; both models run on the processor, more slowly.

## Check your download

Every file's SHA-256 is in `SHA256SUMS.txt`, attached below.

- Windows: `certutil -hashfile AhoosAI-Studio-2.0.0-windows-x64.zip SHA256`
- macOS / Linux: `shasum -a 256 AhoosAI-Studio-2.0.0-*`

## Security warnings on first run

The app is not yet code-signed, so your system may warn you. The warning is about the missing signature, not about the app.

- **Windows, "Windows protected your PC":** click **More info**, then **Run anyway**. With Smart App Control on, Windows 11 may refuse it outright until the app is signed.
- **macOS, "cannot be opened because the developer cannot be verified":** right-click the app, choose **Open**, then **Open** again — or allow it under System Settings → Privacy & Security.
- **Linux:** mark `ahoos-studio` executable if asked. Without WebKitGTK the studio opens in your browser instead of its own window, and works the same.

## Not in the offline edition

Web search and team mode. Every message is answered by the one model on your computer.

## Upgrading from 1.1

Download the new package and run it in place of the old one. Conversations, files and settings live in your user data folder, not beside the program, and 2.0 uses the same folder.

---

<div dir="rtl">

## چه چیزی در نسخه‌ی ۲ تازه است

**مدل دوم: نیمبوس ۲ ایپکس.** آداپتوری روی Qwen3-8B که آموزش دیده دقیقاً همان کاری را بکند که گفته شده. در برابر همان وزن‌هایی که رویشان آموزش دیده — پرسش‌به‌پرسش و در یک نشست — دنبال‌کردن دستور (IFEval) از ۷۳٫۲ به **۸۱٫۹** می‌رسد و هیچ چیز دیگری آن‌قدر تکان نمی‌خورد که از آزمون معناداری رد شود. ادعا همین است، به همین اندازه‌ی واقعی. نیمبوس ۱٫۱ پرایم هم کنارش می‌ماند، چون در ۸ گیگابایت حافظه اجرا می‌شود و ایپکس نمی‌شود. جابه‌جایی بینشان یک کلیک است.

**دو دستگیره‌ای که مدل با آن‌ها آموزش دیده.**
- **سطح تفکر، ۱ تا ۲۰** — هدفی برای تعداد کلمه‌های استدلال، با سقفی که وقتی بودجه تمام شد فکر را می‌بندد. مدل این هدف‌ها را یاد گرفته، پس سطح یک کنترل واقعی است، نه پیشنهاد.
- **دما، ۱ تا ۱۰** — هر پله یک پروفایل کامل نمونه‌برداری است، نه یک عدد جابه‌جاشده.

**حافظه بین پیام‌ها.** خاموش، چهار نوبت یا دوازده نوبت. پیش‌فرض خاموش است: هر پیام از صفر شروع می‌شود مگر بخواهید به یاد بسپارد.

**یک پوشه‌ی کاری، و یک دروازه جلویش.** پوشه را از پنجره‌ی خود سیستم‌عامل انتخاب می‌کنید. پیش از اینکه مدل فایلی بنویسد یا دستوری اجرا کند، دقیقاً می‌بینید چه اتفاقی می‌افتد و تا اجازه ندهید هیچ اتفاقی نمی‌افتد. مسیری که از پوشه بیرون بزند رد می‌شود، نه اصلاح. فهرست دستورهای ممنوع در کار نیست — چنین فهرستی شبیه امنیت است و امنیت نیست.

**ترمینال** داخل برنامه، در همان پوشه، و دکمه‌ای که ترمینال خود سیستم را همان‌جا باز می‌کند.

**حالت پلن یا عادی.** در حالت پلن، مدل می‌نویسد چه می‌خواهد بکند و منتظر شما می‌ماند.

**تصویر، بدون وزن‌های بینایی.** عکس به توصیفی XML از صحنه تبدیل می‌شود — شکل‌ها، جایشان، رنگ‌ها و متن داخلش — و مدل آن را مثل متن می‌خواند. حدود نیمی از نمودارها را درست می‌خواند و همیشه نمی‌فهمد نیم دیگر را اشتباه خوانده، پس هرچه مهم است را خودتان بررسی کنید.

**نسخه برای همه‌ی سیستم‌عامل‌ها.** ویندوز، مک (اپل سیلیکون و اینتل) و لینوکس — همه را workflow عمومی همین مخزن از همین کد ساخته است.

## دانلود

فایل سیستم‌عامل خود را از پایین همین صفحه بگیرید، باز کنید و **AhoosAI Studio** را اجرا کنید. بار اول می‌پرسد کدام مدل و کدام اندازه دانلود شود: ایپکس ۵٫۰ تا ۶٫۷ گیگابایت (حافظه‌ی ۱۲ گیگابایت به بالا)، و ۱٫۱ از ۳٫۸ تا ۵٫۴ گیگابایت (از ۸ گیگابایت). مدل پایه مستقیم از مخزن خود Qwen می‌آید، اگر ارتباط قطع شود ادامه می‌دهد، و با چک‌سام منتشرشده‌اش بررسی می‌شود. بعد از همان یک بار، برنامه دیگر به اینترنت کاری ندارد. کارت گرافیک لازم نیست؛ بدون آن کندتر اجرا می‌شود.

چک‌سام SHA-256 همه‌ی فایل‌ها در `SHA256SUMS.txt` پایین همین صفحه است.

## هشدار امنیتی هنگام اجرا

برنامه هنوز امضای دیجیتال ندارد، برای همین سیستم ممکن است هشدار بدهد. هشدار به‌خاطر نداشتن امضاست، نه به‌خاطر خود برنامه.

- **ویندوز:** روی **More info** و بعد **Run anyway** بزنید. اگر Smart App Control روشن باشد، ویندوز ۱۱ ممکن است تا امضا نشدن برنامه اجرایش نکند.
- **مک:** روی برنامه راست‌کلیک کنید، **Open** و دوباره **Open** را بزنید.
- **لینوکس:** اگر لازم شد `ahoos-studio` را اجراشدنی کنید.

## در نسخه‌ی آفلاین نیست

جست‌وجوی وب و حالت تیمی. همه‌ی پیام‌ها را همان یک مدل روی کامپیوتر شما جواب می‌دهد.

## به‌روزرسانی از ۱٫۱

بسته‌ی جدید را دانلود کنید و به‌جای نسخه‌ی قبلی اجرا کنید. گفتگوها، فایل‌ها و تنظیمات در پوشه‌ی داده‌ی کاربر شما هستند، نه کنار برنامه، و نسخه‌ی ۲ از همان پوشه استفاده می‌کند.

</div>

---

<p align="center"><sub>© 2026 AhoosAI · <a href="https://ahoos-ai.site">ahoos-ai.site</a></sub></p>

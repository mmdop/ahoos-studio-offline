<p align="center">
  <b>AhoosAI Studio 2.5 — a model that does the work</b><br>
  Still on your own computer, still no account. The internet only if you turn it on.
</p>

<p align="center">
  <a href="https://ahoos-ai.site/int-offline-studio">About the app</a> ·
  <a href="https://ahoos-ai.site/nimbus-2-apex-benchmarks.html">How the model was measured</a> ·
  <a href="https://huggingface.co/AhoosAI/nimbus-2-apex">Model on Hugging Face</a>
</p>

---

## What's new in 2.5

**It acts instead of describing.** Connect a folder and ask for something: the model lists and reads what is there, writes and edits files, runs commands, reads what they printed, and then tells you what it did. Before 2.5, asking a small model to "make hello.py and run it" got you a code block to copy — and once, the invented output of a run that never happened. Now each step of its turn is a single decision, written as one well-formed call: a tool and its arguments, or its answer. On Nimbus 1.1 at its smallest size, on a four-core laptop, that request now ends with the file on disk, `hello world` from running it, and one sentence saying so.

**It knows where it is.** The model is told it is running inside AhoosAI Studio on your computer, which folder it may touch, which shell your system uses, and whether the internet is on — so it stops saying it cannot see your files when it can, and tells you to connect a folder when it cannot.

**The same gate.** Reading is free. Every write, delete and command is shown to you first — the diff, or the command exactly as it will run — and nothing happens until you allow it. A command that never ends (a dev server, a watcher) is stopped after a time limit you set, along with everything it started.

**The internet, if you want it.** Off by default. In Settings, choose one of:
- **your computer's own connection** — no key, through your VPN or proxy;
- **an API key** for Tavily, Brave Search, Exa, Serper or Jina;
- **any other search API**, described by its address, its key and where its results are.

The model can then search and read pages. Addresses on your own computer or local network are refused unless you allow them, so a web page cannot send the model there.

**Battle.** One prompt, two answers: the base model, and the same model with its adapter. Run them together, or one after the other with the whole machine each. Vote — blind if you like, with the names hidden until you do — and the sidebar keeps the score. Both run on one copy of the weights: the adapter is simply applied at strength zero for the base.

**Any model from Hugging Face.** In Models, paste a link to a model and, if you want, a link to an adapter. The app reads the repository and lists what you can run — every quantisation, split files grouped, the recommended size marked — and your choice goes to a download queue that resumes and verifies like every other download. An adapter in PEFT form (`adapter_config.json` + `adapter_model.safetensors`) is converted to GGUF on your computer. Any base model can be paired with any adapter made for its architecture.

**A new page.** Rebuilt for the desktop, in the studio's classic look, in English and Persian.

## Download

| System | File |
|---|---|
| Windows 10 / 11, 64-bit | `AhoosAI-Studio-2.5.0-windows-x64.zip` |
| macOS, Apple silicon (M1 and later) | `AhoosAI-Studio-2.5.0-macos-arm64.dmg` |
| macOS, Intel | `AhoosAI-Studio-2.5.0-macos-x64.dmg` |
| Linux, x86-64 | `AhoosAI-Studio-2.5.0-linux-x64.tar.gz` |

Unpack it and run **AhoosAI Studio**. With no model yet, it opens on the Models page: pick a Nimbus model and a size, or paste a Hugging Face link.

## Check your download

Every file's SHA-256 is in `SHA256SUMS.txt`, attached below.

- Windows: `certutil -hashfile AhoosAI-Studio-2.5.0-windows-x64.zip SHA256`
- macOS / Linux: `shasum -a 256 AhoosAI-Studio-2.5.0-*`

## Security warnings on first run

The app is not yet code-signed, so your system may warn you. The warning is about the missing signature, not about the app.

- **Windows, "Windows protected your PC":** click **More info**, then **Run anyway**. With Smart App Control on, Windows 11 may refuse it outright until the app is signed.
- **macOS, "cannot be opened because the developer cannot be verified":** right-click the app, choose **Open**, then **Open** again.
- **Linux:** mark `ahoos-studio` executable if asked. Without WebKitGTK the studio opens in your browser instead of its own window.

## Not in 2.5

Team mode, and pictures: the new page takes text files only for now.

## Upgrading from 2.0

Run the new package in place of the old one. Your models, settings and conversations stay where they were; conversations from 2.0 are brought into the new page the first time it opens.

---

<div dir="rtl">

## چه چیزی در نسخه‌ی ۲٫۵ تازه است

**کار را انجام می‌دهد، نه اینکه توضیحش بدهد.** یک پوشه وصل کنید و چیزی بخواهید: مدل فایل‌های آنجا را می‌بیند و می‌خواند، فایل می‌سازد و ویرایش می‌کند، دستور اجرا می‌کند، خروجی‌اش را می‌خواند و بعد می‌گوید چه کرد. پیش از ۲٫۵ اگر از یک مدل کوچک می‌خواستید «hello.py بساز و اجرایش کن»، یک تکه کد برای کپی تحویل می‌گرفتید — و یک بار هم خروجی ساختگیِ اجرایی که هرگز انجام نشده بود. حالا هر گام مدل یک تصمیم است که به شکل یک فراخوانی درست نوشته می‌شود: یک ابزار با ورودی‌هایش، یا پاسخ نهایی. روی نیمبوس ۱٫۱ در کوچک‌ترین اندازه و روی یک لپ‌تاپ چهار هسته‌ای، همان درخواست با فایلی روی دیسک، `hello world` از اجرای آن، و یک جمله‌ی گزارش تمام می‌شود.

**می‌داند کجاست.** به مدل گفته می‌شود داخل استودیوی اهوس روی کامپیوتر شما اجرا می‌شود، به کدام پوشه دسترسی دارد، سیستم شما از چه شِلی استفاده می‌کند و اینترنت روشن است یا نه — پس دیگر نمی‌گوید فایل‌های شما را نمی‌بیند وقتی می‌بیند، و وقتی نمی‌بیند می‌گوید پوشه وصل کنید.

**همان دروازه.** خواندن آزاد است. هر نوشتن، حذف و اجرای دستور اول به شما نشان داده می‌شود — تغییرات فایل، یا دستور دقیقاً همان‌طور که اجرا می‌شود — و تا اجازه ندهید هیچ اتفاقی نمی‌افتد. دستوری که تمام نمی‌شود (مثل یک سرور) بعد از مهلتی که خودتان تعیین می‌کنید، همراه هر چیزی که راه انداخته، متوقف می‌شود.

**اینترنت، اگر بخواهید.** پیش‌فرض خاموش است. در تنظیمات یکی را انتخاب کنید:
- **اتصال خود کامپیوتر** — بدون کلید، از طریق VPN یا پراکسی شما؛
- **کلید API** برای Tavily، Brave Search، Exa، Serper یا Jina؛
- **هر سرویس جستجوی دیگر** با آدرس، کلید و محل نتایجش.

آن‌وقت مدل می‌تواند جستجو کند و صفحه بخواند. آدرس‌های روی خود کامپیوتر یا شبکه‌ی محلی رد می‌شوند مگر اجازه بدهید، تا صفحه‌ای از وب نتواند مدل را به آن‌جا بفرستد.

**نبرد.** یک پرسش، دو پاسخ: مدل پایه، و همان مدل با اداپترش. هم‌زمان اجرا کنید یا یکی‌یکی که هر کدام کل سیستم را داشته باشد. رأی بدهید — اگر خواستید کور، با نام‌های پنهان تا لحظه‌ی رأی — و نوار کناری امتیاز را نگه می‌دارد. هر دو روی یک نسخه از وزن‌ها اجرا می‌شوند: برای مدل پایه، اداپتر با قدرت صفر اعمال می‌شود.

**هر مدلی از هاگینگ‌فیس.** در بخش مدل‌ها لینک یک مدل و اگر خواستید لینک یک اداپتر را بدهید. برنامه مخزن را می‌خواند و آنچه قابل اجراست فهرست می‌کند — هر کوانتیزاسیون، فایل‌های چندتکه یک‌جا، اندازه‌ی پیشنهادی علامت‌خورده — و انتخاب شما به صف دانلودی می‌رود که مثل بقیه‌ی دانلودها ادامه‌پذیر و بررسی‌شده است. اداپتر PEFT روی همین کامپیوتر به GGUF تبدیل می‌شود. هر مدل پایه را می‌شود با هر اداپتری که برای معماری آن ساخته شده جفت کرد.

**صفحه‌ی تازه.** از نو برای دسکتاپ ساخته شده، با ظاهر کلاسیک استودیو، به فارسی و انگلیسی.

## دانلود

فایل سیستم‌عامل خود را از پایین همین صفحه بگیرید، باز کنید و **AhoosAI Studio** را اجرا کنید. اگر هنوز مدلی ندارید، روی صفحه‌ی مدل‌ها باز می‌شود: یک مدل نیمبوس و اندازه‌اش را انتخاب کنید، یا لینک هاگینگ‌فیس بدهید.

چک‌سام SHA-256 همه‌ی فایل‌ها در `SHA256SUMS.txt` پایین همین صفحه است.

## هشدار امنیتی هنگام اجرا

برنامه هنوز امضای دیجیتال ندارد، برای همین سیستم ممکن است هشدار بدهد. هشدار به‌خاطر نداشتن امضاست، نه به‌خاطر خود برنامه.

- **ویندوز:** روی **More info** و بعد **Run anyway** بزنید.
- **مک:** روی برنامه راست‌کلیک کنید، **Open** و دوباره **Open** را بزنید.
- **لینوکس:** اگر لازم شد `ahoos-studio` را اجراشدنی کنید.

## در نسخه‌ی ۲٫۵ نیست

حالت تیمی، و تصویر: صفحه‌ی تازه فعلاً فقط فایل متنی می‌پذیرد.

## به‌روزرسانی از ۲٫۰

بسته‌ی جدید را به‌جای قبلی اجرا کنید. مدل‌ها، تنظیمات و گفتگوها سر جایشان می‌مانند؛ گفتگوهای نسخه‌ی ۲٫۰ بار اولی که صفحه‌ی تازه باز می‌شود به آن منتقل می‌شوند.

</div>

---

<p align="center"><sub>© 2026 AhoosAI · <a href="https://ahoos-ai.site">ahoos-ai.site</a></sub></p>

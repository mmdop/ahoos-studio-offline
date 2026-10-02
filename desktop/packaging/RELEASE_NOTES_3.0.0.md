<p align="center">
  <b>AhoosAI Studio 3.0 — a model that finishes the job</b><br>
  Still on your own computer, still no account. Now it plans, writes the whole project, checks it, and runs it.
</p>

<p align="center">
  <a href="https://ahoos-ai.site/int-offline-studio">About the app</a> ·
  <a href="https://ahoos-ai.site/nimbus-2-apex-benchmarks.html">How the model was measured</a> ·
  <a href="https://huggingface.co/AhoosAI/nimbus-2-apex">Model on Hugging Face</a>
</p>

---

## What's new in 3.0

**It plans, then does all of it.** Ask for something to build, change or fix and the model first writes a plan: one step for each file, in order, then a step to run or open the result. You see it as a checklist that ticks itself off. Then the steps are carried out one by one — a turn no longer ends after the first file because the model decided it had done enough.

**Whole files, not snippets.** In 2.5 a file was written inside a JSON tool call, every line break and quote escaped, and a small model wrote very little code that way: asked for a snake game, Nimbus 1.1 wrote a 16-line page and a 59-line script with no score on screen and no way to restart. Now each file is written the way the model writes code best — as a code block — with everything written so far in front of it, so the script uses the page's own ids. The same request, on the same model, at the same size: three files, a 125-line script, the score on screen, collisions, game over and a working restart button. A file long enough to reach the length limit is continued where it stopped.

**It checks its own work.** Before it answers, every file it wrote is checked without running it: Python that does not compile, broken JSON, a page that loads a file that is not there, a script that looks up an element the page does not have, a game canvas with no size, a `const` assigned twice. What is found goes back to the model to fix — on that snake game, the canvas and the restart crash, both fixed before the answer.

**Changes as edits.** An existing file is changed with targeted edits, not rewritten, and an edit lands even when the model's indentation is a little off. You see the diff.

**Undo a whole answer.** Every file an answer creates or changes is copied aside first. **Undo the changes** puts all of them back in one click — files it created are removed, files it changed come back as they were.

**Approve a plan once.** In the default mode you approve the plan, and its files are written without asking again; commands are still asked about. A new **Automatic** mode changes files in the project folder without asking at all, because every change can be undone; commands are asked about once each.

**Continue where it stopped.** A big task can run out of steps (40 by default, up to 120). It keeps its plan, and **Continue** picks it up at the first step not done.

**A folder for each project.** With no folder connected, a new project gets its own folder in *Documents › AhoosAI Studio*. Asked to build something in a folder full of other things, it makes a folder for it there instead of writing among them. Each conversation remembers its project folder.

**Servers that keep running.** A dev server, `python -m http.server`, a watcher — these now run in the background instead of holding the conversation until they are killed, with their address and a **Stop** button; they all stop when the app closes. **Open the page** opens a web project in your browser.

**On your graphics card.** The Windows and Linux builds now use llama.cpp's Vulkan engine (Macs already used Metal): as much of the model as fits goes on your graphics card — NVIDIA, AMD or Intel — and the rest runs on the processor. Measured on a laptop with a 4 GB GTX 1050 Ti and Nimbus 1.1 at q3_k_m: 15 of the model's 29 layers on the card, 4.5 tokens a second against 3.3 on its processor alone, and a whole project turn — plan, three files, the check, the answer — in 11 minutes. A card with more memory takes more of the model and gains more. Settings shows what went on the card. With no usable card it runs on the processor exactly as before, and it can be turned off in Settings.

**Also:**
- The context is 16K tokens by default (up to 64K), so a project of several files fits in one conversation. Settings left at 2.5's defaults are moved to 3.0's on first start; values you chose stay.
- Binary files (programs, archives, images) are recognised and left alone — the model no longer spends its turn trying to read `.exe` files in a games folder.
- On Windows, commands with a quoted path (`python "my script.py"`) failed in 2.5; they run now.
- If Windows' **Smart App Control** blocks the model engine, the app says so and what you can do, instead of "the model stopped while loading".
- Some graphics drivers lose the card when the model is put on it in blocks of a gigabyte — a GTX 1050 Ti on a 2022 driver did, past nine layers. The model now goes on in blocks of 512 MB, which every card takes.
- The summary at the end is in the language you asked in.

## Download

| System | File |
|---|---|
| Windows 10 / 11, 64-bit | `AhoosAI-Studio-3.0.0-windows-x64.zip` |
| macOS, Apple silicon (M1 and later) | `AhoosAI-Studio-3.0.0-macos-arm64.dmg` |
| macOS, Intel | `AhoosAI-Studio-3.0.0-macos-x64.dmg` |
| Linux, x86-64 | `AhoosAI-Studio-3.0.0-linux-x64.tar.gz` |

Unpack it and run **AhoosAI Studio**. With no model yet, it opens on the Models page.

For writing code, use **Nimbus 1.1 at q4_k_m** or larger if your computer has the memory: q3_k_m is smaller and faster, and makes more mistakes.

## Check your download

Every file's SHA-256 is in `SHA256SUMS.txt`, attached below.

- Windows: `certutil -hashfile AhoosAI-Studio-3.0.0-windows-x64.zip SHA256`
- macOS / Linux: `shasum -a 256 AhoosAI-Studio-3.0.0-*`

## Security warnings on first run

The app is not yet code-signed, and neither is llama.cpp, which runs the model.

- **Windows, "Windows protected your PC":** click **More info**, then **Run anyway**.
- **Windows 11 with Smart App Control on:** Windows refuses to run llama.cpp's unsigned files, so no model can start; the app tells you when this happens. Smart App Control can be turned off in *Windows Security › App & browser control › Smart App Control settings*. That is your decision to make: on some versions of Windows it can only be turned back on by reinstalling Windows.
- **macOS, "cannot be opened because the developer cannot be verified":** right-click the app, choose **Open**, then **Open** again.
- **Linux:** mark `ahoos-studio` executable if asked. Without WebKitGTK the studio opens in your browser instead of its own window.

## Upgrading from 2.5

Run the new package in place of the old one. Your models, settings and conversations stay where they were.

---

<div dir="rtl">

## چه چیزی در نسخه‌ی ۳ تازه است

**اول برنامه می‌ریزد، بعد همه‌اش را انجام می‌دهد.** هر چیزی برای ساختن، تغییر دادن یا درست کردن بخواهید، مدل اول یک برنامه می‌نویسد: برای هر فایل یک گام، به ترتیب، و در آخر یک گام برای اجرا یا باز کردن نتیجه. برنامه را به شکل یک فهرست می‌بینید که خودش تیک می‌خورد. بعد گام‌ها یکی‌یکی انجام می‌شوند — دیگر کار بعد از اولین فایل تمام نمی‌شود فقط چون مدل فکر کرده به اندازه‌ی کافی کار کرده.

**فایل کامل، نه یک تکه کد.** در نسخه‌ی ۲٫۵ هر فایل داخل یک فراخوانی JSON نوشته می‌شد و همه‌ی خط‌ها و نقل‌قول‌هایش باید escape می‌شد؛ مدل کوچک این‌طوری کد خیلی کمی می‌نوشت: برای یک بازی مار، نیمبوس ۱٫۱ یک صفحه‌ی ۱۶ خطی و یک اسکریپت ۵۹ خطی نوشت، بدون نمایش امتیاز و بدون شروع دوباره. حالا هر فایل همان‌طور نوشته می‌شود که مدل کد را بهتر از هر راه دیگری می‌نویسد — به شکل یک بلوک کد — و همه‌ی فایل‌های قبلی جلوی چشمش است، پس اسکریپت همان شناسه‌هایی را به کار می‌برد که صفحه دارد. همان درخواست، روی همان مدل و همان اندازه: سه فایل، یک اسکریپت ۱۲۵ خطی، امتیاز روی صفحه، برخورد، پایان بازی و دکمه‌ی شروع دوباره‌ای که کار می‌کند. فایلی که به سقف طول برسد، از همان جایی که قطع شده ادامه پیدا می‌کند.

**کار خودش را بررسی می‌کند.** پیش از پاسخ، همه‌ی فایل‌هایی که نوشته بدون اجرا بررسی می‌شوند: پایتونی که کامپایل نمی‌شود، JSON خراب، صفحه‌ای که فایلی را بار می‌کند که وجود ندارد، اسکریپتی که دنبال عنصری می‌گردد که صفحه ندارد، بوم بازی‌ای که اندازه ندارد، و `const`ای که دوباره مقدار می‌گیرد. هر ایرادی پیدا شود، برای درست کردن به مدل برمی‌گردد — در همان بازی مار، اندازه‌ی بوم و خطای شروع دوباره، هر دو پیش از پاسخ درست شدند.

**تغییر، به شکل ویرایش.** فایلی که از قبل هست با ویرایش‌های دقیق تغییر می‌کند، نه با بازنویسی کامل، و ویرایش حتی وقتی تورفتگی مدل کمی جابه‌جا باشد سر جایش می‌نشیند. تغییرات را می‌بینید.

**برگرداندن یک پاسخ کامل.** هر فایلی که یک پاسخ می‌سازد یا تغییر می‌دهد، اول یک نسخه از آن کنار گذاشته می‌شود. «برگرداندن تغییرها» همه را با یک کلیک به حالت قبل برمی‌گرداند — فایل‌هایی که ساخته پاک می‌شوند و فایل‌هایی که تغییر داده به حالت قبل برمی‌گردند.

**یک بار تأیید برای یک برنامه.** در حالت پیش‌فرض برنامه را تأیید می‌کنید و فایل‌هایش بدون پرسیدن دوباره نوشته می‌شوند؛ دستورها هنوز پرسیده می‌شوند. حالت تازه‌ی «خودکار» فایل‌های پوشه‌ی پروژه را بدون هیچ پرسشی تغییر می‌دهد، چون هر تغییر برگشت‌پذیر است؛ هر دستور یک بار پرسیده می‌شود.

**ادامه از همان‌جا.** یک کار بزرگ ممکن است گام‌هایش تمام شود (به‌طور پیش‌فرض ۴۰ گام، تا ۱۲۰). برنامه‌اش نگه داشته می‌شود و «ادامه بده» کار را از اولین گامِ انجام‌نشده پی می‌گیرد.

**یک پوشه برای هر پروژه.** وقتی پوشه‌ای وصل نیست، هر پروژه‌ی تازه پوشه‌ی خودش را در *Documents › AhoosAI Studio* می‌گیرد. اگر در پوشه‌ای پر از چیزهای دیگر چیزی بخواهید، به‌جای نوشتن لابه‌لای آن‌ها، یک پوشه‌ی جدا برایش می‌سازد. هر گفتگو پوشه‌ی پروژه‌ی خودش را به خاطر می‌سپارد.

**سرورهایی که می‌مانند.** یک سرور توسعه، `python -m http.server`، یا هر برنامه‌ای که تمام نمی‌شود، حالا در پس‌زمینه اجرا می‌شود و دیگر گفتگو را تا وقتی متوقف شود معطل نمی‌کند — با نشانی‌اش و یک دکمه‌ی «متوقف کن»؛ با بسته شدن برنامه همه متوقف می‌شوند. «باز کردن صفحه» یک پروژه‌ی وب را در مرورگر شما باز می‌کند.

**روی کارت گرافیک.** نسخه‌های ویندوز و لینوکس حالا موتور Vulkan از llama.cpp را به کار می‌برند (مک از قبل Metal داشت): هر چقدر از مدل که جا شود روی کارت گرافیک — NVIDIA، AMD یا Intel — اجرا می‌شود و بقیه روی پردازنده. اندازه‌گیری روی یک لپ‌تاپ با کارت ۴ گیگابایتی GTX 1050 Ti و نیمبوس ۱٫۱ با اندازه‌ی q3_k_m: ۱۵ لایه از ۲۹ لایه‌ی مدل روی کارت، ۴٫۵ توکن در ثانیه در برابر ۳٫۳ روی پردازنده‌ی تنها، و یک نوبت کامل پروژه — برنامه، سه فایل، بررسی و پاسخ — در ۱۱ دقیقه. کارتی با حافظه‌ی بیشتر سهم بیشتری از مدل را می‌گیرد و سریع‌تر است. تنظیمات نشان می‌دهد چه مقدار روی کارت رفته. اگر کارت قابل استفاده‌ای نباشد، درست مثل قبل روی پردازنده اجرا می‌شود، و در تنظیمات می‌شود خاموشش کرد.

**و همچنین:**
- طول context به‌طور پیش‌فرض ۱۶ هزار توکن است (تا ۶۴ هزار)، تا پروژه‌ای با چند فایل در یک گفتگو جا شود. تنظیماتی که روی پیش‌فرض‌های ۲٫۵ مانده‌اند در اولین اجرا به پیش‌فرض‌های ۳٫۰ می‌روند؛ مقدارهایی که خودتان انتخاب کرده‌اید دست نمی‌خورند.
- فایل‌های باینری (برنامه، فایل فشرده، تصویر) شناخته می‌شوند و به حال خود رها می‌شوند — مدل دیگر نوبتش را صرف خواندن فایل‌های `.exe` یک پوشه‌ی بازی نمی‌کند.
- در ویندوز، دستورهایی که مسیرشان داخل گیومه بود (`python "my script.py"`) در ۲٫۵ اجرا نمی‌شدند؛ حالا می‌شوند.
- اگر Smart App Control ویندوز موتور مدل را مسدود کند، برنامه همین را می‌گوید و اینکه چه کاری از دستتان برمی‌آید، به‌جای «مدل هنگام بارگذاری متوقف شد».
- بعضی درایورهای کارت گرافیک وقتی مدل در تکه‌های یک گیگابایتی روی کارت گذاشته شود، کارت را از دست می‌دهند — یک GTX 1050 Ti با درایور سال ۲۰۲۲ از نُه لایه به بعد همین‌طور بود. حالا مدل در تکه‌های ۵۱۲ مگابایتی روی کارت می‌رود که همه‌ی کارت‌ها می‌پذیرند.
- جمع‌بندی آخر کار به همان زبانی است که پرسیده‌اید.

## دانلود

| سیستم | فایل |
|---|---|
| ویندوز ۱۰ / ۱۱، ۶۴ بیتی | `AhoosAI-Studio-3.0.0-windows-x64.zip` |
| مک، اپل سیلیکون (M1 به بعد) | `AhoosAI-Studio-3.0.0-macos-arm64.dmg` |
| مک، اینتل | `AhoosAI-Studio-3.0.0-macos-x64.dmg` |
| لینوکس، x86-64 | `AhoosAI-Studio-3.0.0-linux-x64.tar.gz` |

فایل را باز کنید و **AhoosAI Studio** را اجرا کنید. اگر هنوز مدلی نصب نشده باشد، برنامه با صفحه‌ی مدل‌ها باز می‌شود.

برای کدنویسی، اگر حافظه‌ی کامپیوترتان اجازه می‌دهد **نیمبوس ۱٫۱ با اندازه‌ی q4_k_m** یا بزرگ‌تر را انتخاب کنید: q3_k_m کوچک‌تر و سریع‌تر است و اشتباه بیشتری می‌کند.

## هشدارهای امنیتی در اولین اجرا

برنامه هنوز امضای دیجیتال ندارد، و llama.cpp هم که مدل را اجرا می‌کند ندارد.

- **ویندوز، «Windows protected your PC»:** روی **More info** و بعد **Run anyway** بزنید.
- **ویندوز ۱۱ با Smart App Control روشن:** ویندوز فایل‌های بدون امضای llama.cpp را اجرا نمی‌کند و هیچ مدلی راه نمی‌افتد؛ برنامه وقتی این اتفاق بیفتد به شما می‌گوید. Smart App Control را می‌شود از *Windows Security › App & browser control › Smart App Control settings* خاموش کرد. این تصمیم با شماست: در بعضی نسخه‌های ویندوز، روشن کردن دوباره‌ی آن فقط با نصب دوباره‌ی ویندوز ممکن است.
- **مک، «cannot be opened because the developer cannot be verified»:** روی برنامه راست‌کلیک کنید، **Open** را بزنید و دوباره **Open**.

## ارتقا از ۲٫۵

نسخه‌ی تازه را به‌جای قبلی اجرا کنید. مدل‌ها، تنظیمات و گفتگوهایتان سر جایشان می‌مانند.

</div>

/* Starting points, for the empty chat of both studios (the online one copies this file, through
   tools/site-build/studio_ui.py). A category opens a sheet of prompts with blanks to fill: {hint} is
   a blank on one line, {*hint} a blank for pasted text. `web`: the category turns web search on.
   `image`: it needs an attached image (online). `folder`: shown only with a project folder (offline). */
window.STARTERS = [
  { id: 'project', icon: 'folder', folder: true, fa: 'پروژه', en: 'Project', prompts: {
    fa: ['این پروژه را بررسی کن و توضیح بده هر بخش چه کاری می‌کند.',
         'باگ‌های {فایل یا بخش} را پیدا و درست کن.',
         'یک {README} برای این پروژه بنویس که {نصب و اجرا} را توضیح دهد.',
         'به این پروژه {ویژگی تازه} اضافه کن.'],
    en: ['Look through this project and explain what each part does.',
         'Find and fix the bugs in {a file or part}.',
         'Write a {README} for this project that explains {setup and running}.',
         'Add {a new feature} to this project.'] } },
  { id: 'code', icon: 'code', fa: 'کدنویسی', en: 'Code', prompts: {
    fa: ['یک اسکریپت {پایتون} بنویس که {چه کاری انجام دهد}.',
         'باگ‌های این کد را پیدا و درست کن:\n{*کد را اینجا بگذارید}',
         'یک {صفحهٔ وب} با {HTML، CSS و JS} بساز که {ویژگی اصلی آن}.',
         'این خطا را توضیح بده و راه‌حلش را بگو: {*متن خطا}'],
    en: ['Write a {Python} script that {what it should do}.',
         'Find and fix the bugs in this code:\n{*paste the code}',
         'Build a {web page} with {HTML, CSS and JS} that {its main feature}.',
         'Explain this error and how to fix it: {*the error message}'] } },
  { id: 'learn', icon: 'book', fa: 'یادگیری', en: 'Learn', prompts: {
    fa: ['{مفهوم} را با یک مثال ساده برای {یک مبتدی} توضیح بده.',
         '{گزینهٔ اول} و {گزینهٔ دوم} را در یک جدول از نظر {معیارها} مقایسه کن.',
         'یک برنامهٔ یادگیری {چهار} هفته‌ای برای {مهارت} بنویس، با {پنج} ساعت در هفته.',
         'با {پنج} سؤال دربارهٔ {موضوع} از من آزمون بگیر؛ هر بار یک سؤال.'],
    en: ['Explain {concept} with a simple example, for {a beginner}.',
         'Compare {option A} and {option B} in a table, by {criteria}.',
         'Make a {4}-week learning plan for {skill}, with {5} hours a week.',
         'Quiz me with {5} questions on {topic}, one at a time.'] } },
  { id: 'write', icon: 'pencil', fa: 'نوشتن', en: 'Write', prompts: {
    fa: ['یک ایمیل {مودبانه} به {گیرنده} دربارهٔ {موضوع} بنویس.',
         'این متن را {کوتاه‌تر و رسمی‌تر} کن:\n{*متن}',
         'یک پست {لینکدین} دربارهٔ {موضوع} در {سه} پاراگراف کوتاه بنویس.',
         'این را به {انگلیسی} ترجمه کن:\n{*متن}'],
    en: ['Write a {polite} email to {whom} about {subject}.',
         'Rewrite this to be {shorter and more formal}:\n{*the text}',
         'Write a {LinkedIn} post about {topic} in {3} short paragraphs.',
         'Translate this into {Persian}:\n{*the text}'] } },
  { id: 'research', icon: 'globe', web: true, fa: 'جستجو و تحلیل', en: 'Research', prompts: {
    fa: ['تازه‌ترین خبرها دربارهٔ {موضوع} را جستجو کن و با ذکر منبع خلاصه کن.',
         'مزایا و معایب {گزینه} را برای {هدف} بررسی کن.',
         '{سه} {ابزار یا محصول} برای {نیاز} پیدا کن و مقایسه کن.',
         'این داده‌ها را تحلیل کن و بگو چه چیزی مهم است:\n{*داده‌ها}'],
    en: ['Search the web for the latest on {topic} and summarise it, naming the sources.',
         'Weigh the pros and cons of {option} for {purpose}.',
         'Find {3} {tools or products} for {need} and compare them.',
         'Analyse this data and tell me what stands out:\n{*the data}'] } },
  { id: 'image', icon: 'eye', image: true, fa: 'تصویر', en: 'Image', prompts: {
    fa: ['متن این تصویر را بخوان و {خلاصه کن}.',
         'جدول این تصویر را به {CSV} تبدیل کن.',
         'این نمودار چه چیزی نشان می‌دهد؟ کدام {دسته} بیشترین مقدار را دارد؟',
         'این صفحه را بررسی کن و {مشکلات طراحی} آن را بگو.'],
    en: ['Read the text in this image and {summarise it}.',
         'Turn the table in this image into {CSV}.',
         'What does this chart show? Which {category} is highest?',
         'Look at this screen and point out its {design problems}.'] } },
];

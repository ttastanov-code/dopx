// static/js/alpine-components.js
//
// Все Alpine.data()-компоненты проекта — единая точка регистрации.
//
// ПОЧЕМУ ЭТОТ ФАЙЛ ПОЯВИЛСЯ (2026-08-21): раньше x-data писался ПРЯМО в
// HTML-атрибутах как инлайновый объектный литерал — `x-data="{ open: false,
// ... }"`. Обычная (не-CSP) сборка Alpine компилирует КАЖДОЕ такое выражение
// через `new Function(...)` — это и есть eval с точки зрения браузера,
// поэтому CSP-политике сайта (dopx/middleware.py::ContentSecurityPolicyMiddleware)
// приходилось держать 'unsafe-eval' в script-src. Сборка @alpinejs/csp (см.
// <script> в base.html/base_auth.html) специально ЗАПРЕЩАЕТ инлайновые
// объектные литералы в x-data — компонент обязан быть зарегистрирован ЗДЕСЬ
// через Alpine.data(имя, фабрика) и подключаться в HTML как `x-data="имя"`
// или `x-data="имя(аргумент)"` (документированный поддерживаемый паттерн,
// см. tooltipTrigger ниже — он и раньше так был устроен).
//
// ПРАВИЛО ДЛЯ НОВЫХ КОМПОНЕНТОВ: вся логика — методы/геттеры/init() —
// должна жить ВНУТРИ фабричной функции (обычный JS-файл, браузер выполняет
// его напрямую, никакого урезанного CSP-евалуатора здесь нет). В САМИХ
// HTML-атрибутах (@click=, x-show=, :class=, x-init=) оставляйте только
// простые выражения — ссылку на свойство, вызов метода, тернарник. Никаких
// инлайновых стрелочных функций/объектных литералов в атрибутах — именно
// это раньше требовало eval и не поддерживается CSP-евалуатором.
//
// Подключается в <head> ДО скрипта ядра Alpine (оба через defer — порядок
// выполнения нескольких defer-скриптов соответствует порядку в DOM), чтобы
// слушатель 'alpine:init' успел навесится до того, как Alpine сам
// инициализируется.

document.addEventListener('alpine:init', () => {
    // === Переключатель темы (base.html/base_auth.html, <body>) ===
    Alpine.data('themeSwitcher', () => ({
        theme: localStorage.getItem('theme') || 'light',
        init() {
            document.documentElement.setAttribute('data-theme', this.theme);
            this.$watch('theme', (value) => {
                document.documentElement.setAttribute('data-theme', value);
                localStorage.setItem('theme', value);
            });
        },
        toggleTheme() {
            this.theme = this.theme === 'light' ? 'dark' : 'light';
        },
    }));

    // === Дублирующийся тост-блок flash-сообщений (base.html/base_auth.html) ===
    Alpine.data('dismissible', () => ({
        show: true,
    }));

    // === Плашка cookie (components/_cookie_banner.html) ===
    Alpine.data('cookieBanner', () => ({
        show: false,
        ackKey: 'dopx_cookie_notice_ack_v1',
        init() {
            this.show = !localStorage.getItem(this.ackKey);
        },
        accept() {
            localStorage.setItem(this.ackKey, '1');
            this.show = false;
        },
    }));

    // === Кнопка "наверх" (components/_footer.html) ===
    Alpine.data('scrollToTopButton', () => ({
        visible: false,
        init() {
            window.addEventListener('scroll', () => {
                this.visible = window.scrollY > 300;
                this.$el.classList.toggle('opacity-0', !this.visible);
                this.$el.classList.toggle('pointer-events-none', !this.visible);
            });
        },
        scrollToTop() {
            window.scrollTo({ top: 0, behavior: 'smooth' });
        },
    }));

    // === Колокольчик уведомлений (components/_notification_badge.html) ===
    Alpine.data('notificationDropdown', () => ({
        open: false,
    }));

    // === Подсказка-пузырь (components/_tooltip_icon.html, tooltip_tags.py) ===
    // ИСПРАВЛЕНО (2026-08-21, баг "Дербии002Дэксперт" вместо "Дерби-эксперт"):
    // раньше текст приходил аргументом фабрики — tooltipTrigger('...'),
    // строка была экранирована через escapejs() на сервере, что для дефиса
    // даёт `-`. CSP-евалуатор Alpine разбирает x-data-выражение сам
    // (это не настоящий JS eval) и не раскрывает \uXXXX-эскейпы внутри
    // строковых литералов — символы утекали в текст как есть. Теперь текст
    // читается из обычного HTML data-атрибута (штатный HTML-escape на
    // сервере, никакого JS-парсинга вообще не участвует).
    Alpine.data('tooltipTrigger', () => ({
        text: '',
        open: false,
        pos: { top: 0, left: 0 },
        init() {
            this.text = this.$el.dataset.tooltipText || '';
        },
        show() {
            this.open = true;
            this.$nextTick(() => this.position());
        },
        hide() {
            this.open = false;
        },
        toggle() {
            this.open ? this.hide() : this.show();
        },
        position() {
            const trigger = this.$refs.trigger;
            const bubble = this.$refs.bubble;
            if (!trigger || !bubble) return;
            const margin = 8;
            const tRect = trigger.getBoundingClientRect();
            const bRect = bubble.getBoundingClientRect();
            let top = tRect.top - bRect.height - margin;
            if (top < margin) {
                top = tRect.bottom + margin;
            }
            let left = tRect.left + tRect.width / 2 - bRect.width / 2;
            left = Math.max(margin, Math.min(left, window.innerWidth - bRect.width - margin));
            this.pos = { top, left };
        },
    }));

    // === Переключатель видимости пароля, форма входа (auth/login.html) ===
    // Сохраняет 1:1 поведение исходного инлайн-выражения, включая его
    // особенность: $el — это сама кнопка, а previousElementSibling кнопки —
    // иконка <i>, НЕ <input> (между ними в разметке лежит иконка). У <i>
    // нет отражаемого в DOM атрибута/свойства type, так что .type на нём
    // ни на что не влияет — идентично оригиналу, это НЕ новый баг.
    Alpine.data('loginForm', () => ({
        showPassword: false,
        toggleVisibility(btnEl) {
            this.showPassword = !this.showPassword;
            btnEl.previousElementSibling.type = this.showPassword ? 'text' : 'password';
        },
    }));

    // === Форма регистрации (auth/register.html) — показ/скрытие пароля
    // здесь идёт через ГЛОБАЛЬНУЮ togglePassword() (base_auth.html), это
    // компонент только для сверки "пароли совпадают". ===
    Alpine.data('registerForm', () => ({
        password: '',
        confirmPassword: '',
        passwordsMatch: true,
        checkMatch() {
            this.passwordsMatch = this.password === this.confirmPassword;
        },
    }));

    // === Форма сброса пароля (auth/password_reset_confirm.html) — два
    // независимых поля-пароля, toggle(field, $el) переиспользуется для
    // обоих вместо копипасты многострокового @click-выражения (которое
    // раньше делало ДВЕ вещи через ";" — CSP-евалуатор такое не гарантирует
    // поддерживать, поэтому логика перенесена в метод компонента). ===
    Alpine.data('passwordResetForm', () => ({
        showPassword: false,
        showPassword2: false,
        toggle(field, btnEl) {
            this[field] = !this[field];
            btnEl.previousElementSibling.type = this[field] ? 'text' : 'password';
        },
    }));

    // === Контекст просмотра матча, шаг 1 вайзарда (evaluations/context.html) ===
    // ИСПРАВЛЕНО (2026-08-21): раньше supportedTeam/watchedType шли
    // аргументами фабрики через escapejs() — тот же класс бага, что и у
    // tooltipTrigger (см. комментарий выше), но здесь незаметный: id команды
    // это UUID С ДЕФИСАМИ, поэтому :class="supportedTeam === '{{ team.id }}'"
    // мог тихо не совпадать и не подсвечивать выбранную команду. Теперь
    // читаем исходные значения из data-атрибутов (обычный HTML-escape).
    Alpine.data('matchContextForm', () => ({
        supportedTeam: '',
        watchedType: 'full',
        attendedStadium: false,
        // evalMode — режим "Быстро/Подробно" (см.
        // docs/adr/0006-quick-full-evaluation-mode.md и
        // docs/adr/0031-quick-mode-primary-flow.md), читается
        // evaluations/views.py::EvaluateContextView.form_valid из POST
        // ('eval_mode') и пишется в EvaluationSession.mode. Дефолт 'quick'
        // (с 2026-09-07) — быстрый режим теперь основной сценарий, если
        // пользователь ничего не выбрал (или JS не выполнился, тогда
        // радио-инпут просто не рендерится визуально выбранным, но
        // нативная разметка всё равно шлёт value по умолчанию через
        // checked-атрибут на сервере — см. context.html).
        evalMode: 'quick',
        init() {
            this.supportedTeam = this.$el.dataset.supportedTeam || '';
            this.watchedType = this.$el.dataset.watchedType || 'full';
            this.attendedStadium = this.$el.dataset.attendedStadium === 'true';
            this.evalMode = this.$el.dataset.evalMode || 'quick';
        },
    }));

    // === Карточка игрока в вайзарде оценки (evaluations/_player_card.html) —
    // рендерится в цикле по составу, каждый экземпляр независим. ===
    // initialEvaluate — режим "Быстро" (см.
    // docs/adr/0006-quick-full-evaluation-mode.md) предзаполняет тумблер
    // для небольшого набора заметных игроков; булево значение приходит из
    // шаблона как литерал true/false (не строка пользовательского ввода —
    // это безопасно инлайнить прямо в x-data, в отличие от текстовых полей,
    // см. историю бага с tooltipTrigger/escapejs в docs/BACKLOG.md).
    Alpine.data('playerEvaluationCard', (initialEvaluate) => ({
        evaluate: !!initialEvaluate,
        contribution: 5,
        risk: 5,
        potential: 5,
    }));

    // === Форма обратной связи (core/contacts.html) ===
    // ИСПРАВЛЕНО (2026-08-21): та же категория бага, что у tooltipTrigger —
    // category/email раньше приходили через escapejs()-аргументы фабрики;
    // email с дефисом (например "anna-k@example.com") ловил ту же порчу.
    // Теперь читаем из data-атрибутов.
    Alpine.data('contactForm', () => ({
        submitting: false,
        category: 'general',
        categoryOpen: false,
        subject: '',
        message: '',
        email: '',
        screenshot: null,
        screenshotPreview: null,
        // НОВОЕ (2026-09-10, запрос пользователя: заменить emoji в
        // "Тема обращения" на "нормальные солидные премиум обозначения"):
        // нативный <select><option> физически не умеет рисовать иконки
        // внутри option (ограничение браузера, не Alpine/CSS) — поэтому
        // дропдаун теперь кастомный (details/summary, тот же паттерн, что
        // в components/_navbar.html), а этот массив — единственный
        // источник правды для его пунктов. value должен совпадать с
        // notifications/models.py::ContactSubmission.CATEGORY_CHOICES.
        categoryOptions: [
            { value: 'general', label: 'Общий вопрос', hint: 'Что-то другое или не уверены, куда', icon: 'ti-message-circle', color: 'primary' },
            { value: 'bug', label: 'Сообщение об ошибке', hint: 'Что-то не работает или ведёт себя не так', icon: 'ti-bug', color: 'error' },
            { value: 'feature', label: 'Предложение функции', hint: 'Идея, как сделать DOPX лучше', icon: 'ti-bulb', color: 'warning' },
            { value: 'evaluation', label: 'Проблема с оценкой матча', hint: 'Не согласны с рейтингом или оценкой', icon: 'ti-star', color: 'secondary' },
            { value: 'account', label: 'Вопрос по аккаунту', hint: 'Профиль, вход, уведомления', icon: 'ti-user', color: 'info' },
            { value: 'dispute', label: 'Оспорить рейтинг / право на ответ', hint: 'Формальное обращение по конкретной оценке', icon: 'ti-scale', color: 'accent' },
            { value: 'data_error', label: 'Ошибка в данных матча', hint: 'Неверный счёт, состав, события матча', icon: 'ti-alert-triangle', color: 'error' },
            { value: 'other', label: 'Другое', hint: '', icon: 'ti-dots', color: 'neutral' },
        ],
        get selectedCategory() {
            return this.categoryOptions.find((c) => c.value === this.category) || this.categoryOptions[0];
        },
        selectCategory(value) {
            this.category = value;
            this.categoryOpen = false;
        },
        init() {
            this.category = this.$el.dataset.initialCategory || 'general';
            this.email = this.$el.dataset.initialEmail || '';
            // НОВОЕ (2026-09-09, Центр доверия к данным): переход с кнопки
            // "Сообщить об ошибке в данных" на странице матча приносит
            // готовую тему через data-initial-subject — экономит человеку
            // набор текста, но остаётся редактируемым полем, не readonly.
            this.subject = this.$el.dataset.initialSubject || '';
        },
        handleFileSelect(event) {
            const file = event.target.files[0];
            if (!file) return;
            if (file.size > 5 * 1024 * 1024) {
                alert('⚠️ Файл слишком большой (макс. 5MB)');
                return;
            }
            this.screenshot = file;
            const reader = new FileReader();
            reader.onload = (e) => {
                this.screenshotPreview = e.target.result;
            };
            reader.readAsDataURL(file);
        },
        removeScreenshot() {
            this.screenshot = null;
            this.screenshotPreview = null;
            const input = document.getElementById('screenshot-input');
            if (input) input.value = '';
        },
        get characterCount() {
            return this.message.length;
        },
        get isMessageValid() {
            return this.message.length >= 20 && this.subject.length >= 5;
        },
        get canSubmit() {
            return !this.submitting && this.isMessageValid;
        },
    }));

    // === Embed-модалка на странице игрока (players/detail.html) ===
    Alpine.data('embedModal', () => ({
        open: false,
        copied: false,
        copy() {
            navigator.clipboard.writeText(this.$refs.embedCode.value);
            this.copied = true;
            setTimeout(() => {
                this.copied = false;
            }, 2000);
        },
    }));

    // === Копирование кода без модалки (dashboard/widgets.html) ===
    // Та же идея, что у embedModal.copy(), но без open/modal-состояния —
    // на странице "Виджеты" сразу три независимых блока с готовым кодом
    // (игрок/команда/таблица), каждому нужна только кнопка "Скопировать"
    // рядом с textarea, без модалки поверх.
    Alpine.data('copyBox', () => ({
        copied: false,
        copy() {
            navigator.clipboard.writeText(this.$refs.codeBox.value);
            this.copied = true;
            setTimeout(() => {
                this.copied = false;
            }, 2000);
        },
    }));

    // === Карточка push-уведомлений (users/notification_settings.html) ===
    // csrfToken раньше приходил аргументом фабрики через escapejs() — сам
    // токен Django генерирует из алфавита без спецсимволов, так что этот
    // конкретный случай на практике не ловил баг tooltipTrigger (см. выше),
    // но паттерн тот же самый и хрупкий, поэтому на всякий случай тоже
    // переведён на data-атрибут — единообразно с остальными компонентами.
    Alpine.data('pushSettings', () => ({
        status: 'checking',
        csrfToken: '',
        async init() {
            this.csrfToken = this.$el.dataset.csrfToken || '';
            // csrfToken пробрасывается в dopxPushStatus для самолечения
            // осиротевшей подписки (см. static/js/push.js::dopxPushStatus
            // за полным объяснением бага, найденного пользователем 2026-09-09).
            this.status = await window.dopxPushStatus(this.csrfToken);
        },
        async subscribe() {
            this.status = 'loading';
            const vapidKey = document.body.dataset.vapidPublicKey;
            if (!vapidKey) {
                this.status = 'unavailable';
                return;
            }
            const result = await window.dopxSubscribePush(vapidKey, this.csrfToken);
            this.status = result.ok ? 'subscribed' : (result.reason || 'error');
        },
        async unsubscribe() {
            this.status = 'loading';
            const result = await window.dopxUnsubscribePush(this.csrfToken);
            this.status = result.ok ? 'idle' : 'error';
        },
    }));

    // === Countdown до стартового свистка (components/_match_card.html,
    // пункт 7 брифа редизайна карточки матча, 2026-09-10) ===
    // Дата приходит через data-kickoff (не аргументом фабрики) — та же
    // защита от бага tooltipTrigger/escapejs (см. комментарии выше в этом
    // файле): ISO-строка с "c"-форматом Django содержит дефисы/двоеточия,
    // и хотя конкретно здесь нет escapejs (значит формально бага и не
    // было бы), проект последовательно читает ЛЮБОЙ строковый аргумент из
    // data-атрибута — не стоит заводить единственное исключение из этого
    // правила ради одного компонента.
    Alpine.data('matchCountdown', () => ({
        countdownText: '',
        _timerId: null,
        init() {
            const kickoffIso = this.$el.dataset.kickoff;
            if (!kickoffIso) return;
            const kickoffMs = new Date(kickoffIso).getTime();
            if (Number.isNaN(kickoffMs)) return;
            this._tick(kickoffMs);
            // 30с достаточно для минутной точности текста ("Через N мин"),
            // не нужен ежесекундный тик — карточка в списке, не таймер
            // обратного отсчёта крупным планом.
            this._timerId = setInterval(() => this._tick(kickoffMs), 30000);
        },
        destroy() {
            if (this._timerId) clearInterval(this._timerId);
        },
        _tick(kickoffMs) {
            const diffMs = kickoffMs - Date.now();
            if (diffMs <= 0) {
                this.countdownText = 'Начинается';
                if (this._timerId) clearInterval(this._timerId);
                return;
            }
            const totalMinutes = Math.floor(diffMs / 60000);
            const days = Math.floor(totalMinutes / 1440);
            const hours = Math.floor((totalMinutes % 1440) / 60);
            const minutes = totalMinutes % 60;
            if (days >= 1) {
                this.countdownText = `Через ${days} дн ${hours} ч`;
            } else if (hours >= 1) {
                this.countdownText = `Через ${hours} ч ${minutes} мин`;
            } else {
                this.countdownText = `Через ${minutes} мин`;
            }
        },
    }));
});

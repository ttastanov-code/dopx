// static/js/alpine-components.js
//
// Все Alpine.data()-компоненты проекта (CSP-сборка Alpine не поддерживает x-data="{...}").
// Логику держать в фабриках; в HTML-атрибутах — только простые выражения.
// Подключается до ядра Alpine, чтобы успеть подписаться на 'alpine:init'.

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

    // === Горизонтальная лента с кнопками прокрутки (css/ui.css, .dx-rail-wrap) ===
    Alpine.data('railScroller', () => ({
        canPrev: false,
        canNext: false,
        init() {
            this.update();
            this.$refs.rail.addEventListener('scroll', () => this.update(), { passive: true });
            window.addEventListener('resize', () => this.update(), { passive: true });
        },
        update() {
            const rail = this.$refs.rail;
            this.canPrev = rail.scrollLeft > 4;
            this.canNext = rail.scrollLeft + rail.clientWidth < rail.scrollWidth - 4;
        },
        scrollPrev() {
            this.$refs.rail.scrollBy({ left: -this.$refs.rail.clientWidth * 0.8, behavior: 'smooth' });
        },
        scrollNext() {
            this.$refs.rail.scrollBy({ left: this.$refs.rail.clientWidth * 0.8, behavior: 'smooth' });
        },
    }));

    // === Дублирующийся тост-блок flash-сообщений (base.html/base_auth.html) ===
    // data-autohide — через сколько мс скрыть; наведение ставит таймер на паузу.
    Alpine.data('dismissible', () => ({
        show: true,
        timer: null,
        init() {
            this.delay = Number(this.$el.dataset.autohide || 0);
            this.start();
        },
        start() {
            if (!this.delay) return;
            clearTimeout(this.timer);
            this.timer = setTimeout(() => { this.show = false; }, this.delay);
        },
        pause() {
            clearTimeout(this.timer);
        },
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
    // Текст берём из data-атрибута: CSP-евалуатор не раскрывает \uXXXX в аргументах.
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

    // === Показ пароля, форма входа (auth/login.html) ===
    Alpine.data('loginForm', () => ({
        showPassword: false,
        toggleVisibility(btnEl) {
            this.showPassword = !this.showPassword;
            btnEl.previousElementSibling.type = this.showPassword ? 'text' : 'password';
        },
    }));

    // === Регистрация (auth/register.html): проверка совпадения паролей.
    // Показ пароля — глобальная togglePassword() в base_auth.html. ===
    Alpine.data('registerForm', () => ({
        password: '',
        confirmPassword: '',
        passwordsMatch: true,
        checkMatch() {
            this.passwordsMatch = this.password === this.confirmPassword;
        },
    }));

    // === Сброс пароля (auth/password_reset_confirm.html): toggle(field, $el) для обоих полей. ===
    Alpine.data('passwordResetForm', () => ({
        showPassword: false,
        showPassword2: false,
        toggle(field, btnEl) {
            this[field] = !this[field];
            btnEl.previousElementSibling.type = this[field] ? 'text' : 'password';
        },
    }));

    // === Контекст просмотра, шаг 1 вайзарда (evaluations/context.html) ===
    // Начальные значения — из data-атрибутов.
    Alpine.data('matchContextForm', () => ({
        supportedTeam: '',
        watchedType: 'full',
        attendedStadium: false,
        // evalMode — «Быстро/Подробно», уходит в POST как eval_mode. По умолчанию 'quick'.
        evalMode: 'quick',
        init() {
            this.supportedTeam = this.$el.dataset.supportedTeam || '';
            this.watchedType = this.$el.dataset.watchedType || 'full';
            this.attendedStadium = this.$el.dataset.attendedStadium === 'true';
            this.evalMode = this.$el.dataset.evalMode || 'quick';

            // «Были на стадионе» + «Голы» не бывает — сбрасываем на «Полный».
            this.$watch('attendedStadium', (value) => {
                if (value && this.watchedType === 'highlights') {
                    this.watchedType = 'full';
                }
            });
        },
    }));

    // === Карточка игрока в вайзарде (evaluations/_player_card.html) ===
    // initialEvaluate — предвключённый тумблер для ключевых игроков в режиме «Быстро».
    Alpine.data('playerEvaluationCard', (initialEvaluate) => ({
        evaluate: !!initialEvaluate,
        contribution: 5,
        risk: 5,
        potential: 5,
    }));

    // === Форма обратной связи (core/contacts.html) ===
    // Начальные значения — из data-атрибутов.
    Alpine.data('contactForm', () => ({
        submitting: false,
        category: 'general',
        categoryOpen: false,
        subject: '',
        message: '',
        email: '',
        screenshot: null,
        screenshotPreview: null,
        // Пункты кастомного дропдауна тем. value = ContactSubmission.CATEGORY_CHOICES.
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
            // Тема из data-initial-subject (кнопка «Сообщить об ошибке в данных»), поле редактируемое.
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

    // === Push-уведомления (users/notification_settings.html) ===
    // csrfToken — из data-атрибута.
    Alpine.data('pushSettings', () => ({
        status: 'checking',
        csrfToken: '',
        // iOS: Web Push работает только у сайта, добавленного на экран «Домой».
        isIOS: false,
        isStandalone: false,
        async init() {
            this.csrfToken = this.$el.dataset.csrfToken || '';
            this.isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream;
            // navigator.standalone — iOS, display-mode: standalone — остальные браузеры.
            this.isStandalone = window.navigator.standalone === true
                || window.matchMedia('(display-mode: standalone)').matches;
            // Список устройств перерисовываем при любом изменении подписок.
            document.addEventListener('dopx:push-devices-changed', () => this.refreshDevices());
            // csrfToken — чтобы dopxPushStatus мог восстановить запись на сервере.
            this.status = await window.dopxPushStatus(this.csrfToken);
            this.markCurrentDevice();
        },
        async refreshDevices() {
            const box = this.$el.querySelector('[data-push-devices]');
            if (!box) return;
            try {
                const res = await fetch(box.dataset.url, { headers: { 'X-Requested-With': 'fetch' } });
                if (res.ok) box.innerHTML = await res.text();
            } catch (err) {
                console.warn('DOPX: push devices refresh failed', err);
            }
            this.markCurrentDevice();
        },
        // Бейдж «это устройство» у записи с endpoint текущего браузера.
        markCurrentDevice() {
            const endpoint = window.dopxPushEndpoint;
            this.$el.querySelectorAll('[data-push-endpoint]').forEach((row) => {
                const badge = row.querySelector('[data-push-current]');
                if (badge) badge.classList.toggle('hidden', !endpoint || row.dataset.pushEndpoint !== endpoint);
            });
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

    // === Обратный отсчёт до матча (components/_match_card.html) ===
    // Дата — из data-kickoff.
    Alpine.data('matchCountdown', () => ({
        countdownText: '',
        _timerId: null,
        init() {
            const kickoffIso = this.$el.dataset.kickoff;
            if (!kickoffIso) return;
            const kickoffMs = new Date(kickoffIso).getTime();
            if (Number.isNaN(kickoffMs)) return;
            this._tick(kickoffMs);
            // Тик раз в 30 с — точности до минуты хватает.
            this._timerId = setInterval(() => this._tick(kickoffMs), 30000);
            // Alpine сам не вызывает destroy() — чистим таймер на htmx:beforeCleanupElement.
            this.$el.addEventListener('htmx:beforeCleanupElement', () => this.destroy(), { once: true });
        },
        destroy() {
            if (this._timerId) clearInterval(this._timerId);
            this._timerId = null;
        },
        _tick(kickoffMs) {
            const diffMs = kickoffMs - Date.now();
            if (diffMs <= 0) {
                this.countdownText = 'Начинается';
                if (this._timerId) clearInterval(this._timerId);
                // Карточка перерисуется сама (статус live, счёт) — без перезагрузки страницы.
                const el = this.$el;
                setTimeout(() => { if (el.isConnected && window.htmx) htmx.trigger(el, 'dopx:wake'); }, 20000);
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

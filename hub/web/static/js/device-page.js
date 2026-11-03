// DroidHub — страница управления устройством (/device/{id}): экран, ввод, приложения, питание
(function () {
  'use strict';
  const { DEVICES_POLL_INTERVAL_MS, SHUTDOWN_WARNING, REBOOT_WARNING, PowerModal, sendPowerAction, formatTemperature, formatBattery } = window.DroidHub;

  /* =========================================================================
     Страница управления устройством (/device/{id})
     ========================================================================= */
  function initDeviceControlPage() {
    const pageEl = document.querySelector('.device-control-page');
    if (!pageEl) return;

    const deviceId = pageEl.dataset.deviceId;
    if (!deviceId) return;

    const screenImg = document.getElementById('screen-img');
    const screenPlaceholder = document.getElementById('screen-placeholder');
    const statusDot = document.getElementById('stream-status-dot');
    const statusText = document.getElementById('stream-status-text');
    const streamUpdating = document.getElementById('stream-updating');
    const frameAgeEl = document.getElementById('frame-age');
    const qualitySelect = document.getElementById('quality-select');
    const btnToggleStream = document.getElementById('btn-toggle-stream');
    const btnRefreshScreen = document.getElementById('btn-refresh-screen');
    const tapIndicator = document.getElementById('tap-indicator');

    // Состояние потока скриншотов
    let streamingActive = true;
    let isFetchingScreenshot = false;
    let needsAnotherFrame = false;
    let screenshotTimer = null;
    let realScreenWidth = 0;
    let realScreenHeight = 0;
    let lastFrameTime = 0;
    const targetIntervalMs = 1000; // Целевой интервал по умолчанию 1 с

    // Селектор качества (макс. ширина 360 / 540 / 720) и сохранение в localStorage
    const STORAGE_KEY_QUALITY = 'screenshot_quality';
    let currentQuality = '720';
    try {
      const savedQuality = localStorage.getItem(STORAGE_KEY_QUALITY) || localStorage.getItem('quality') || localStorage.getItem('max_width');
      if (savedQuality && ['360', '540', '720'].includes(savedQuality)) {
        currentQuality = savedQuality;
      }
    } catch (e) {
      console.warn('Ошибка чтения localStorage:', e);
    }

    if (qualitySelect) {
      qualitySelect.value = currentQuality;
      qualitySelect.addEventListener('change', () => {
        const val = qualitySelect.value;
        if (['360', '540', '720'].includes(val)) {
          currentQuality = val;
          try {
            localStorage.setItem(STORAGE_KEY_QUALITY, val);
            localStorage.setItem('quality', val);
            localStorage.setItem('max_width', val);
          } catch (e) {
            console.warn('Ошибка записи localStorage:', e);
          }
          requestImmediateFrame();
        }
      });
    }

    function setStatus(type, msg) {
      if (statusDot) {
        statusDot.className = 'status-dot';
        if (type === 'active') statusDot.classList.add('active');
        else if (type === 'error') statusDot.classList.add('error');
      }
      if (statusText) {
        statusText.textContent = msg;
      }
    }

    function setUpdating(updating) {
      if (streamUpdating) {
        streamUpdating.style.display = updating ? 'inline' : 'none';
        streamUpdating.textContent = 'обновляется…';
      }
    }

    function updateFrameAge() {
      if (!frameAgeEl || !lastFrameTime) return;
      const ageSec = Math.max(0, Math.floor((Date.now() - lastFrameTime) / 1000));
      frameAgeEl.textContent = `кадр ${ageSec} с назад`;
    }

    // Регулярное обновление возраста кадра
    setInterval(updateFrameAge, 1000);

    /* -----------------------------------------------------------------------
       1. Скриншоты с адаптивным интервалом, индикатором задержки и без наложения
       ----------------------------------------------------------------------- */
    async function fetchScreenshot(force = false) {
      if (isFetchingScreenshot || (!streamingActive && !force) || document.hidden) {
        return;
      }

      isFetchingScreenshot = true;
      setUpdating(true);
      const startTime = Date.now();

      try {
        const maxWidth = currentQuality || '720';
        const url = `/api/devices/${encodeURIComponent(deviceId)}/screenshot?max_width=${encodeURIComponent(maxWidth)}&_t=${Date.now()}`;
        const resp = await fetch(url, { cache: 'no-store' });

        if (!resp.ok) {
          setStatus('error', `Ошибка ${resp.status}`);
          return;
        }

        // Читаем реальные размеры экрана из заголовков ответа
        const sw = resp.headers.get('X-Screen-Width');
        const sh = resp.headers.get('X-Screen-Height');
        if (sw && sh) {
          const w = parseInt(sw, 10);
          const h = parseInt(sh, 10);
          if (w > 0 && h > 0) {
            realScreenWidth = w;
            realScreenHeight = h;
          }
        }

        const blob = await resp.blob();
        const objectUrl = URL.createObjectURL(blob);
        const oldUrl = screenImg.src;

        screenImg.onload = () => {
          if (oldUrl && oldUrl.startsWith('blob:')) {
            URL.revokeObjectURL(oldUrl);
          }
          screenImg.style.display = 'block';
          if (screenPlaceholder) {
            screenPlaceholder.style.display = 'none';
          }
        };
        screenImg.src = objectUrl;
        setStatus('active', 'В сети');
        lastFrameTime = Date.now();
        updateFrameAge();
      } catch (err) {
        setStatus('error', 'Нет связи');
        console.warn('Screenshot fetch failed:', err);
      } finally {
        const frameDuration = Date.now() - startTime;
        isFetchingScreenshot = false;
        setUpdating(false);

        // Пауза обновления, когда вкладка скрыта
        if (streamingActive && !document.hidden) {
          if (needsAnotherFrame) {
            needsAnotherFrame = false;
            fetchScreenshot();
          } else {
            // Адаптивный интервал: max(0, целевой интервал − время предыдущего кадра)
            const delay = Math.max(0, targetIntervalMs - frameDuration);
            screenshotTimer = setTimeout(fetchScreenshot, delay);
          }
        }
      }
    }

    function requestImmediateFrame(force = false) {
      if (screenshotTimer) {
        clearTimeout(screenshotTimer);
        screenshotTimer = null;
      }
      if ((!streamingActive && !force) || document.hidden) {
        return;
      }
      if (isFetchingScreenshot) {
        needsAnotherFrame = true;
      } else {
        fetchScreenshot(force);
      }
    }

    const triggerQuickRefresh = requestImmediateFrame;

    // Пауза обновления, когда вкладка скрыта (visibilitychange)
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        if (screenshotTimer) {
          clearTimeout(screenshotTimer);
          screenshotTimer = null;
        }
      } else {
        if (streamingActive && !isFetchingScreenshot) {
          fetchScreenshot();
        }
      }
    });

    if (btnToggleStream) {
      btnToggleStream.addEventListener('click', () => {
        streamingActive = !streamingActive;
        if (streamingActive) {
          btnToggleStream.textContent = 'Пауза';
          fetchScreenshot();
        } else {
          btnToggleStream.textContent = 'Пуск';
          if (screenshotTimer) {
            clearTimeout(screenshotTimer);
            screenshotTimer = null;
          }
          needsAnotherFrame = false;
          setUpdating(false);
          setStatus('paused', 'Приостановлено');
        }
      });
    }

    if (btnRefreshScreen) {
      btnRefreshScreen.addEventListener('click', () => {
        requestImmediateFrame(true);
      });
    }

    // Запуск первого кадра
    fetchScreenshot();

    /* -----------------------------------------------------------------------
       2. Тап и свайп с пересчетом координат через X-Screen-Width/Height
       ----------------------------------------------------------------------- */
    function getNormalizedCoords(clientX, clientY) {
      if (!screenImg || screenImg.style.display === 'none') return null;

      const rect = screenImg.getBoundingClientRect();
      const displayW = rect.width;
      const displayH = rect.height;
      if (displayW <= 0 || displayH <= 0) return null;

      let relX = clientX - rect.left;
      let relY = clientY - rect.top;

      // Ограничиваем рамками картинки
      relX = Math.max(0, Math.min(displayW, relX));
      relY = Math.max(0, Math.min(displayH, relY));

      const targetW = realScreenWidth > 0 ? realScreenWidth : (screenImg.naturalWidth || displayW);
      const targetH = realScreenHeight > 0 ? realScreenHeight : (screenImg.naturalHeight || displayH);

      const realX = Math.round((relX / displayW) * targetW);
      const realY = Math.round((relY / displayH) * targetH);

      return {
        realX,
        realY,
        clientX,
        clientY,
      };
    }

    let tapTimeout = null;
    function showTapEffect(clientX, clientY) {
      const viewport = document.getElementById('screen-viewport');
      if (!viewport) return;
      const vRect = viewport.getBoundingClientRect();

      // Кружок в точке касания (исчезает через 600 мс)
      const circle = document.createElement('div');
      circle.className = 'tap-indicator active';
      circle.style.left = `${clientX - vRect.left}px`;
      circle.style.top = `${clientY - vRect.top}px`;
      viewport.appendChild(circle);

      if (tapIndicator) {
        if (tapTimeout) clearTimeout(tapTimeout);
        tapIndicator.style.left = `${clientX - vRect.left}px`;
        tapIndicator.style.top = `${clientY - vRect.top}px`;
        tapIndicator.classList.remove('active');
        void tapIndicator.offsetWidth;
        tapIndicator.classList.add('active');
      }

      setTimeout(() => {
        circle.classList.remove('active');
        if (tapIndicator) {
          tapIndicator.classList.remove('active');
        }
        setTimeout(() => {
          if (circle.parentNode) {
            circle.parentNode.removeChild(circle);
          }
        }, 300);
      }, 600);
    }

    async function sendTap(x, y) {
      try {
        await fetch(`/api/devices/${encodeURIComponent(deviceId)}/tap`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ x, y }),
        });
      } catch (err) {
        console.warn('Tap request failed:', err);
      } finally {
        requestImmediateFrame();
      }
    }

    async function sendSwipe(x1, y1, x2, y2, ms) {
      try {
        await fetch(`/api/devices/${encodeURIComponent(deviceId)}/swipe`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ x1, y1, x2, y2, ms: ms || 300 }),
        });
      } catch (err) {
        console.warn('Swipe request failed:', err);
      } finally {
        requestImmediateFrame();
      }
    }

    // Обработка жестов: мышь и тач
    let isPointerDown = false;
    let startCoords = null;
    let lastCoords = null;
    let pointerStartTime = 0;

    function handlePointerStart(clientX, clientY) {
      const coords = getNormalizedCoords(clientX, clientY);
      if (!coords) return;
      isPointerDown = true;
      startCoords = coords;
      lastCoords = coords;
      pointerStartTime = Date.now();
    }

    function handlePointerMove(clientX, clientY) {
      if (!isPointerDown) return;
      const coords = getNormalizedCoords(clientX, clientY);
      if (coords) {
        lastCoords = coords;
      }
    }

    async function handlePointerEnd(clientX, clientY) {
      if (!isPointerDown || !startCoords) {
        isPointerDown = false;
        return;
      }
      isPointerDown = false;

      const endCoords = (clientX !== undefined && clientY !== undefined)
        ? (getNormalizedCoords(clientX, clientY) || lastCoords || startCoords)
        : (lastCoords || startCoords);

      const dx = endCoords.clientX - startCoords.clientX;
      const dy = endCoords.clientY - startCoords.clientY;
      const distance = Math.hypot(dx, dy);
      const duration = Math.max(100, Math.min(1000, Date.now() - pointerStartTime));

      // Если сдвиг меньше 10px — это тап, иначе — свайп
      if (distance < 10) {
        showTapEffect(startCoords.clientX, startCoords.clientY);
        await sendTap(startCoords.realX, startCoords.realY);
      } else {
        await sendSwipe(
          startCoords.realX,
          startCoords.realY,
          endCoords.realX,
          endCoords.realY,
          duration,
        );
      }
    }

    // События мыши
    screenImg.addEventListener('mousedown', (e) => {
      e.preventDefault();
      handlePointerStart(e.clientX, e.clientY);
    });

    window.addEventListener('mousemove', (e) => {
      if (isPointerDown) {
        e.preventDefault();
        handlePointerMove(e.clientX, e.clientY);
      }
    });

    window.addEventListener('mouseup', (e) => {
      if (isPointerDown) {
        e.preventDefault();
        handlePointerEnd(e.clientX, e.clientY);
      }
    });

    // События тача
    screenImg.addEventListener('touchstart', (e) => {
      if (e.touches.length === 1) {
        e.preventDefault();
        handlePointerStart(e.touches[0].clientX, e.touches[0].clientY);
      }
    }, { passive: false });

    screenImg.addEventListener('touchmove', (e) => {
      if (e.touches.length === 1 && isPointerDown) {
        e.preventDefault();
        handlePointerMove(e.touches[0].clientX, e.touches[0].clientY);
      }
    }, { passive: false });

    screenImg.addEventListener('touchend', (e) => {
      if (isPointerDown) {
        e.preventDefault();
        if (e.changedTouches.length === 1) {
          handlePointerEnd(e.changedTouches[0].clientX, e.changedTouches[0].clientY);
        } else {
          handlePointerEnd();
        }
      }
    }, { passive: false });

    screenImg.addEventListener('touchcancel', () => {
      isPointerDown = false;
    });

    /* -----------------------------------------------------------------------
       3. Аппаратные кнопки: Назад / Домой / Недавние / Питание
       ----------------------------------------------------------------------- */
    async function sendKey(keyName) {
      if (!keyName) return;
      try {
        await fetch(`/api/devices/${encodeURIComponent(deviceId)}/key`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: keyName }),
        });
      } catch (err) {
        console.warn(`Key "${keyName}" request failed:`, err);
      } finally {
        requestImmediateFrame();
      }
    }

    document.querySelectorAll('[data-key]').forEach((btn) => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        const key = btn.dataset.key;
        if (key) {
          sendKey(key);
        }
      });
    });

    /* -----------------------------------------------------------------------
       4. Ввод текста
       ----------------------------------------------------------------------- */
    const textForm = document.getElementById('text-form');
    const textInput = document.getElementById('text-input');

    async function sendText(text) {
      if (!text) return;
      try {
        await fetch(`/api/devices/${encodeURIComponent(deviceId)}/text`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text }),
        });
      } catch (err) {
        console.warn('Send text request failed:', err);
      } finally {
        requestImmediateFrame();
      }
    }

    if (textForm && textInput) {
      textForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const text = textInput.value;
        if (!text) return;
        await sendText(text);
        textInput.value = '';
      });
    }

    /* -----------------------------------------------------------------------
       5. Список приложений и запуск
       ----------------------------------------------------------------------- */
    const appsListContainer = document.getElementById('apps-list');
    const appSearchInput = document.getElementById('app-search');
    const btnRefreshApps = document.getElementById('btn-refresh-apps');
    let installedApps = [];

    async function openApp(packageName) {
      if (!packageName) return;
      try {
        await fetch(`/api/devices/${encodeURIComponent(deviceId)}/open_app`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ package: packageName }),
        });
      } catch (err) {
        console.warn('Open app request failed:', err);
      } finally {
        requestImmediateFrame();
      }
    }

    function renderApps(appsToRender) {
      if (!appsListContainer) return;
      appsListContainer.innerHTML = '';

      if (!appsToRender || appsToRender.length === 0) {
        appsListContainer.innerHTML = '<div class="apps-empty">Приложения не найдены</div>';
        return;
      }

      appsToRender.forEach((pkg) => {
        const item = document.createElement('div');
        item.className = 'app-item';

        const nameSpan = document.createElement('span');
        nameSpan.className = 'app-pkg-name';
        nameSpan.textContent = pkg;

        const launchBtn = document.createElement('button');
        launchBtn.type = 'button';
        launchBtn.className = 'btn btn-outline btn-xs app-launch-btn';
        launchBtn.textContent = 'Открыть';

        item.appendChild(nameSpan);
        item.appendChild(launchBtn);

        item.addEventListener('click', () => openApp(pkg));

        appsListContainer.appendChild(item);
      });
    }

    async function loadApps() {
      if (!appsListContainer) return;
      appsListContainer.innerHTML = '<div class="apps-loading">Загрузка приложений...</div>';
      try {
        const resp = await fetch(`/api/devices/${encodeURIComponent(deviceId)}/apps`, {
          cache: 'no-store',
        });
        if (!resp.ok) {
          appsListContainer.innerHTML = '<div class="apps-empty">Не удалось загрузить список</div>';
          return;
        }
        const data = await resp.json();
        if (Array.isArray(data)) {
          installedApps = data;
          filterAndRenderApps();
        } else {
          appsListContainer.innerHTML = '<div class="apps-empty">Пустой список приложений</div>';
        }
      } catch (err) {
        appsListContainer.innerHTML = '<div class="apps-empty">Ошибка загрузки списка</div>';
        console.warn('Load apps failed:', err);
      }
    }

    function filterAndRenderApps() {
      const q = (appSearchInput ? appSearchInput.value : '').trim().toLowerCase();
      if (!q) {
        renderApps(installedApps);
      } else {
        const filtered = installedApps.filter((pkg) => pkg.toLowerCase().includes(q));
        renderApps(filtered);
      }
    }

    if (appSearchInput) {
      appSearchInput.addEventListener('input', filterAndRenderApps);
    }

    if (btnRefreshApps) {
      btnRefreshApps.addEventListener('click', loadApps);
    }

    // Загружаем приложения при открытии страницы
    loadApps();

    /* -----------------------------------------------------------------------
       6. Управление питанием (Разбудить / Экран выкл. / Перезагрузить / Выключить)
       ----------------------------------------------------------------------- */
    const powerCard = document.getElementById('power-card');
    if (powerCard) {
      powerCard.querySelectorAll('[data-power]').forEach((btn) => {
        btn.addEventListener('click', (e) => {
          e.preventDefault();
          const action = btn.dataset.power;
          if (!action) return;

          if (action === 'wake') {
            sendPowerAction(deviceId, 'wake', true).then((ok) => {
              if (ok) {
                requestImmediateFrame();
                fetchDeviceStatus();
              }
            });
          } else if (action === 'sleep') {
            sendPowerAction(deviceId, 'sleep', true).then((ok) => {
              if (ok) {
                requestImmediateFrame();
                fetchDeviceStatus();
              }
            });
          } else if (action === 'reboot') {
            PowerModal.show({
              title: 'Перезагрузка телефона',
              text: REBOOT_WARNING,
              confirmText: 'Перезагрузить',
              confirmClass: 'btn btn-danger',
              onConfirm: async () => {
                const ok = await sendPowerAction(deviceId, 'reboot', true);
                if (ok) {
                  setStatus('warning', 'Перезагрузка...');
                  setTimeout(requestImmediateFrame, 2000);
                  fetchDeviceStatus();
                }
              },
            });
          } else if (action === 'shutdown') {
            PowerModal.show({
              title: 'Выключение телефона',
              text: SHUTDOWN_WARNING,
              confirmText: 'Выключить',
              confirmClass: 'btn btn-danger',
              onConfirm: async () => {
                const ok = await sendPowerAction(deviceId, 'shutdown', true);
                if (ok) {
                  setStatus('error', 'Телефон выключен');
                  fetchDeviceStatus();
                }
              },
            });
          }
        });
      });
    }

    /* -----------------------------------------------------------------------
       7. Телеметрия (температура и заряд) без накладывания запросов
       ----------------------------------------------------------------------- */
    let isFetchingStatus = false;
    let statusPollTimer = null;

    async function fetchDeviceStatus() {
      if (isFetchingStatus || document.hidden) return;
      isFetchingStatus = true;

      try {
        const resp = await fetch('/api/devices', { cache: 'no-store' });
        if (resp.ok) {
          const list = await resp.json();
          if (Array.isArray(list)) {
            const current = list.find((d) => d.id === deviceId);
            if (current) {
              updateDevicePageStatus(current);
            }
          }
        }
      } catch (err) {
        console.warn('Ошибка обновления телеметрии устройства:', err);
      } finally {
        isFetchingStatus = false;
        if (!document.hidden) {
          scheduleNextStatusPoll();
        }
      }
    }

    function scheduleNextStatusPoll() {
      if (statusPollTimer) clearTimeout(statusPollTimer);
      statusPollTimer = setTimeout(fetchDeviceStatus, DEVICES_POLL_INTERVAL_MS);
    }

    function updateDevicePageStatus(status) {
      const batteryEl = document.getElementById('device-battery-val');
      if (batteryEl) {
        const bInfo = formatBattery(status.battery);
        batteryEl.textContent = bInfo.text;
        batteryEl.className = `telemetry-value battery-value ${bInfo.className}`;
      }

      const tempEl = document.getElementById('device-temp-val');
      const tInfo = formatTemperature(status.temperature);
      if (tempEl) {
        tempEl.textContent = tInfo.text;
        tempEl.className = `telemetry-value temp-value ${tInfo.className}`;
      }

      const overheatBadge = document.getElementById('device-overheat-badge');
      if (overheatBadge) {
        overheatBadge.style.display = tInfo.isOverheat ? 'inline-block' : 'none';
      }
    }

    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        if (statusPollTimer) {
          clearTimeout(statusPollTimer);
          statusPollTimer = null;
        }
      } else {
        if (!isFetchingStatus) {
          fetchDeviceStatus();
        }
      }
    });

    scheduleNextStatusPoll();
  }

  window.DroidHub.initDeviceControlPage = initDeviceControlPage;
})();

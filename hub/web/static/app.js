// Phone Hub — Клиентский скрипт управления устройствами (Vanilla JS, без CDN)

(function () {
  'use strict';

  // Инициализация при загрузке DOM
  document.addEventListener('DOMContentLoaded', () => {
    initDeviceControlPage();
    initDevicesListPage();
  });

  /* =========================================================================
     Страница списка устройств (/)
     ========================================================================= */
  function initDevicesListPage() {
    const refreshBtn = document.getElementById('btn-refresh-devices');
    if (!refreshBtn) return;

    refreshBtn.addEventListener('click', async () => {
      refreshBtn.disabled = true;
      const originalText = refreshBtn.textContent;
      refreshBtn.textContent = 'Обновление...';
      try {
        const resp = await fetch('/api/devices', { cache: 'no-store' });
        if (resp.ok) {
          window.location.reload();
        }
      } catch (e) {
        console.warn('Ошибка обновления списка устройств:', e);
      } finally {
        refreshBtn.disabled = false;
        refreshBtn.textContent = originalText;
      }
    });
  }

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
    const btnToggleStream = document.getElementById('btn-toggle-stream');
    const btnRefreshScreen = document.getElementById('btn-refresh-screen');
    const tapIndicator = document.getElementById('tap-indicator');

    // Состояние потока скриншотов
    let streamingActive = true;
    let isFetchingScreenshot = false;
    let screenshotTimer = null;
    let realScreenWidth = 0;
    let realScreenHeight = 0;

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

    /* -----------------------------------------------------------------------
       1. Скриншоты 1-3 раза/с без наложения запросов
       ----------------------------------------------------------------------- */
    async function fetchScreenshot() {
      if (isFetchingScreenshot || !streamingActive) {
        return;
      }

      isFetchingScreenshot = true;
      try {
        const url = `/api/devices/${encodeURIComponent(deviceId)}/screenshot?max_width=720&_t=${Date.now()}`;
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
      } catch (err) {
        setStatus('error', 'Нет связи');
        console.warn('Screenshot fetch failed:', err);
      } finally {
        isFetchingScreenshot = false;
        // Следующий запрос планируется только ПОСЛЕ завершения предыдущего (без наложения!)
        if (streamingActive) {
          screenshotTimer = setTimeout(fetchScreenshot, 500); // ~2 кадра/с (1-3 раза в сек)
        }
      }
    }

    function triggerQuickRefresh() {
      if (screenshotTimer) {
        clearTimeout(screenshotTimer);
        screenshotTimer = null;
      }
      setTimeout(() => {
        if (!isFetchingScreenshot && streamingActive) {
          fetchScreenshot();
        }
      }, 120);
    }

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
          setStatus('paused', 'Приостановлено');
        }
      });
    }

    if (btnRefreshScreen) {
      btnRefreshScreen.addEventListener('click', () => {
        if (!isFetchingScreenshot) {
          fetchScreenshot();
        }
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

    function showTapEffect(clientX, clientY) {
      if (!tapIndicator) return;
      const viewport = document.getElementById('screen-viewport');
      if (!viewport) return;
      const vRect = viewport.getBoundingClientRect();

      tapIndicator.style.left = `${clientX - vRect.left}px`;
      tapIndicator.style.top = `${clientY - vRect.top}px`;
      tapIndicator.classList.remove('active');
      // trigger reflow
      void tapIndicator.offsetWidth;
      tapIndicator.classList.add('active');
      setTimeout(() => tapIndicator.classList.remove('active'), 260);
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
        triggerQuickRefresh();
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
        triggerQuickRefresh();
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
        triggerQuickRefresh();
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
        triggerQuickRefresh();
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
        triggerQuickRefresh();
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
  }
})();

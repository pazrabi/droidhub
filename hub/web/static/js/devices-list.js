// DroidHub — страница списка устройств (/)
(function () {
  'use strict';
  const { DEVICES_POLL_INTERVAL_MS, SHUTDOWN_WARNING, REBOOT_WARNING, PowerModal, sendPowerAction, formatTemperature, formatBattery } = window.DroidHub;

  /* =========================================================================
     Страница списка устройств (/)
     ========================================================================= */
  function initDevicesListPage() {
    const refreshBtn = document.getElementById('btn-refresh-devices');
    const container = document.getElementById('devices-container');
    if (!refreshBtn && !container) return;

    let isUpdatingDevices = false;
    let pollTimer = null;

    function setHubStatus(ok) {
      const pill = document.getElementById('hub-status');
      const upd = document.getElementById('updated-at');
      if (pill) {
        pill.textContent = ok ? 'онлайн' : 'нет связи';
        pill.classList.toggle('is-bad', !ok);
      }
      if (ok && upd) {
        const t = new Date();
        upd.textContent = 'обновлено ' + String(t.getHours()).padStart(2, '0') + ':' + String(t.getMinutes()).padStart(2, '0');
      }
    }
    setHubStatus(true);

    async function fetchDevicesList(manual = false) {
      if (isUpdatingDevices || (document.hidden && !manual)) {
        return;
      }
      isUpdatingDevices = true;
      if (manual && refreshBtn) {
        refreshBtn.disabled = true;
        refreshBtn.textContent = 'Обновление...';
      }

      try {
        const resp = await fetch('/api/devices', { cache: 'no-store' });
        if (resp.ok) {
          const devices = await resp.json();
          if (Array.isArray(devices)) {
            updateDevicesListDOM(devices);
          }
          setHubStatus(true);
        } else {
          setHubStatus(false);
        }
      } catch (e) {
        setHubStatus(false);
        console.warn('Ошибка обновления списка устройств:', e);
      } finally {
        isUpdatingDevices = false;
        if (manual && refreshBtn) {
          refreshBtn.disabled = false;
          refreshBtn.textContent = '↻ Обновить';
        }
        if (!document.hidden) {
          scheduleNextDevicesPoll();
        }
      }
    }

    function scheduleNextDevicesPoll() {
      if (pollTimer) clearTimeout(pollTimer);
      pollTimer = setTimeout(() => fetchDevicesList(false), DEVICES_POLL_INTERVAL_MS);
    }

    function updateDevicesListDOM(devices) {
      devices.forEach((d) => {
        const card = document.getElementById(`card-${d.id}`);
        if (!card) return;

        // Онлайн / оффлайн статус
        const badge = card.querySelector('.device-card-header .badge:not(.busy-badge)');
        if (badge) {
          if (d.online) {
            badge.className = 'badge badge-success';
            badge.textContent = 'В сети';
          } else {
            badge.className = 'badge badge-danger';
            badge.textContent = 'Не в сети';
          }
        }

        // Занятость: кто и что сейчас делает на телефоне
        const busyEl = document.getElementById(`busy-${d.id}`);
        if (busyEl) {
          busyEl.style.display = d.online ? '' : 'none';
          if (d.busy && d.activity) {
            busyEl.className = 'badge busy-badge badge-busy';
            busyEl.textContent = `Занят: ${d.activity.label}`;
            busyEl.title = d.activity.actor ? `Запрос от: ${d.activity.actor}` : '';
          } else {
            busyEl.className = 'badge busy-badge badge-idle';
            busyEl.textContent = 'Свободен';
            busyEl.title = '';
          }
        }

        // Батарея
        const batteryEl = document.getElementById(`battery-${d.id}`);
        if (batteryEl) {
          const bInfo = formatBattery(d.battery);
          batteryEl.textContent = bInfo.text;
          batteryEl.className = `meta-value battery-value ${bInfo.className}`;
        }

        // Температура
        const tempEl = document.getElementById(`temp-${d.id}`);
        const tInfo = formatTemperature(d.temperature);
        if (tempEl) {
          tempEl.textContent = tInfo.text;
          tempEl.className = `meta-value temp-value ${tInfo.className}`;
        }

        // Бейдж «ПЕРЕГРЕВ»
        const overheatBadge = document.getElementById(`overheat-${d.id}`);
        if (overheatBadge) {
          overheatBadge.style.display = tInfo.isOverheat ? 'inline-block' : 'none';
        }

        // Кнопка «Выключить» в карточке (видна только при перегреве)
        const shutdownBtn = document.getElementById(`btn-shutdown-${d.id}`);
        if (shutdownBtn) {
          shutdownBtn.style.display = tInfo.isOverheat ? 'block' : 'none';
        }

        // Статус экрана
        const screenEl = document.getElementById(`screen-status-${d.id}`);
        if (screenEl) {
          if (d.screen_on === true) screenEl.textContent = 'Включен';
          else if (d.screen_on === false) screenEl.textContent = 'Выключен';
          else screenEl.textContent = '—';
        }
      });
    }

    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => {
        if (pollTimer) clearTimeout(pollTimer);
        fetchDevicesList(true);
      });
    }

    // Обработка кнопки выключения в карточке устройства
    if (container) {
      container.addEventListener('click', (e) => {
        const target = e.target.closest('.btn-shutdown-card');
        if (!target) return;
        const devId = target.dataset.deviceId;
        const devName = target.dataset.deviceName || devId;
        if (!devId) return;

        PowerModal.show({
          title: `Выключение ${devName}`,
          text: SHUTDOWN_WARNING,
          confirmText: 'Выключить',
          confirmClass: 'btn btn-danger',
          onConfirm: async () => {
            const ok = await sendPowerAction(devId, 'shutdown', true);
            if (ok) {
              fetchDevicesList(true);
            }
          },
        });
      });
    }

    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        if (pollTimer) {
          clearTimeout(pollTimer);
          pollTimer = null;
        }
      } else {
        if (!isUpdatingDevices) {
          fetchDevicesList(false);
        }
      }
    });

    scheduleNextDevicesPoll();
  }

  window.DroidHub.initDevicesListPage = initDevicesListPage;
})();

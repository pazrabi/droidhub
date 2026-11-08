// DroidHub — общие части: настройки, модалка питания, действия питания, форматирование
(function () {
  'use strict';

  // Интервал опроса статусов устройств (совпадает для списка и страницы устройства)
  const DEVICES_POLL_INTERVAL_MS = 5000;

  // Тексты предупреждений при опасных операциях питания
  const SHUTDOWN_WARNING = 'Выключенный телефон нельзя включить удалённо. Зарядка при подключённом кабеле продолжится. Выключить?';
  const REBOOT_WARNING = 'Телефон будет перезагружен и временно станет недоступен. Перезагрузить?';

  /* =========================================================================
     Модальное окно подтверждения (Перезагрузка / Выключение)
     ========================================================================= */
  const PowerModal = {
    modalEl: null,
    titleEl: null,
    textEl: null,
    confirmBtn: null,
    cancelBtn: null,
    closeBtn: null,
    onConfirmCallback: null,

    init() {
      this.modalEl = document.getElementById('power-modal');
      if (!this.modalEl) return;
      this.titleEl = document.getElementById('modal-title');
      this.textEl = document.getElementById('modal-warning-text');
      this.confirmBtn = document.getElementById('modal-btn-confirm');
      this.cancelBtn = document.getElementById('modal-btn-cancel');
      this.closeBtn = document.getElementById('modal-btn-close');

      const hide = () => this.hide();

      if (this.cancelBtn) this.cancelBtn.addEventListener('click', hide);
      if (this.closeBtn) this.closeBtn.addEventListener('click', hide);

      this.modalEl.addEventListener('click', (e) => {
        if (e.target === this.modalEl) hide();
      });

      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && this.modalEl.style.display !== 'none') {
          hide();
        }
      });

      if (this.confirmBtn) {
        this.confirmBtn.addEventListener('click', async () => {
          if (this.onConfirmCallback) {
            const cb = this.onConfirmCallback;
            this.hide();
            await cb();
          } else {
            this.hide();
          }
        });
      }
    },

    show({ title, text, confirmText, confirmClass, onConfirm }) {
      if (!this.modalEl) return;
      if (this.titleEl) this.titleEl.textContent = title || 'Подтверждение';
      if (this.textEl) this.textEl.textContent = text || '';
      if (this.confirmBtn) {
        this.confirmBtn.textContent = confirmText || 'Подтвердить';
        this.confirmBtn.className = confirmClass || 'btn btn-danger';
      }
      this.onConfirmCallback = onConfirm;
      this.modalEl.style.display = 'flex';
    },

    hide() {
      if (!this.modalEl) return;
      this.modalEl.style.display = 'none';
      this.onConfirmCallback = null;
    },
  };

  /* =========================================================================
     Отправка команд питания (POST /api/devices/{id}/power)
     ========================================================================= */
  async function sendPowerAction(deviceId, action, confirm = true) {
    if (!deviceId || !action) return false;
    try {
      const resp = await fetch(`/api/devices/${encodeURIComponent(deviceId)}/power`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: action, confirm: Boolean(confirm) }),
      });
      if (!resp.ok) {
        let msg = `Ошибка ${resp.status}`;
        try {
          const data = await resp.json();
          if (data && data.detail) msg = data.detail;
        } catch (_) {}
        alert(`Ошибка управления питанием: ${msg}`);
        return false;
      }
      return true;
    } catch (err) {
      console.warn(`Power action "${action}" failed:`, err);
      alert(`Сетевая ошибка при выполнении действия питания: ${err}`);
      return false;
    }
  }

  /* =========================================================================
     Форматирование температуры и заряда с цветами
     ========================================================================= */
  function formatTemperature(temp) {
    if (temp === null || temp === undefined || isNaN(temp)) {
      return { text: '—', className: '', isOverheat: false };
    }
    const val = Number(temp);
    const rounded = Math.round(val * 10) / 10;
    const text = `${rounded}°C`;
    if (val >= 45) {
      return { text, className: 'temp-danger', isOverheat: true };
    }
    if (val >= 40) {
      return { text, className: 'temp-warning', isOverheat: false };
    }
    return { text, className: 'temp-neutral', isOverheat: false };
  }

  function formatBattery(batt, charging) {
    if (batt === null || batt === undefined || isNaN(batt)) {
      return { text: '—', className: '' };
    }
    const val = Number(batt);
    const text = `${Math.round(val)}%` + (charging === true ? ' ⚡ заряжается' : '');
    let className = 'battery-neutral';
    if (val <= 15) {
      className = 'battery-danger';
    } else if (val <= 30) {
      className = 'battery-warning';
    }
    return { text, className };
  }

  window.DroidHub = {
    DEVICES_POLL_INTERVAL_MS, SHUTDOWN_WARNING, REBOOT_WARNING,
    PowerModal, sendPowerAction, formatTemperature, formatBattery,
  };
})();

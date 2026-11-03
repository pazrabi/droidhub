// DroidHub — точка входа: инициализация страниц (скрипты js/*.js подключаются в base.html)
document.addEventListener('DOMContentLoaded', () => {
  const hub = window.DroidHub;
  hub.PowerModal.init();
  hub.initDeviceControlPage();
  hub.initDevicesListPage();
});

// DroidHub — живое видео экрана: WebSocket с H.264 (Annex B) -> WebCodecs VideoDecoder -> canvas
(function () {
  'use strict';

  const FRAME_DURATION_US = 33333;

  class VideoStream {
    static supported() {
      return typeof window.VideoDecoder === 'function' && typeof window.EncodedVideoChunk === 'function';
    }

    // hooks: onConfig({width, height, real_width, real_height}), onFrame(), onClose(reason)
    constructor(deviceId, canvas, hooks) {
      this.deviceId = deviceId;
      this.canvas = canvas;
      this.ctx = canvas.getContext('2d');
      this.hooks = hooks || {};
      this.ws = null;
      this.decoder = null;
      this.counter = 0;
      this.gotKey = false;
      this.closed = false;
    }

    start(width) {
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      const url = `${proto}//${location.host}/api/devices/${encodeURIComponent(this.deviceId)}/stream?width=${encodeURIComponent(width)}`;
      this.ws = new WebSocket(url);
      this.ws.binaryType = 'arraybuffer';
      this.ws.onmessage = (ev) => this._onMessage(ev);
      this.ws.onclose = (ev) => this._finish(ev.code === 1008 ? 'auth' : 'closed');
      this.ws.onerror = () => this._finish('error');
    }

    stop() {
      this.closed = true;
      if (this.ws) { try { this.ws.close(); } catch (e) { /* уже закрыт */ } }
      this._closeDecoder();
    }

    _finish(reason) {
      if (this.closed) return;
      this.closed = true;
      this._closeDecoder();
      if (this.hooks.onClose) this.hooks.onClose(reason);
    }

    _closeDecoder() {
      if (this.decoder && this.decoder.state !== 'closed') {
        try { this.decoder.close(); } catch (e) { /* уже закрыт */ }
      }
      this.decoder = null;
    }

    async _configure(msg) {
      const config = { codec: msg.codec, optimizeForLatency: true, avc: { format: 'annexb' } };
      const support = await VideoDecoder.isConfigSupported(config);
      if (!support.supported) throw new Error(`кодек ${msg.codec} не поддерживается`);
      this._closeDecoder();
      this.gotKey = false;
      this.decoder = new VideoDecoder({
        output: (frame) => this._draw(frame),
        error: () => this._finish('decoder'),
      });
      this.decoder.configure(config);
    }

    _draw(frame) {
      if (this.canvas.width !== frame.displayWidth || this.canvas.height !== frame.displayHeight) {
        this.canvas.width = frame.displayWidth;
        this.canvas.height = frame.displayHeight;
      }
      this.ctx.drawImage(frame, 0, 0);
      frame.close();
      if (this.hooks.onFrame) this.hooks.onFrame();
    }

    async _onMessage(ev) {
      if (typeof ev.data === 'string') {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'config') {
          try {
            await this._configure(msg);
            if (this.hooks.onConfig) this.hooks.onConfig(msg);
          } catch (e) {
            this._finish('unsupported');
          }
        } else if (msg.type === 'error') {
          this._finish('error');
        }
        return;
      }
      if (!this.decoder || this.decoder.state !== 'configured') return;
      const bytes = new Uint8Array(ev.data);
      const isKey = bytes[0] === 1;
      if (!isKey && !this.gotKey) return; // декодер можно начинать только с ключевого кадра
      this.gotKey = true;
      this.decoder.decode(new EncodedVideoChunk({
        type: isKey ? 'key' : 'delta',
        timestamp: this.counter++ * FRAME_DURATION_US,
        data: bytes.subarray(1),
      }));
    }
  }

  window.DroidHub.VideoStream = VideoStream;
})();

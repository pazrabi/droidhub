// DroidHub — живое видео экрана: WebSocket с H.264 -> WebCodecs VideoDecoder (AVCC) -> canvas
(function () {
  'use strict';

  const FRAME_DURATION_US = 33333;

  function b64ToBytes(b64) {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  // Описание декодера (avcC) из SPS и PPS: так формат понимают все браузеры, включая Safari
  function buildAvcC(sps, pps) {
    const out = new Uint8Array(11 + sps.length + pps.length);
    let o = 0;
    out.set([1, sps[1], sps[2], sps[3], 0xFF, 0xE1, sps.length >> 8, sps.length & 0xFF], o); o += 8;
    out.set(sps, o); o += sps.length;
    out.set([1, pps.length >> 8, pps.length & 0xFF], o); o += 3;
    out.set(pps, o);
    return out;
  }

  // Annex B (00 00 00 01 + NAL) -> AVCC (4 байта длины + NAL); SPS и PPS уже в описании, их выкидываем
  function annexbToAvcc(data) {
    const nals = [];
    let start = -1;
    for (let i = 0; i + 3 < data.length; i++) {
      if (data[i] === 0 && data[i + 1] === 0 && data[i + 2] === 0 && data[i + 3] === 1) {
        if (start >= 0) nals.push(data.subarray(start, i));
        start = i + 4;
        i += 3;
      }
    }
    if (start >= 0) nals.push(data.subarray(start));
    const kept = nals.filter((n) => { const t = n[0] & 0x1F; return t !== 7 && t !== 8; });
    const out = new Uint8Array(kept.reduce((sum, n) => sum + 4 + n.length, 0));
    let o = 0;
    for (const n of kept) {
      out[o] = n.length >>> 24; out[o + 1] = (n.length >>> 16) & 0xFF; out[o + 2] = (n.length >>> 8) & 0xFF; out[o + 3] = n.length & 0xFF;
      out.set(n, o + 4);
      o += 4 + n.length;
    }
    return out;
  }

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
      const config = {
        codec: msg.codec,
        optimizeForLatency: true,
        description: buildAvcC(b64ToBytes(msg.sps), b64ToBytes(msg.pps)),
      };
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
        data: annexbToAvcc(bytes.subarray(1)),
      }));
    }
  }

  window.DroidHub.VideoStream = VideoStream;
})();

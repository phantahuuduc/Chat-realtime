/* =====================================================================
   atmosphere.js — nền 4 tầng + canvas hiệu ứng.
   Đây là NƠI DUY NHẤT có requestAnimationFrame trong toàn app.

   Các hệ hạt chạy chung một loop:
     mưa 3 lớp thị sai → tàn lửa vàng → sao lấp lánh 4 cánh → đèn flash
     → sao băng (đầu sáng + đuôi + tia lửa) → gợn nước đáy → chớp trời
     → hạt hiệu ứng theo từ khoá (tim, mưa rào, tuyết, kim tuyến, pháo hoa).
   Người dùng chỉ đổi được hình nền; cường độ hiệu ứng do preset quyết định.
   ===================================================================== */

'use strict';

App.atmosphere = (function () {
  const { dom, util, ui, state: appState } = App;

  const STATIC_ROOT = '/static/';

  const PRESETS = [
    {
      id: 'moscow-rain',
      name: 'Moscow 2008',
      image: `url("${STATIC_ROOT}img/cr7-master.jpg")`,
      thumb: `${STATIC_ROOT}img/cr7-master.jpg`,
      video: true,
      rain: 1,
      ember: 1,
      meteor: 1,
      lightning: 1,
      sparkle: 3.4,
    },
    {
      id: 'stadium-night',
      name: 'Đêm sân cỏ',
      image: `url("${STATIC_ROOT}img/cr7-full-seamless.jpg")`,
      thumb: `${STATIC_ROOT}img/cr7-full-seamless.jpg`,
      video: false,
      rain: 0.25,
      ember: 1.8,
      meteor: 0.6,
      lightning: 0,
      sparkle: 3.8,
    },
    {
      id: 'deep-space',
      name: 'Vũ trụ',
      image: 'radial-gradient(120% 90% at 65% 25%, #241a52 0%, #0d1030 45%, #04050f 100%)',
      thumbCss: 'radial-gradient(120% 90% at 65% 25%, #241a52 0%, #0d1030 45%, #04050f 100%)',
      video: false,
      rain: 0,
      ember: 2.2,
      meteor: 3.4,
      lightning: 0,
      sparkle: 3.0,
    },
    {
      id: 'pitch-green',
      name: 'Sắc cỏ',
      image: 'linear-gradient(160deg, #0d3524 0%, #07231a 45%, #03100c 100%)',
      thumbCss: 'linear-gradient(160deg, #0d3524 0%, #07231a 45%, #03100c 100%)',
      video: false,
      rain: 0.5,
      ember: 1.6,
      meteor: 0,
      lightning: 0,
      sparkle: 1.6,
    },
    {
      id: 'midnight-gold',
      name: 'Vàng đêm',
      image: 'radial-gradient(130% 100% at 20% 10%, #3a2a08 0%, #16110a 40%, #05070f 100%)',
      thumbCss: 'radial-gradient(130% 100% at 20% 10%, #3a2a08 0%, #16110a 40%, #05070f 100%)',
      video: false,
      rain: 0,
      ember: 3,
      meteor: 1.4,
      lightning: 0,
      sparkle: 3.6,
    },
    {
      id: 'minimal-dark',
      name: 'Tối giản',
      image: 'linear-gradient(180deg, #0a0f1c 0%, #05070f 100%)',
      thumbCss: 'linear-gradient(180deg, #0a0f1c 0%, #05070f 100%)',
      video: false,
      rain: 0,
      ember: 0,
      meteor: 0,
      lightning: 0,
      sparkle: 0,
    },
  ];

  const MAX_UPLOAD_BYTES = 5 * 1024 * 1024;
  const ACCEPTED_TYPES = ['image/jpeg', 'image/png', 'image/webp'];

  // Mưa 3 lớp thị sai: xa mờ chậm → gần sáng nhanh.
  const RAIN_LAYERS = [
    { share: 0.45, speed: [4, 7], len: [6, 12], alpha: [0.05, 0.11], width: 0.6, drift: 0.5 },
    { share: 0.35, speed: [8, 12], len: [12, 22], alpha: [0.12, 0.22], width: 1.0, drift: 0.35 },
    { share: 0.20, speed: [14, 20], len: [22, 38], alpha: [0.24, 0.40], width: 1.5, drift: 0.2 },
  ];

  const state = {
    preset: PRESETS[0],
    ctx: null,
    width: 0,
    height: 0,
    dpr: 0,
    rafId: null,
    running: false,
    lastTs: 0,
    slowFrames: 0,
    density: 1,
    rain: [],
    embers: [],
    meteors: [],
    sparks: [],
    splashes: [],
    glints: [],        // sao lấp lánh 4 cánh
    flashes: [],       // đèn flash phóng viên
    burst: [],         // hạt của hiệu ứng theo từ khoá
    rainBoost: 0,      // mưa rào tạm thời, tính bằng ms
    flash: 0,
    bolt: null,
    nextMeteorAt: 0,
    nextLightningAt: 0,
    nextGlintAt: 0,
    nextFlashAt: 0,
    videoOk: false,
  };

  const rand = (min, max) => min + Math.random() * (max - min);

  // -------------------------------------------------------------------
  // Khởi tạo hạt
  // -------------------------------------------------------------------

  function rainBudget() {
    const base = state.width < 768 ? 70 : 150;
    // Video mưa hỏng hoặc preset không có video -> canvas gánh phần mưa.
    return Math.round(base * (state.videoOk ? 1 : 1.5));
  }

  function makeDrop(layer) {
    return {
      layer,
      x: Math.random() * state.width,
      y: Math.random() * state.height,
      len: rand(layer.len[0], layer.len[1]),
      speed: rand(layer.speed[0], layer.speed[1]),
      alpha: rand(layer.alpha[0], layer.alpha[1]),
      drift: rand(-layer.drift, layer.drift),
    };
  }

  function makeEmber() {
    return {
      x: Math.random() * state.width,
      y: rand(0, state.height),
      r: rand(0.7, 2.0),
      alpha: rand(0.10, 0.34),
      speed: rand(0.10, 0.34),
      phase: Math.random() * Math.PI * 2,
      sway: rand(0.15, 0.6),
      warm: Math.random() < 0.7,   // đa số vàng ấm, còn lại trắng lạnh
    };
  }

  function makeMeteor() {
    const fromTop = Math.random() < 0.62;
    const speed = rand(11, 19);
    return {
      x: fromTop ? state.width * rand(0.25, 1.05) : state.width + 60,
      y: fromTop ? -40 : state.height * rand(0.02, 0.34),
      vx: -speed * rand(0.78, 1.12),
      vy: speed * rand(0.42, 0.72),
      tail: rand(230, 420),
      head: rand(2.4, 4.2),
      life: 0,
      maxLife: rand(1100, 1750),
      sparkAt: 0,
    };
  }

  function makeSpark(meteor) {
    return {
      x: meteor.x,
      y: meteor.y,
      vx: meteor.vx * rand(0.10, 0.30) + rand(-0.8, 0.8),
      vy: meteor.vy * rand(0.10, 0.30) + rand(-0.4, 0.8),
      r: rand(0.6, 1.6),
      life: 0,
      maxLife: rand(260, 620),
    };
  }

  function makeGlint() {
    const big = Math.random() < 0.18;   // thỉnh thoảng một ngôi sao lớn
    return {
      x: state.width * rand(0.16, 0.99),
      y: state.height * rand(0.04, 0.86),
      size: rand(1.2, 2.6),
      maxSize: big ? rand(13, 20) : rand(5, 11),
      growth: rand(0.4, 0.85),
      rotation: Math.random() * Math.PI,
      rotSpeed: rand(0.008, 0.022),
      life: 0,
      maxLife: rand(700, 1400),
    };
  }

  function makeFlash() {
    return {
      x: state.width * rand(0.28, 0.98),
      y: state.height * rand(0.04, 0.55),
      r: rand(10, 24),
      maxR: rand(38, 78),
      alpha: rand(0.5, 0.85),
      decay: rand(0.035, 0.06),
    };
  }

  /**
   * Tia sáng 4 cánh. Tia vẽ bằng gradient mảnh dần về đầu mút thay vì
   * đường kẻ dày, nên nhìn thanh chứ không thô; lõi là quầng mềm.
   */
  function drawGlints(ctx, deltaMs) {
    for (let i = state.glints.length - 1; i >= 0; i -= 1) {
      const g = state.glints[i];
      g.life += deltaMs;
      if (g.life >= g.maxLife) { state.glints.splice(i, 1); continue; }

      const t = g.life / g.maxLife;
      const alpha = t < 0.16
        ? easeOutQuad(t / 0.16)
        : 1 - easeOutQuad((t - 0.16) / 0.84);
      if (g.size < g.maxSize) g.size += g.growth;
      g.rotation += g.rotSpeed;

      const long = g.size * 6.4;    // tia dài -> nhìn mảnh và thanh
      const short = g.size * 2.2;

      ctx.save();
      ctx.translate(g.x, g.y);
      ctx.rotate(g.rotation);
      ctx.lineCap = 'round';

      const ray = (len, width, colour, fade) => {
        const grad = ctx.createLinearGradient(-len, 0, len, 0);
        grad.addColorStop(0, `rgba(${colour},0)`);
        grad.addColorStop(0.5, `rgba(${colour},${alpha * fade})`);
        grad.addColorStop(1, `rgba(${colour},0)`);
        ctx.strokeStyle = grad;
        ctx.lineWidth = width;
        ctx.beginPath();
        ctx.moveTo(-len, 0);
        ctx.lineTo(len, 0);
        ctx.stroke();
      };

      ray(long, 0.9, '255,255,255', 1);
      ctx.rotate(Math.PI / 2);
      ray(long, 0.9, '255,255,255', 1);
      ctx.rotate(-Math.PI / 4);
      ray(short, 0.6, '255,236,178', 0.7);
      ctx.rotate(Math.PI / 2);
      ray(short, 0.6, '255,236,178', 0.7);
      ctx.rotate(-Math.PI / 4);

      const core = ctx.createRadialGradient(0, 0, 0, 0, 0, g.size * 1.5);
      core.addColorStop(0, `rgba(255,255,255,${alpha})`);
      core.addColorStop(0.4, `rgba(255,244,214,${alpha * 0.55})`);
      core.addColorStop(1, 'rgba(255,236,178,0)');
      ctx.fillStyle = core;
      ctx.beginPath();
      ctx.arc(0, 0, g.size * 1.5, 0, Math.PI * 2);
      ctx.fill();

      ctx.lineCap = 'butt';
      ctx.restore();
    }
  }

  function drawFlashes(ctx) {
    for (let i = state.flashes.length - 1; i >= 0; i -= 1) {
      const f = state.flashes[i];
      const grad = ctx.createRadialGradient(f.x, f.y, 0, f.x, f.y, f.r);
      grad.addColorStop(0, `rgba(255,255,255,${f.alpha})`);
      grad.addColorStop(0.35, `rgba(214,236,255,${f.alpha * 0.55})`);
      grad.addColorStop(1, 'rgba(255,255,255,0)');
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.arc(f.x, f.y, f.r, 0, Math.PI * 2);
      ctx.fill();

      f.r += (f.maxR - f.r) * 0.24;
      f.alpha -= f.decay;
      if (f.alpha <= 0) state.flashes.splice(i, 1);
    }
  }

  function seedParticles() {
    const preset = state.preset;
    const scale = state.density;

    state.rain = [];
    if (preset.rain) {
      const total = Math.round(rainBudget() * preset.rain * scale);
      RAIN_LAYERS.forEach((layer) => {
        const count = Math.round(total * layer.share);
        for (let i = 0; i < count; i += 1) state.rain.push(makeDrop(layer));
      });
    }

    state.embers = preset.ember
      ? Array.from({ length: Math.round(34 * preset.ember * scale) }, makeEmber)
      : [];

    state.meteors = [];
    state.sparks = [];
    state.splashes = [];
    state.glints = [];
    state.flashes = [];
    scheduleMeteor();
    scheduleLightning();
    scheduleSparkle();
  }

  function scheduleSparkle() {
    const rate = state.preset.sparkle;
    state.nextGlintAt = rate ? performance.now() + rand(280, 900) / rate : Infinity;
    state.nextFlashAt = rate ? performance.now() + rand(900, 2600) / rate : Infinity;
  }

  function scheduleMeteor() {
    const rate = state.preset.meteor;
    state.nextMeteorAt = rate ? performance.now() + rand(1800, 5200) / rate : Infinity;
  }

  function scheduleLightning() {
    const rate = state.preset.lightning;
    state.nextLightningAt = rate ? performance.now() + rand(14000, 38000) / rate : Infinity;
  }

  // -------------------------------------------------------------------
  // Vẽ
  // -------------------------------------------------------------------

  function drawRain(ctx) {
    for (const d of state.rain) {
      ctx.strokeStyle = `rgba(214,232,255,${d.alpha})`;
      ctx.lineWidth = d.layer.width;
      ctx.beginPath();
      ctx.moveTo(d.x, d.y);
      ctx.lineTo(d.x + d.drift * d.len, d.y + d.len);
      ctx.stroke();

      d.x += d.drift;
      d.y += d.speed;

      if (d.y > state.height) {
        // Lớp gần nhất chạm đáy thì bắn gợn nước.
        if (d.layer.width >= 1.5 && Math.random() < 0.22 && state.splashes.length < 26) {
          state.splashes.push({ x: d.x, r: 1, alpha: 0.32 });
        }
        d.y = -d.len;
        d.x = Math.random() * state.width;
      }
    }
  }

  function drawEmbers(ctx) {
    for (const m of state.embers) {
      const radius = m.r * 4;
      const core = m.warm ? '255,214,128' : '214,232,255';
      const glow = ctx.createRadialGradient(m.x, m.y, 0, m.x, m.y, radius);
      glow.addColorStop(0, `rgba(${core},${m.alpha})`);
      glow.addColorStop(1, `rgba(${core},0)`);
      ctx.fillStyle = glow;
      ctx.beginPath();
      ctx.arc(m.x, m.y, radius, 0, Math.PI * 2);
      ctx.fill();

      m.phase += 0.012;
      m.x += Math.sin(m.phase) * m.sway * 0.35;
      m.y -= m.speed;
      if (m.y < -6) {
        m.y = state.height + 6;
        m.x = Math.random() * state.width;
      }
    }
  }

  function drawSplashes(ctx) {
    for (let i = state.splashes.length - 1; i >= 0; i -= 1) {
      const s = state.splashes[i];
      ctx.strokeStyle = `rgba(214,232,255,${s.alpha})`;
      ctx.lineWidth = 0.9;
      ctx.beginPath();
      ctx.ellipse(s.x, state.height - 3, s.r * 2.4, s.r * 0.7, 0, 0, Math.PI * 2);
      ctx.stroke();

      s.r += 0.5;
      s.alpha -= 0.022;
      if (s.alpha <= 0) state.splashes.splice(i, 1);
    }
  }

  // -------------------------------------------------------------------
  // Hiệu ứng theo từ khoá trong tin nhắn
  // -------------------------------------------------------------------

  const CONFETTI_COLORS = ['255,214,102', '255,120,140', '120,200,255', '150,240,180', '210,160,255'];

  function heartPath(ctx, size) {
    const s = size;
    ctx.beginPath();
    ctx.moveTo(0, s * 0.35);
    ctx.bezierCurveTo(0, 0, -s, 0, -s, s * 0.35);
    ctx.bezierCurveTo(-s, s * 0.82, 0, s * 1.12, 0, s * 1.5);
    ctx.bezierCurveTo(0, s * 1.12, s, s * 0.82, s, s * 0.35);
    ctx.bezierCurveTo(s, 0, 0, 0, 0, s * 0.35);
    ctx.closePath();
  }

  function spawnBurst(kind, count, factory) {
    for (let i = 0; i < count; i += 1) state.burst.push(factory(i));
    if (state.burst.length > 420) state.burst.splice(0, state.burst.length - 420);
    start();
  }

  /** API công khai: chat.js gọi khi tin nhắn chứa từ khoá. */
  function celebrate(kind) {
    if (util.prefersReducedMotion()) return;
    if (state.width <= 0 || state.height <= 0) return;   // chưa đo được khung

    switch (kind) {
      case 'hearts':
        spawnBurst(kind, 34, () => ({
          kind: 'heart',
          x: rand(0, state.width),
          y: rand(-state.height * 0.45, state.height * 0.9),
          vy: rand(1.6, 3.6),
          size: rand(7, 16),
          phase: Math.random() * Math.PI * 2,
          sway: rand(0.4, 1.3),
          spin: rand(-0.02, 0.02),
          angle: rand(-0.3, 0.3),
          life: 0,
          maxLife: rand(4200, 6800),
        }));
        break;

      case 'rain':
        state.rainBoost = 6000;
        spawnBurst(kind, 90, () => ({
          kind: 'drop',
          x: rand(0, state.width),
          y: rand(-state.height, state.height),
          vy: rand(16, 26),
          len: rand(26, 52),
          alpha: rand(0.3, 0.6),
          life: 0,
          maxLife: 6000,
        }));
        break;

      case 'snow':
        spawnBurst(kind, 70, () => ({
          kind: 'snow',
          x: rand(0, state.width),
          y: rand(-state.height * 0.6, state.height),
          vy: rand(0.7, 1.8),
          r: rand(1.2, 3.4),
          phase: Math.random() * Math.PI * 2,
          sway: rand(0.3, 1.1),
          life: 0,
          maxLife: rand(6000, 9000),
        }));
        break;

      case 'confetti':
        spawnBurst(kind, 90, () => ({
          kind: 'confetti',
          x: rand(0, state.width),
          y: rand(-state.height * 0.4, state.height * 0.85),
          vy: rand(2.4, 5),
          vx: rand(-1.2, 1.2),
          w: rand(4, 9),
          h: rand(7, 14),
          color: CONFETTI_COLORS[Math.floor(Math.random() * CONFETTI_COLORS.length)],
          angle: Math.random() * Math.PI,
          spin: rand(-0.16, 0.16),
          flip: Math.random() * Math.PI,
          flipSpeed: rand(0.08, 0.2),
          life: 0,
          maxLife: rand(3600, 5600),
        }));
        break;

      case 'fireworks':
        for (let shell = 0; shell < 3; shell += 1) {
          const cx = state.width * rand(0.3, 0.9);
          const cy = state.height * rand(0.15, 0.5);
          const hue = CONFETTI_COLORS[Math.floor(Math.random() * CONFETTI_COLORS.length)];
          spawnBurst(kind, 46, () => {
            const angle = Math.random() * Math.PI * 2;
            const speed = rand(1.6, 6.2);
            return {
              kind: 'firework',
              x: cx,
              y: cy,
              vx: Math.cos(angle) * speed,
              vy: Math.sin(angle) * speed,
              r: rand(1, 2.6),
              color: hue,
              life: 0,
              maxLife: rand(900, 1700),
            };
          });
        }
        break;

      case 'stars':
        for (let i = 0; i < 16; i += 1) state.glints.push(makeGlint());
        start();
        break;

      default:
        break;
    }
  }

  function drawBurst(ctx, deltaMs) {
    for (let i = state.burst.length - 1; i >= 0; i -= 1) {
      const p = state.burst[i];
      p.life += deltaMs;
      const left = 1 - p.life / p.maxLife;
      if (left <= 0 || p.y > state.height + 80) { state.burst.splice(i, 1); continue; }
      const fade = Math.min(1, left * 3);

      if (p.kind === 'heart') {
        p.phase += 0.03;
        p.x += Math.sin(p.phase) * p.sway;
        p.y += p.vy;
        p.angle += p.spin;
        ctx.save();
        ctx.translate(p.x, p.y);
        ctx.rotate(p.angle);
        const grad = ctx.createLinearGradient(0, 0, 0, p.size * 1.5);
        grad.addColorStop(0, `rgba(255,140,170,${0.95 * fade})`);
        grad.addColorStop(1, `rgba(214,32,72,${0.85 * fade})`);
        ctx.fillStyle = grad;
        heartPath(ctx, p.size);
        ctx.fill();
        ctx.restore();
      } else if (p.kind === 'drop') {
        p.y += p.vy;
        if (p.y > state.height) { p.y = -p.len; p.x = rand(0, state.width); }
        ctx.strokeStyle = `rgba(198,226,255,${p.alpha * fade})`;
        ctx.lineWidth = 1.4;
        ctx.beginPath();
        ctx.moveTo(p.x, p.y);
        ctx.lineTo(p.x - 2, p.y + p.len);
        ctx.stroke();
      } else if (p.kind === 'snow') {
        p.phase += 0.02;
        p.x += Math.sin(p.phase) * p.sway;
        p.y += p.vy;
        if (p.y > state.height) { p.y = -6; p.x = rand(0, state.width); }
        ctx.fillStyle = `rgba(238,246,255,${0.85 * fade})`;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fill();
      } else if (p.kind === 'confetti') {
        p.x += p.vx;
        p.y += p.vy;
        p.angle += p.spin;
        p.flip += p.flipSpeed;
        ctx.save();
        ctx.translate(p.x, p.y);
        ctx.rotate(p.angle);
        ctx.scale(1, Math.abs(Math.cos(p.flip)) * 0.85 + 0.15);
        ctx.fillStyle = `rgba(${p.color},${0.95 * fade})`;
        ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h);
        ctx.restore();
      } else if (p.kind === 'firework') {
        p.x += p.vx;
        p.y += p.vy;
        p.vy += 0.045;      // trọng lực
        p.vx *= 0.985;
        const glow = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.r * 4);
        glow.addColorStop(0, `rgba(${p.color},${0.95 * fade})`);
        glow.addColorStop(1, `rgba(${p.color},0)`);
        ctx.fillStyle = glow;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r * 4, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  }

  const easeOutQuad = (t) => t * (2 - t);

  function drawMeteors(ctx, deltaMs) {
    for (let i = state.meteors.length - 1; i >= 0; i -= 1) {
      const m = state.meteors[i];
      m.life += deltaMs;
      const progress = m.life / m.maxLife;
      if (progress >= 1) { state.meteors.splice(i, 1); continue; }

      const alpha = progress < 0.16
        ? easeOutQuad(progress / 0.16)
        : 1 - easeOutQuad((progress - 0.16) / 0.84);

      m.x += m.vx;
      m.y += m.vy;

      const norm = Math.hypot(m.vx, m.vy) || 1;
      const tx = m.x - (m.vx / norm) * m.tail;
      const ty = m.y - (m.vy / norm) * m.tail;

      // Đuôi: 3 đoạn, lineWidth nhỏ dần, trắng ấm -> vàng -> tắt.
      const grad = ctx.createLinearGradient(m.x, m.y, tx, ty);
      grad.addColorStop(0, `rgba(255,255,248,${alpha})`);
      grad.addColorStop(0.22, `rgba(255,236,168,${0.8 * alpha})`);
      grad.addColorStop(0.55, `rgba(255,157,61,${0.34 * alpha})`);
      grad.addColorStop(1, 'rgba(201,162,39,0)');
      ctx.strokeStyle = grad;
      ctx.lineCap = 'round';

      const widths = [m.head, m.head * 0.55, m.head * 0.25];
      for (let seg = 0; seg < 3; seg += 1) {
        const from = seg / 3;
        const to = (seg + 1) / 3;
        ctx.lineWidth = widths[seg];
        ctx.beginPath();
        ctx.moveTo(m.x + (tx - m.x) * from, m.y + (ty - m.y) * from);
        ctx.lineTo(m.x + (tx - m.x) * to, m.y + (ty - m.y) * to);
        ctx.stroke();
      }
      ctx.lineCap = 'butt';

      // Đầu sao băng: quầng sáng tròn.
      const halo = m.head * 9;
      const headGlow = ctx.createRadialGradient(m.x, m.y, 0, m.x, m.y, halo);
      headGlow.addColorStop(0, `rgba(255,255,255,${alpha})`);
      headGlow.addColorStop(0.22, `rgba(255,244,206,${0.75 * alpha})`);
      headGlow.addColorStop(0.55, `rgba(255,157,61,${0.3 * alpha})`);
      headGlow.addColorStop(1, 'rgba(245,217,122,0)');
      ctx.fillStyle = headGlow;
      ctx.beginPath();
      ctx.arc(m.x, m.y, halo, 0, Math.PI * 2);
      ctx.fill();

      // Tia lửa rụng dọc đường bay.
      m.sparkAt += deltaMs;
      if (m.sparkAt > 45 && state.sparks.length < 90) {
        m.sparkAt = 0;
        state.sparks.push(makeSpark(m));
      }

      if (m.x < -m.tail || m.y > state.height + m.tail) state.meteors.splice(i, 1);
    }
  }

  function drawSparks(ctx, deltaMs) {
    for (let i = state.sparks.length - 1; i >= 0; i -= 1) {
      const s = state.sparks[i];
      s.life += deltaMs;
      const left = 1 - s.life / s.maxLife;
      if (left <= 0) { state.sparks.splice(i, 1); continue; }

      s.x += s.vx;
      s.y += s.vy;
      s.vy += 0.02;          // hơi rơi xuống

      ctx.fillStyle = `rgba(255,226,150,${0.7 * left})`;
      ctx.beginPath();
      ctx.arc(s.x, s.y, s.r * left, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function makeBolt() {
    const points = [{ x: state.width * rand(0.2, 0.85), y: -10 }];
    const target = state.height * rand(0.32, 0.6);
    while (points[points.length - 1].y < target) {
      const last = points[points.length - 1];
      points.push({ x: last.x + rand(-38, 38), y: last.y + rand(18, 46) });
    }
    return { points, alpha: 1 };
  }

  function drawLightning(ctx, deltaMs) {
    if (state.flash > 0) {
      ctx.fillStyle = `rgba(186,214,255,${state.flash * 0.13})`;
      ctx.fillRect(0, 0, state.width, state.height);
      state.flash = Math.max(0, state.flash - deltaMs / 260);
    }

    if (!state.bolt) return;
    const bolt = state.bolt;
    ctx.strokeStyle = `rgba(226,240,255,${bolt.alpha * 0.55})`;
    ctx.lineWidth = 1.6;
    ctx.beginPath();
    bolt.points.forEach((pt, i) => (i ? ctx.lineTo(pt.x, pt.y) : ctx.moveTo(pt.x, pt.y)));
    ctx.stroke();

    bolt.alpha -= deltaMs / 420;
    if (bolt.alpha <= 0) state.bolt = null;
  }

  // -------------------------------------------------------------------
  // Vòng lặp — duy nhất trong app
  // -------------------------------------------------------------------

  function frame(ts) {
    if (!state.running) return;

    const delta = state.lastTs ? Math.min(ts - state.lastTs, 64) : 16;
    state.lastTs = ts;

    // Ngân sách hiệu năng: chậm liên tục thì giảm hạt.
    if (delta > 32) {
      state.slowFrames += 1;
      if (state.slowFrames >= 30 && state.density > 0.25) {
        state.density *= 0.5;
        state.slowFrames = 0;
        seedParticles();
      }
    } else if (state.slowFrames > 0) {
      state.slowFrames -= 1;
    }

    const ctx = state.ctx;
    ctx.clearRect(0, 0, state.width, state.height);

    if (state.rain.length) { drawRain(ctx); drawSplashes(ctx); }
    if (state.embers.length) drawEmbers(ctx);

    if (ts >= state.nextGlintAt) {
      state.glints.push(makeGlint());
      if (Math.random() < 0.35) state.glints.push(makeGlint());
      state.nextGlintAt = ts + rand(280, 900) / state.preset.sparkle;
    }
    if (ts >= state.nextFlashAt) {
      state.flashes.push(makeFlash());
      state.nextFlashAt = ts + rand(900, 2600) / state.preset.sparkle;
    }
    if (state.flashes.length) drawFlashes(ctx);
    if (state.glints.length) drawGlints(ctx, delta);

    if (ts >= state.nextMeteorAt && state.meteors.length < 3) {
      state.meteors.push(makeMeteor());
      // Thỉnh thoảng bay đôi cho bất ngờ.
      if (Math.random() < 0.22 && state.meteors.length < 3) state.meteors.push(makeMeteor());
      scheduleMeteor();
    }
    if (state.meteors.length) drawMeteors(ctx, delta);
    if (state.sparks.length) drawSparks(ctx, delta);

    if (ts >= state.nextLightningAt) {
      state.bolt = makeBolt();
      state.flash = 1;
      scheduleLightning();
    }
    drawLightning(ctx, delta);

    if (state.burst.length) drawBurst(ctx, delta);
    if (state.rainBoost > 0) state.rainBoost = Math.max(0, state.rainBoost - delta);

    state.rafId = requestAnimationFrame(frame);
  }

  function start() {
    if (state.running || !state.ctx || !canAnimate()) return;
    state.running = true;
    state.lastTs = 0;
    state.rafId = requestAnimationFrame(frame);
  }

  function stop() {
    state.running = false;
    if (state.rafId) cancelAnimationFrame(state.rafId);
    state.rafId = null;
    if (state.ctx) state.ctx.clearRect(0, 0, state.width, state.height);
  }

  function canAnimate() {
    if (util.prefersReducedMotion()) return false;
    // Hạt của hiệu ứng theo từ khoá phải chạy được kể cả ở preset tắt hết nền.
    if (state.burst.length || state.glints.length) return true;
    const p = state.preset;
    return Boolean(p.rain || p.ember || p.meteor || p.lightning || p.sparkle);
  }

  // -------------------------------------------------------------------
  // Kích thước canvas
  // -------------------------------------------------------------------

  function viewportSize() {
    const rect = dom.atmosCanvas.getBoundingClientRect();
    return {
      width: window.innerWidth || document.documentElement.clientWidth || rect.width,
      height: window.innerHeight || document.documentElement.clientHeight || rect.height,
    };
  }

  function resize() {
    const canvas = dom.atmosCanvas;
    if (!canvas) return;
    const { width, height } = viewportSize();

    // Cửa sổ bị ẩn/thu nhỏ trả về 0. Nhận giá trị đó sẽ dồn toàn bộ hạt về
    // x = 0 rồi cắt sạch chúng — giữ nguyên kích thước cũ và chờ lần sau.
    if (width <= 0 || height <= 0) return;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (width === state.width && height === state.height && dpr === state.dpr && state.ctx) {
      return;
    }

    state.dpr = dpr;
    state.width = width;
    state.height = height;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    state.ctx = canvas.getContext('2d');
    state.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    seedParticles();
  }

  // -------------------------------------------------------------------
  // Nền — preset, ảnh tuỳ chọn, crossfade
  // -------------------------------------------------------------------

  function presetById(id) {
    return PRESETS.find((p) => p.id === id) || PRESETS[0];
  }

  function currentImageCss() {
    const p = appState.prefs;
    if (p.theme_id === 'custom' && p.custom_bg_url) return `url("${p.custom_bg_url}")`;
    return presetById(p.theme_id).image;
  }

  function preload(imageCss) {
    const match = /url\("(.+?)"\)/.exec(imageCss);
    if (!match) return Promise.resolve();
    return new Promise((resolve) => {
      const img = new Image();
      img.onload = resolve;
      img.onerror = resolve;
      img.src = match[1];
    });
  }

  /** Crossfade 600ms: preload xong mới fade lớp mới đè lên lớp cũ. */
  async function swapBackground(imageCss) {
    const root = document.documentElement;
    if (!root.style.getPropertyValue('--atmos-image')) {
      root.style.setProperty('--atmos-image', imageCss);
      return;
    }
    await preload(imageCss);
    dom.atmosImageNext.style.backgroundImage = imageCss;
    dom.atmos.classList.add('atmos--swapping');
    await new Promise((r) => setTimeout(r, 600));
    root.style.setProperty('--atmos-image', imageCss);
    dom.atmos.classList.remove('atmos--swapping');
    dom.atmosImageNext.style.backgroundImage = '';
  }

  function applyPrefs() {
    const p = appState.prefs;
    state.preset = p.theme_id === 'custom' ? PRESETS[0] : presetById(p.theme_id);

    document.body.classList.toggle('atmos-no-rain', !state.preset.video);
    swapBackground(currentImageCss());

    state.density = 1;
    state.slowFrames = 0;
    seedParticles();

    if (canAnimate()) start(); else stop();
  }

  // -------------------------------------------------------------------
  // Video mưa
  // -------------------------------------------------------------------

  function initVideo() {
    const video = dom.atmosVideo;
    if (!video) { document.body.classList.add('no-video'); return; }

    const fail = () => {
      document.body.classList.add('no-video');
      state.videoOk = false;
      seedParticles();
    };
    const ok = () => {
      document.body.classList.remove('no-video');
      state.videoOk = true;
      seedParticles();
    };

    video.addEventListener('error', fail);
    video.addEventListener('stalled', fail);
    video.addEventListener('playing', ok);

    const tryPlay = () => {
      const attempt = video.play();
      if (attempt && typeof attempt.catch === 'function') attempt.catch(fail);
    };

    tryPlay();
    // Autoplay bị chặn: nền tĩnh hiện ngay, thử phát lại ở tương tác đầu tiên.
    ['pointerdown', 'keydown'].forEach((evt) =>
      window.addEventListener(evt, () => { if (video.paused) tryPlay(); }, { once: true }));

    setTimeout(() => { if (video.readyState < 2) fail(); }, 4000);
  }

  // -------------------------------------------------------------------
  // Panel hình nền — CHỈ đổi nền, không có tuỳ chọn hiệu ứng
  // -------------------------------------------------------------------

  function thumbCss(preset) {
    return preset.thumb ? `url("${preset.thumb}")` : preset.thumbCss;
  }

  function renderThemePanel(host) {
    const p = appState.prefs;
    host.innerHTML = `
      <div class="theme-grid">
        ${PRESETS.map((preset) => `
          <button type="button"
                  class="theme-card${p.theme_id === preset.id ? ' theme-card--active' : ''}"
                  data-theme="${preset.id}" data-bg="${util.esc(thumbCss(preset))}">
            <span class="theme-card__label">${util.esc(preset.name)}</span>
          </button>`).join('')}
        ${p.custom_bg_url ? `
          <button type="button"
                  class="theme-card${p.theme_id === 'custom' ? ' theme-card--active' : ''}"
                  data-theme="custom" data-bg="url(&quot;${util.esc(p.custom_bg_url)}&quot;)">
            <span class="theme-card__label">Ảnh của bạn</span>
          </button>` : ''}
      </div>

      <div class="side-panel__section">
        <label class="upload-drop" id="uploadDrop">
          ${util.icon('upload')} Kéo thả hoặc bấm để tải ảnh nền
          <input type="file" id="uploadInput" accept="image/jpeg,image/png,image/webp">
        </label>
        <img class="upload-preview" id="uploadPreview" alt="" hidden>
        <p class="field__hint">JPG, PNG hoặc WEBP · tối đa 5MB</p>
      </div>`;

    host.querySelectorAll('[data-theme]').forEach((btn) => {
      btn.style.backgroundImage = btn.dataset.bg;
      btn.addEventListener('click', () => {
        App.prefs.set({ theme_id: btn.dataset.theme });
        renderThemePanel(host);
      });
    });

    bindUpload(host);
  }

  function bindUpload(host) {
    const drop = host.querySelector('#uploadDrop');
    const input = host.querySelector('#uploadInput');
    const preview = host.querySelector('#uploadPreview');

    const handle = (file) => {
      if (!file) return;
      if (!ACCEPTED_TYPES.includes(file.type)) {
        ui.toast({ title: 'Sai định dạng', body: 'Chỉ nhận JPG, PNG hoặc WEBP.' });
        return;
      }
      if (file.size > MAX_UPLOAD_BYTES) {
        ui.toast({
          title: 'Ảnh quá lớn',
          body: `Tối đa 5MB, ảnh của bạn ${(file.size / 1048576).toFixed(1)}MB.`,
        });
        return;
      }
      preview.src = URL.createObjectURL(file);
      preview.hidden = false;
      upload(file, host);
    };

    input.addEventListener('change', () => handle(input.files[0]));

    ['dragenter', 'dragover'].forEach((evt) =>
      drop.addEventListener(evt, (e) => {
        e.preventDefault();
        drop.classList.add('upload-drop--over');
      }));

    ['dragleave', 'drop'].forEach((evt) =>
      drop.addEventListener(evt, (e) => {
        e.preventDefault();
        drop.classList.remove('upload-drop--over');
      }));

    drop.addEventListener('drop', (e) => handle(e.dataTransfer.files[0]));
  }

  async function upload(file, host) {
    const form = new FormData();
    form.append('image', file);
    try {
      const resp = await App.api.fetch('/api/preferences/background/', {
        method: 'POST',
        body: form,
      });
      const data = await resp.json();
      if (!resp.ok) {
        ui.toast({ title: 'Không tải được ảnh', body: data.error || 'Thử ảnh khác.' });
        return;
      }
      App.prefs.set({ custom_bg_url: data.custom_bg_url, theme_id: 'custom' }, { sync: false });
      ui.toast({ title: 'Đã đổi ảnh nền', tone: 'ok' });
      renderThemePanel(host);
    } catch {
      ui.toast({ title: 'Không tải được ảnh', body: 'Kiểm tra kết nối rồi thử lại.' });
    }
  }

  // -------------------------------------------------------------------
  // Khởi tạo
  // -------------------------------------------------------------------

  function init() {
    initVideo();
    resize();
    applyPrefs();

    const onViewportChange = util.debounce(() => { resize(); start(); }, 150);

    window.addEventListener('resize', onViewportChange);
    // Cửa sổ ẩn lúc tải trang trả về innerWidth = 0; ResizeObserver bắt được
    // thời điểm layout có kích thước thật để dựng lại canvas.
    if ('ResizeObserver' in window) {
      new ResizeObserver(onViewportChange).observe(document.documentElement);
    }

    // Tab ẩn -> dừng hẳn loop, CPU về 0.
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) stop(); else start();
    });

    window.matchMedia('(prefers-reduced-motion: reduce)')
      .addEventListener('change', applyPrefs);

    dom.btnTheme.addEventListener('click', () => App.rooms.togglePanel('theme'));
  }

  return { init, applyPrefs, renderThemePanel, celebrate, presets: PRESETS, presetById };
}());

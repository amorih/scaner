/* Чистая математика для 2D- и 3D-визуализации магнитного поля.

   Файл не зависит от DOM, Canvas или Qt. Одинаковые формулы работают
   в браузере / QtWebEngine и проверяются Node.js-тестами без оборудования.
*/
(function exposeHeatmapMath(root) {
  'use strict';

  /**
   * Возвращает IDW-оценку компоненты поля в заданной координате.
   * Вес каждой реальной точки равен 1 / r²: ближайшее измерение влияет
   * сильнее, а при r = 0 возвращается исходное значение без округления.
   */
  function interpolate(points, key, x, y) {
    let weighted = 0;
    let weights = 0;
    for (const point of points) {
      const dx = x - Number(point.x);
      const dy = y - Number(point.y);
      const distanceSquared = dx * dx + dy * dy;
      const value = Number(point[key]);
      if (!Number.isFinite(value)) continue;
      if (distanceSquared < 1e-7) return value;
      const weight = 1 / distanceSquared;
      weighted += value * weight;
      weights += weight;
    }
    return weights ? weighted / weights : NaN;
  }

  /** Проверяет, принадлежит ли точка реальной области измерения цилиндра. */
  function insideCylinder(x, y, radius) {
    return Math.hypot(x, y) <= radius + 1e-9;
  }

  /**
   * Создаёт преобразование миллиметров в пиксели с одинаковым масштабом осей.
   * За счёт этого геометрия остаётся пропорциональной на любом Canvas.
   */
  function equalScaleGeometry(minX, maxX, minY, maxY, width, height) {
    const rangeX = Math.max(maxX - minX, 1e-6);
    const rangeY = Math.max(maxY - minY, 1e-6);
    const pixelsPerMillimetre = Math.min(width / rangeX, height / rangeY);
    const drawingWidth = rangeX * pixelsPerMillimetre;
    const drawingHeight = rangeY * pixelsPerMillimetre;
    return {
      minX, maxX, minY, maxY, pixelsPerMillimetre,
      drawingWidth, drawingHeight,
      offsetX: (width - drawingWidth) / 2,
      offsetY: (height - drawingHeight) / 2,
    };
  }

  /** Палитры цветов для научного отображения */
  const PALETTES = {
    hot: [
      [14, 8, 18],
      [84, 14, 45],
      [175, 28, 48],
      [238, 77, 34],
      [255, 185, 55],
      [255, 250, 220],
    ],
    viridis: [
      [68, 1, 84],
      [59, 82, 139],
      [33, 145, 140],
      [94, 201, 98],
      [253, 231, 37],
    ],
    plasma: [
      [13, 8, 135],
      [126, 3, 168],
      [204, 71, 120],
      [248, 149, 64],
      [240, 249, 33],
    ],
    turbo: [
      [48, 18, 59],
      [70, 134, 251],
      [27, 229, 181],
      [164, 252, 60],
      [251, 185, 56],
      [227, 68, 10],
      [122, 4, 3],
    ],
  };

  /**
   * Возвращает цвет [r, g, b] по нормализованному значению 0..1 для выбранной палитры.
   */
  function sampleColor(val, paletteName) {
    const stops = PALETTES[paletteName] || PALETTES.hot;
    const clamped = Math.max(0, Math.min(1, Number(val) || 0));
    const p = clamped * (stops.length - 1);
    const index = Math.min(stops.length - 2, Math.floor(p));
    const t = p - index;
    const a = stops[index];
    const b = stops[index + 1];
    return [
      Math.round(a[0] + (b[0] - a[0]) * t),
      Math.round(a[1] + (b[1] - a[1]) * t),
      Math.round(a[2] + (b[2] - a[2]) * t),
    ];
  }

  function sampleColorRgb(val, paletteName) {
    const [r, g, b] = sampleColor(val, paletteName);
    return `rgb(${r},${g},${b})`;
  }

  /**
   * Проекция 3D точки (x, y, z) в экранные координаты (px, py, depth).
   * Поддерживает поворот по азимуту (yaw) и углу места (pitch/elevation),
   * а также масштаб (zoom) и центрирование.
   *
   * @param {number} x - координата X (мм)
   * @param {number} y - координата Y (мм)
   * @param {number} z - высота поля Z (масштабированная)
   * @param {object} opts - { yaw, pitch, zoom, cx, cy }
   */
  function project3D(x, y, z, opts) {
    const yaw = (opts.yaw || 0) * Math.PI / 180;
    const pitch = (opts.pitch || 0) * Math.PI / 180;
    const zoom = opts.zoom || 1;
    const cx = opts.cx || 0;
    const cy = opts.cy || 0;

    // Вращение вокруг вертикальной оси Z (азимут):
    const cosY = Math.cos(yaw);
    const sinY = Math.sin(yaw);
    const x1 = x * cosY - y * sinY;
    const y1 = x * sinY + y * cosY;
    const z1 = z;

    // Вращение вокруг горизонтальной оси X (угол места / наклона):
    const cosP = Math.cos(pitch);
    const sinP = Math.sin(pitch);
    const x2 = x1;
    const y2 = y1 * cosP - z1 * sinP;
    const z2 = y1 * sinP + z1 * cosP;

    // Изометрическая научная проекция (без оптических искажений градиента):
    const px = cx + x2 * zoom;
    const py = cy - z2 * zoom;
    // depth для сортировки от дальних к ближним (художественный алгоритм):
    const depth = y2;

    return { px, py, depth, x2, y2, z2 };
  }

  // Экспорт для браузера и Node.js
  const api = {
    interpolate,
    insideCylinder,
    equalScaleGeometry,
    PALETTES,
    sampleColor,
    sampleColorRgb,
    project3D,
  };

  root.HeatmapMath = api;
  if (typeof module !== 'undefined') module.exports = api;
}(typeof window !== 'undefined' ? window : globalThis));

// Автоматические проверки формул, которыми Canvas строит 2D- и 3D-карту.
const assert = require('assert');
const {
  interpolate,
  insideCylinder,
  equalScaleGeometry,
  project3D,
  sampleColor,
  sampleColorRgb,
  PALETTES,
} = require('../ui/heatmap_math.js');

const points = [
  { x: -10, y: -10, b: 10 },
  { x: 10, y: -10, b: 30 },
  { x: -10, y: 10, b: 50 },
  { x: 10, y: 10, b: 70 },
];

// В реальной точке никакая интерполяция не имеет права менять значение.
assert.strictEqual(interpolate(points, 'b', 10, 10), 70);
// В центре равномерного квадрата все расстояния одинаковы: это среднее 40 мТл.
assert.strictEqual(interpolate(points, 'b', 0, 0), 40);
// Граница цилиндра принадлежит измеряемой области, точка за ней — нет.
assert.strictEqual(insideCylinder(10, 0, 10), true);
assert.strictEqual(insideCylinder(10.01, 0, 10), false);
// Широкий Canvas не должен менять масштаб окружности по осям.
const geometry = equalScaleGeometry(-10, 10, -10, 10, 800, 300);
assert.strictEqual(geometry.pixelsPerMillimetre, 15);
assert.strictEqual(geometry.offsetX, 250);
assert.strictEqual(geometry.offsetY, 0);

// Проверка 3D изометрической проекции:
const p0 = project3D(0, 0, 0, { yaw: 0, pitch: 0, zoom: 2, cx: 100, cy: 100 });
assert.strictEqual(p0.px, 100);
assert.strictEqual(p0.py, 100);

const pYaw = project3D(10, 0, 0, { yaw: 90, pitch: 0, zoom: 1, cx: 0, cy: 0 });
assert(Math.abs(pYaw.px - 0) < 1e-6);
assert(Math.abs(pYaw.depth - 10) < 1e-6);

// Проверка палитр
assert(Array.isArray(PALETTES.hot));
assert(Array.isArray(PALETTES.viridis));
const rgb = sampleColorRgb(0.5, 'viridis');
assert(rgb.startsWith('rgb('));

console.log('Heatmap math & 3D projection: OK');

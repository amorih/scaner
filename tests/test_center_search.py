"""Проверки тестовой функции автопоиска центра магнита (чистая логика).

Никакого USB и I²C: физические движения заменены фиктивным принтером, а поле
магнита — синтетическим гауссовым пиком над заданным центром.
"""

import math
import unittest

# Модуль scanner импортируется как объект: внутри него патчится time.sleep,
# чтобы тест автопоиска не ждал реальных пауз между точками.
import scanner as scanner_module
from config import BED_CENTER_X, BED_CENTER_Y, SENSOR_OFFSET_X, SENSOR_OFFSET_Y
from scanner import (
    centroid,
    check_limits,
    create_scan,
    detect_axis_boundaries,
    fast_find_magnet_center,
    find_magnet_boundaries_from_grid,
    find_magnet_center,
    fit_magnet_pattern,
    validate_parameters,
)


class CentroidTests(unittest.TestCase):
    """«Центр масс» поля должен сходиться к пику и быть устойчивым к фону."""

    def _grid(self, nodes):
        """Симметричная сетка точек вокруг нуля в порядке строк y→x."""
        return [(x, y) for y in nodes for x in nodes]

    def test_symmetric_peak_returns_exact_center(self):
        """Симметричный пик в нуле даёт центр (0, 0), а не узел сетки."""
        points = self._grid([-6, -4, -2, 0, 2, 4, 6])
        values = [math.exp(-(x * x + y * y) / 32.0) for x, y in points]
        center_x, center_y = centroid(points, values)
        self.assertAlmostEqual(center_x, 0.0, delta=1e-9)
        self.assertAlmostEqual(center_y, 0.0, delta=1e-9)

    def test_flat_field_falls_back_to_maximum_node(self):
        """Плоский фон без превышения не ломает оценку: берётся узел максимума."""
        points = self._grid([-6, -4, -2, 0, 2, 4, 6])
        values = [1.0] * len(points)
        self.assertEqual(centroid(points, values), points[0])

    def test_peak_between_nodes_pulls_centroid(self):
        """Пик между узлами сетки смещает оценку к своему истинному положению."""
        points = [(x, 0.0) for x in (0.0, 1.0, 2.0, 3.0, 4.0)]
        values = [math.exp(-((x - 1.3) ** 2) / 8.0) for x, _ in points]
        center_x, _ = centroid(points, values)
        # Оценка ближе к истинному центру 1.3, чем к любому целому узлу.
        self.assertLess(abs(center_x - 1.3), 0.5)
        self.assertGreater(center_x, 1.0)

    def test_centroid_rejects_mismatched_lists(self):
        """Разные длины точек и значений — ошибка, а не молчаливое смещение."""
        with self.assertRaises(ValueError):
            centroid([(0.0, 0.0), (1.0, 1.0)], [1.0])


class CenterParameterTests(unittest.TestCase):
    """Смещение центра из автопоиска безопасно по умолчанию и проверяется."""

    def _base(self, **overrides):
        parameters = {
            "shape": "rectangle", "mode": "snake", "size_x": 20,
            "size_y": 20, "height": 5, "step": 5,
        }
        parameters.update(overrides)
        return parameters

    def test_defaults_to_center_of_table(self):
        """Обычный скан без автопоиска остаётся центрированным на столе."""
        normalized = validate_parameters(self._base())
        self.assertEqual(normalized["center_x"], 0.0)
        self.assertEqual(normalized["center_y"], 0.0)

    def test_accepts_numeric_center_from_json(self):
        """Числа-строки из JS становятся float, как и остальные параметры."""
        normalized = validate_parameters(self._base(center_x="12.5", center_y="-4"))
        self.assertEqual(normalized["center_x"], 12.5)
        self.assertEqual(normalized["center_y"], -4.0)

    def test_rejects_non_numeric_center(self):
        """Мусор в center_x не должен дойти до команд движения принтера."""
        with self.assertRaisesRegex(ValueError, "Смещение центра"):
            validate_parameters(self._base(center_x="не число"))


class LimitsWithCenterTests(unittest.TestCase):
    """check_limits учитывает смещение центра до первого G-code."""

    def test_offset_keeps_trajectory_on_table(self):
        """Небольшое смещение внутри стола принимается без ошибок."""
        rows = create_scan("rectangle", 20, 20, 5)
        check_limits(rows, offset_x=60.0, offset_y=-60.0)

    def test_large_offset_raises_before_moving(self):
        """Смещение, уводящее траекторию за стол, отклоняется заранее."""
        rows = create_scan("rectangle", 20, 20, 5)
        with self.assertRaisesRegex(ValueError, "пределы стола"):
            check_limits(rows, offset_x=140.0, offset_y=0.0)


class FakePrinter:
    """Записывает перемещения вместо реального G-code на USB-порту."""

    def __init__(self):
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0

    def move(self, x=None, y=None, z=None, speed=5000):
        if z is not None:
            self.z = z
        if x is not None:
            self.x = x
        if y is not None:
            self.y = y


class FakeSensor:
    """Возвращает синтетический гауссов пик поля над заданным центром."""

    def __init__(self, printer, center_x, center_y):
        self.printer = printer
        self.center_x = center_x
        self.center_y = center_y

    def measure(self):
        # Координата датчика в системе измерений считается из положения сопла.
        measure_x = self.printer.x + SENSOR_OFFSET_X - BED_CENTER_X
        measure_y = self.printer.y + SENSOR_OFFSET_Y - BED_CENTER_Y
        radius_sq = (measure_x - self.center_x) ** 2 + (measure_y - self.center_y) ** 2
        magnitude = 50.0 + 4000.0 * math.exp(-radius_sq / 32.0)
        return {"bx": 0.0, "by": 0.0, "bz": magnitude, "b": magnitude}


class FindMagnetCenterTests(unittest.TestCase):
    """Автопоиск проверяет всю coarse-сетку для устойчивости к шуму и полюсам."""

    def _run_search(self, center_x, center_y):
        # Реальные паузы между точками заменяются мгновенными: тест проверяет
        # логику поиска, а не скорость физического перемещения.
        original_sleep = scanner_module.time.sleep
        scanner_module.time.sleep = lambda _seconds: None
        self.addCleanup(lambda: setattr(scanner_module.time, "sleep", original_sleep))
        printer = FakePrinter()
        sensor = FakeSensor(printer, center_x, center_y)
        result = find_magnet_center(printer, sensor, height=5.0)
        # Финальный подъём: рабочая высота + 20 мм.
        self.assertEqual(printer.z, 5.0 + 5.0 + 20.0)
        return result

    def test_center_at_table_center_stops_after_inner_ring(self):
        """Магнит строго в центре: 9 грубых точек (центр + 8 соседей)."""
        result = self._run_search(0.0, 0.0)
        self.assertAlmostEqual(result["center_x"], 0.0, delta=0.3)
        self.assertAlmostEqual(result["center_y"], 0.0, delta=0.3)
        self.assertEqual(result["coarse_total"], 49)
        self.assertEqual(result["fine_total"], 49)    # уточнение 7×7
        self.assertEqual(result["test_total"], 12)

    def test_magnet_on_first_ring_stops_after_second_ring(self):
        """Магнит на первом кольце (10, -10): нужен и второй круг, 25 точек."""
        result = self._run_search(10.0, -10.0)
        self.assertAlmostEqual(result["center_x"], 10.0, delta=0.3)
        self.assertAlmostEqual(result["center_y"], -10.0, delta=0.3)
        self.assertEqual(result["coarse_total"], 49)

    def test_offset_center_between_coarse_nodes(self):
        """Смещённый между узлами магнит определяется точнее шага сетки."""
        result = self._run_search(10.0, -12.4)
        self.assertAlmostEqual(result["center_x"], 10.0, delta=0.8)
        self.assertAlmostEqual(result["center_y"], -12.4, delta=0.8)
        self.assertGreater(result["peak_b"], 3500.0)  # пик найден
        self.assertEqual(result["coarse_total"], 49)
        self.assertEqual(result["fine_total"], 49)
        self.assertEqual(result["test_total"], 12)

    def test_far_magnet_scans_all_rings(self):
        """Магнит на втором кольце (20, 0): останавливаться рано нельзя."""
        result = self._run_search(20.0, 0.0)
        self.assertAlmostEqual(result["center_x"], 20.0, delta=0.3)
        self.assertAlmostEqual(result["center_y"], 0.0, delta=0.3)
        self.assertEqual(result["coarse_total"], 49)  # все кольца пройдены


class BoundaryDetectionTests(unittest.TestCase):
    """Проверка одномерного поиска границ и центра магнита."""

    def test_symmetric_peak_boundary(self):
        # Пик в центре x=10, ширина ~12 (радиус ~6)
        x_vals = [index * 1.0 for index in range(-10, 31)]
        mag = [10.0 + 1000.0 * math.exp(-((x - 10.0) ** 2) / 36.0) for x in x_vals]
        res = detect_axis_boundaries(x_vals, mag)
        self.assertAlmostEqual(res["center"], 10.0, delta=0.3)
        self.assertAlmostEqual(res["peak_pos"], 10.0, delta=0.1)
        self.assertGreater(res["span"], 8.0)
        self.assertLess(res["span"], 16.0)

    def test_flat_signal_safe_fallback(self):
        x_vals = [0.0, 5.0, 10.0, 15.0]
        mag = [5.0, 5.0, 5.0, 5.0]
        res = detect_axis_boundaries(x_vals, mag)
        self.assertEqual(res["span"], 0.0)
        self.assertEqual(res["center"], 0.0)


class PatternFittingTests(unittest.TestCase):
    """Проверка классификации круглых и прямоугольных оснований."""

    def test_circle_pattern_fitting(self):
        x_bounds = {"span": 20.0, "center": 5.0, "bound_min": -5.0, "bound_max": 15.0}
        y_bounds = {"span": 20.4, "center": -2.0, "bound_min": -12.2, "bound_max": 8.2}
        fitted = fit_magnet_pattern(x_bounds, y_bounds, pattern="auto")
        self.assertEqual(fitted["pattern"], "circle")
        self.assertAlmostEqual(fitted["diameter"], 20.2, delta=0.1)
        self.assertEqual(fitted["center_x"], 5.0)
        self.assertEqual(fitted["center_y"], -2.0)
        self.assertGreaterEqual(fitted["confidence"], 90.0)

    def test_rectangle_pattern_fitting(self):
        x_bounds = {"span": 30.0, "center": 0.0, "bound_min": -15.0, "bound_max": 15.0}
        y_bounds = {"span": 15.0, "center": 0.0, "bound_min": -7.5, "bound_max": 7.5}
        fitted = fit_magnet_pattern(x_bounds, y_bounds, pattern="auto")
        self.assertEqual(fitted["pattern"], "rectangle")
        self.assertEqual(fitted["width"], 30.0)
        self.assertEqual(fitted["length"], 15.0)
        self.assertIsNone(fitted["radius"])


class FastFindMagnetCenterTests(unittest.TestCase):
    """Высокоскоростной автопоиск по осям (5–10 секунд)."""

    def _run_fast_search(self, center_x, center_y, pattern="auto"):
        original_sleep = scanner_module.time.sleep
        scanner_module.time.sleep = lambda _seconds: None
        self.addCleanup(lambda: setattr(scanner_module.time, "sleep", original_sleep))
        printer = FakePrinter()
        sensor = FakeSensor(printer, center_x, center_y)
        return fast_find_magnet_center(printer, sensor, height=5.0, pattern=pattern)

    def test_fast_search_finds_center_at_origin(self):
        result = self._run_fast_search(0.0, 0.0, pattern="circle")
        self.assertAlmostEqual(result["center_x"], 0.0, delta=0.4)
        self.assertAlmostEqual(result["center_y"], 0.0, delta=0.4)
        self.assertEqual(result["pattern"], "circle")
        self.assertGreater(result["peak_b"], 3800.0)
        self.assertIn("bounds", result)
        # Проверяем, что число точек поиска небольшое и выполняется быстро
        self.assertLess(result["total_points"], 90)

    def test_fast_search_offset_magnet(self):
        result = self._run_fast_search(12.5, -8.0, pattern="auto")
        self.assertAlmostEqual(result["center_x"], 12.5, delta=0.6)
        self.assertAlmostEqual(result["center_y"], -8.0, delta=0.6)
        self.assertGreater(result["peak_b"], 3500.0)

    def test_fast_search_far_off_axis_magnet(self):
        """Магнит сильно смещён по обеим осям (X=20, Y=25), где Y=0 не имеет сигнала."""
        result = self._run_fast_search(20.0, 25.0, pattern="auto")
        self.assertAlmostEqual(result["center_x"], 20.0, delta=0.8)
        self.assertAlmostEqual(result["center_y"], 25.0, delta=0.8)
        self.assertLess(result["total_points"], 50)

    def test_fast_search_touchless_height_detection(self):
        """Бесконтактное определение высоты магнита 10 мм и зазор строго 5 мм."""
        original_sleep = scanner_module.time.sleep
        scanner_module.time.sleep = lambda _seconds: None
        self.addCleanup(lambda: setattr(scanner_module.time, "sleep", original_sleep))
        printer = FakePrinter()

        class Fake3DHeightSensor:
            def __init__(self, p, cx, cy, top=10.0):
                self.printer = p
                self.cx = cx
                self.cy = cy
                self.top = top

            def measure(self):
                mx = self.printer.x + SENSOR_OFFSET_X - BED_CENTER_X
                my = self.printer.y + SENSOR_OFFSET_Y - BED_CENTER_Y
                r_sq = (mx - self.cx)**2 + (my - self.cy)**2
                dist_z = max(0.2, self.printer.z - self.top)
                b_z = 25000.0 / (dist_z ** 3)
                val = 10.0 + b_z / (1.0 + (r_sq / 36.0)**1.5)
                return {"bx": 0.0, "by": 0.0, "bz": val, "b": val}

        sensor = Fake3DHeightSensor(printer, 10.0, -10.0, top=10.0)
        result = fast_find_magnet_center(printer, sensor, height=0.0, pattern="auto")

        self.assertAlmostEqual(result["center_x"], 10.0, delta=0.6)
        self.assertAlmostEqual(result["center_y"], -10.0, delta=0.6)
        # Высота верха магнита определена около 10 мм
        self.assertAlmostEqual(result["detected_surface_z"], 10.0, delta=0.5)
        # Рабочая высота ровно 5 мм над магнитом (10 + 5 = 15 мм)
        self.assertAlmostEqual(result["work_z"], 15.0, delta=0.5)
        # Финальный подъём на 20 мм выше рабочей высоты
        self.assertAlmostEqual(printer.z, result["work_z"] + 20.0, delta=0.5)

    def test_find_from_grid_instant_analysis(self):
        # Синтетическая сетка
        grid_nodes = [-15, -10, -5, 0, 5, 10, 15]
        points = [(x, y) for y in grid_nodes for x in grid_nodes]
        values = [50.0 + 3000.0 * math.exp(-((x - 4.0)**2 + (y + 5.0)**2)/25.0) for x, y in points]
        fitted = find_magnet_boundaries_from_grid(points, values, pattern="auto")
        self.assertAlmostEqual(fitted["center_x"], 4.0, delta=0.8)
        self.assertAlmostEqual(fitted["center_y"], -5.0, delta=0.8)
        self.assertEqual(fitted["pattern"], "circle")


if __name__ == "__main__":
    unittest.main()


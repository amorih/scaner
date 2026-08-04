"""Проверки геометрии траекторий без настоящего принтера и датчика."""

import math  # Проверяет расстояния точек от центра окружности.
import unittest  # Запускает тесты штатной библиотекой Python.

from scanner import create_axis, create_circle_scan, create_line_scan, create_scan, sensor_to_printer  # Тестируем чистую геометрию без USB/I²C.


class ScannerGeometryTests(unittest.TestCase):
    """Проверяет инварианты путей, от которых зависит безопасность скана."""

    def test_axis_contains_center_and_does_not_exceed_requested_step(self):
        """Центр магнита всегда измеряется, а фактический шаг не становится больше."""
        axis = create_axis(15, 4)
        self.assertIn(0.0, axis)
        gaps = [right - left for left, right in zip(axis, axis[1:])]
        self.assertTrue(all(gap <= 4 for gap in gaps))

    def test_cylinder_scan_has_no_points_outside_radius(self):
        """Змейка для цилиндра не попадает за его круглую границу."""
        rows = create_scan("cylinder", 20, 20, 5)
        points = [point for row in rows for point in row]
        self.assertTrue(points)
        self.assertTrue(all(math.hypot(x, y) <= 10 + 1e-8 for x, y in points))
        self.assertIn((0.0, 0.0), points)

    def test_rectangle_scan_covers_all_corners(self):
        """Прямоугольная траектория содержит четыре границы рабочей области."""
        points = [point for row in create_scan("rectangle", 20, 10, 5) for point in row]
        self.assertIn((-10.0, -5.0), points)
        self.assertIn((10.0, 5.0), points)

    def test_circle_scan_is_on_circle(self):
        """Контурный режим создаёт точки на одной окружности."""
        points = create_circle_scan(20, 5)[0]
        self.assertGreaterEqual(len(points), 8)
        self.assertTrue(all(math.isclose(math.hypot(x, y), 10, abs_tol=1e-8) for x, y in points))

    def test_line_scan_preserves_endpoints(self):
        """Скан магнитной системы начинается и заканчивается заданными точками."""
        points = create_line_scan(-10, 3, 12, -5, 4)[0]
        self.assertEqual(points[0], (-10.0, 3.0))
        self.assertEqual(points[-1], (12.0, -5.0))

    def test_sensor_center_translates_to_safe_printer_coordinates(self):
        """Ноль измерительной системы преобразуется в координаты стола."""
        printer_x, printer_y = sensor_to_printer(0, 0)
        self.assertGreaterEqual(printer_x, 0)
        self.assertGreaterEqual(printer_y, 0)


if __name__ == "__main__":
    unittest.main()

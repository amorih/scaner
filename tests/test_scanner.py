"""Проверки геометрии траекторий без настоящего принтера и датчика."""

import math  # Проверяет расстояния точек от центра окружности.
import unittest  # Запускает тесты штатной библиотекой Python.

from scanner import create_axis, create_circle_scan, create_line_scan, create_scan, sensor_to_printer, validate_parameters  # Тестируем чистую геометрию без USB/I²C.


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

    def test_validation_rejects_dangerous_or_incomplete_scan_parameters(self):
        """Backend не принимает нулевой шаг и вырожденную линию из DevTools."""
        with self.assertRaisesRegex(ValueError, "Шаг"):
            validate_parameters({
                "shape": "rectangle", "mode": "snake", "size_x": 20,
                "size_y": 20, "height": 5, "step": 0,
            })
        with self.assertRaisesRegex(ValueError, "Начало и конец"):
            validate_parameters({
                "shape": "system", "mode": "line", "size_x": 0,
                "size_y": 0, "height": 5, "step": 1,
                "start_x": 0, "start_y": 0, "end_x": 0, "end_y": 0,
            })

    def test_validation_normalizes_numeric_json_values(self):
        """Числа из JSON-формы не остаются строками в геометрии сканера."""
        parameters = validate_parameters({
            "shape": "rectangle", "mode": "snake", "size_x": "20",
            "size_y": "10", "height": "5", "step": "2.5",
        })
        self.assertEqual(parameters["size_x"], 20.0)
        self.assertEqual(parameters["step"], 2.5)

    def test_validation_rejects_work_height_above_machine_limit(self):
        with self.assertRaisesRegex(ValueError, "Рабочая высота"):
            validate_parameters({
                "shape": "rectangle", "mode": "snake", "size_x": 20,
                "size_y": 20, "height": 246, "step": 2,
            })


if __name__ == "__main__":
    unittest.main()

# Magnetic Scanner Studio — AI Orchestration & Development Guidelines

This project is a high-precision 3D magnetic field scanner combining an Ender 3 CNC/3D printer motion system with an MLX90393 I2C 3-axis magnetometer, driven by a PySide6 Qt WebEngine desktop application.

## Subagent Team & Roles

1. **Lead Orchestrator (Antigravity Primary Agent)**:
   - Coordinates architectural planning, requirement breakdown, delegation, and integration.
   - Ensures code quality, regression prevention, and safe interaction between modules.

2. **`ui-designer`**:
   - Focus: Desktop UI/UX (`ui/studio.html`, `ui/native.qss`), design system tokens, layout ergonomics.
   - Visualization: HTML5 Canvas 2D / WebGL heatmap, magnetic field vector rendering, scan progress animation.
   - Skill: `.agents/skills/ui-designer/SKILL.md`.

3. **`qa-tester`**:
   - Focus: Test suite in `tests/`, test-driven development, regression tests, edge cases.
   - Hardware Mocking: Safe mock environments for serial (`printer.py`), I2C (`sensor.py`), and worker threads (`desktop_app.py`).
   - Skill: `.agents/skills/qa-testing/SKILL.md`.

4. **`embedded-engineer`**:
   - Focus: Marlin G-code protocols (`printer.py`), Ender 3 physical kinematics, PlatformIO firmware (`firmware/`, `marlin-2.1.2.8/`), and MLX90393 I2C driver resilience (`sensor.py`).
   - Safety: Absolute coordinate boundaries (`BED_SIZE_X`, `BED_SIZE_Y`, `BED_MAX_Z`), sensor offsets, and emergency stop handling (`M112` / `M999`).
   - Skill: `.agents/skills/embedded-motion/SKILL.md`.

5. **`magnetic-analyst`**:
   - Focus: Mathematical trajectories (`scanner.py`), magnet auto-centering algorithms, weighted center-of-mass, boundary fitting, and data schemas (`storage.py`, `csv_export.py`).
   - Skill: `.agents/skills/magnetic-analytics/SKILL.md`.

## Mandatory Engineering Rules

- **Hardware Safety**: Never issue G-code or coordinate computations that exceed printer bed limits. Ensure all relative and sensor coordinates pass through `sensor_to_printer` with bounds checks.
- **Thread Safety**: Never perform blocking serial/I2C I/O on the Qt GUI main thread. All hardware interaction runs in `HardwareWorker` with `threading.Event()` for responsive aborts.
- **Testing Verification**: After any backend or geometry changes, execute:
  ```bash
  ./env/bin/python -m unittest discover -s tests -v
  ```
  And verify JavaScript math changes with:
  ```bash
  node tests/test_heatmap_math.js
  ```

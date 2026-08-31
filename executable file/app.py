from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--run-engine":
        if len(sys.argv) < 4:
            print(
                'usage: app.py --run-engine <engine_name> <engine args...>\n'
                '  예: app.py --run-engine blackbox -o "출력폴더" "영상.mp4"',
                file=sys.stderr,
            )
            return 2
        import engine_entry

        return engine_entry.run(sys.argv[2], sys.argv[3:])

    from PySide6.QtWidgets import QApplication

    from ui.main_window import MainWindow
    from ui.styles import APP_QSS

    app = QApplication(sys.argv)
    app.setStyleSheet(APP_QSS)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

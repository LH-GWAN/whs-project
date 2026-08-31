
APP_QSS = """
QWidget {
    background-color: #ffffff;
    color: #111111;
    font-family: -apple-system, "Segoe UI", "Malgun Gothic", sans-serif;
    font-size: 13px;
}

QLabel[role="title"] {
    font-size: 16px;
    font-weight: 600;
}

QLabel[role="heading"] {
    font-size: 20px;
    font-weight: 700;
}

QPushButton {
    border: 1px solid #111111;
    background-color: #ffffff;
    padding: 6px 14px;
    border-radius: 2px;
}
QPushButton:hover {
    background-color: #f0f0f0;
}

QPushButton[role="primary"] {
    background-color: #111111;
    color: #ffffff;
}
QPushButton[role="primary"]:hover {
    background-color: #333333;
}

QFrame[role="panel"] {
    border: 1px solid #cccccc;
}

QFrame[role="upload-box"] {
    border: 1px dashed #999999;
}

#HeaderBar {
    border-bottom: 1px solid #111111;
}

#FileInfoBar {
    border-bottom: 1px solid #dddddd;
}

QTabBar::tab {
    padding: 8px 16px;
    border: none;
    font-weight: 500;
}
QTabBar::tab:selected {
    font-weight: 700;
    border-bottom: 2px solid #111111;
}

QTableWidget {
    gridline-color: #dddddd;
    border: 1px solid #cccccc;
}
QHeaderView::section {
    background-color: #fafafa;
    border: none;
    border-bottom: 1px solid #cccccc;
    padding: 4px;
    font-weight: 600;
}
"""

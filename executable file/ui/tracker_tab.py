from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from core.acceleration import FlaggedSegment
from engine.engine_adapter import TrackPoint
from ui.map_view import MapView


def _fmt_ms(ms: int) -> str:
    total = max(0, int(ms // 1000))
    return f"{total // 60:02d}:{total % 60:02d}"


class TrackerTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._player = QMediaPlayer(self)
        self._video_widget = QVideoWidget(self)
        self._player.setVideoOutput(self._video_widget)

        self._play_btn = QPushButton("▶")
        self._play_btn.setFixedWidth(40)
        self._play_btn.clicked.connect(self._toggle_play)

        self._seek_slider = QSlider(Qt.Horizontal)
        self._seek_slider.sliderMoved.connect(self._on_slider_moved)
        self._time_label = QLabel("00:00 / 00:00")

        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.playbackStateChanged.connect(self._on_state_changed)

        controls = QHBoxLayout()
        controls.addWidget(self._play_btn)
        controls.addWidget(self._seek_slider, 1)
        controls.addWidget(self._time_label)

        video_panel = QVBoxLayout()
        video_panel.setContentsMargins(0, 0, 0, 0)
        video_panel.addWidget(self._video_widget, 1)
        video_panel.addLayout(controls)
        video_container = QWidget()
        video_container.setLayout(video_panel)

        self._map = MapView()

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(video_container)
        splitter.addWidget(self._map)
        splitter.setSizes([520, 520])

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(splitter)

    def load_video(self, path: str) -> None:
        self._player.setSource(QUrl.fromLocalFile(path))

    def load_track(self, points: List[TrackPoint],
                    segments: Optional[List[FlaggedSegment]] = None) -> None:
        self._map.set_track(points, segments)

    def _toggle_play(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    def _on_state_changed(self, state) -> None:
        self._play_btn.setText("⏸" if state == QMediaPlayer.PlayingState else "▶")

    def _on_slider_moved(self, position: int) -> None:
        self._player.setPosition(position)
        self._map.set_playback_time(position / 1000.0)

    def _on_duration_changed(self, duration: int) -> None:
        self._seek_slider.setRange(0, max(0, duration))
        self._time_label.setText(f"{_fmt_ms(self._player.position())} / {_fmt_ms(duration)}")

    def _on_position_changed(self, position: int) -> None:
        if not self._seek_slider.isSliderDown():
            self._seek_slider.setValue(position)
        self._time_label.setText(f"{_fmt_ms(position)} / {_fmt_ms(self._player.duration())}")
        self._map.set_playback_time(position / 1000.0)

    def ensure_map_loaded(self) -> None:
        self._map.ensure_loaded()

    def stop(self) -> None:
        self._player.stop()

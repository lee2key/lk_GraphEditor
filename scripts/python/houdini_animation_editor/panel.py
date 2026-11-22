"""A small, dependency-free animation curve editor for Houdini Python Panels."""

from __future__ import annotations

import math
from bisect import bisect_right

import hou
from PySide6 import QtCore, QtGui, QtWidgets


_EDITOR = None


def create_interface():
    """Entry point called by the .pypanel definition."""
    global _EDITOR
    _EDITOR = AnimationGraphEditor()
    return _EDITOR


def destroy_interface():
    global _EDITOR
    if _EDITOR is not None:
        _EDITOR.stop_listening()
        _EDITOR.deleteLater()
    _EDITOR = None


def on_node_changed(node):
    if _EDITOR is not None:
        _EDITOR.set_node(node)


class CurveCanvas(QtWidgets.QWidget):
    """Minimal curve renderer with click and box selection."""

    keyRequested = QtCore.Signal(float, float)
    selectionChanged = QtCore.Signal(object)
    keyDragStarted = QtCore.Signal(object)
    keyDragMoved = QtCore.Signal(float, float)
    keyDragFinished = QtCore.Signal()
    tangentChanged = QtCore.Signal(float, str, float, float)
    tangentTransformStarted = QtCore.Signal()
    tangentTransformFinished = QtCore.Signal(bool)

    _MARGIN = 34

    def __init__(self, parent=None):
        super().__init__(parent)
        self._selection_text = ""
        self._curve_color = QtGui.QColor("#ff9d36")
        self._keys = []
        self._selected_frames = set()
        self._selected_handles = set()
        self._sample = lambda frame: 0.0
        self._path_view = None
        self._cached_path = None
        self._graph_view = None
        self._graph_pixmap = None
        self._range = (1.0, 24.0, -1.0, 1.0)
        self._press_position = None
        self._drag_rect = None
        self._dragging = False
        self._shift_at_press = False
        self._scrubbing = False
        self._pressed_key = None
        self._key_dragging = False
        self._drag_axis = None
        self._key_drag_start_range = None
        self._active_handle = None
        self._handle_slope = None
        self._handle_start_slope = None
        self._locked_handle_slope = None
        self._tangent_transform = None
        self._consume_transform_release = False
        self._panning = False
        self._pan_start_position = None
        self._pan_start_range = None
        self._fit_x_on_next_curve = True
        self._current_frame = hou.frame()
        self.setMinimumHeight(220)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setCursor(QtCore.Qt.CursorShape.CrossCursor)

    def set_curve(self, keys, sample, color="#ff9d36"):
        self._curve_color = QtGui.QColor(color)
        self._path_view = None
        self._graph_view = None
        had_keys = bool(self._keys)
        self._keys = keys
        existing_frames = {key.frame() for key in keys}
        self._selected_frames.intersection_update(existing_frames)
        if len(self._selected_frames) != 1:
            self._selected_handles.clear()
        self._sample = sample
        fitted_range = self._curve_range(keys)
        if self._fit_x_on_next_curve or (keys and not had_keys):
            left, right = fitted_range[:2]
        else:
            left, right = self._range[:2]
        self._range = (left, right, fitted_range[2], fitted_range[3])
        self._fit_x_on_next_curve = False
        self.update()

    def reset_view(self):
        self._fit_x_on_next_curve = True

    def fit_all(self):
        self._range = self._curve_range(self._keys)
        self.update()

    def fit_selected(self):
        frames = sorted(self._selected_frames)
        if not frames:
            self.fit_all()
            return
        left, right, bottom, top = self._range
        if len(frames) == 1:
            half_span = (right - left) / 2.0
            self._range = (frames[0] - half_span, frames[0] + half_span, bottom, top)
        else:
            frame_span = frames[-1] - frames[0]
            padding = max(1.0, frame_span * 0.1)
            values = [key.value() for key in self.selected_keys()]
            low, high = min(values), max(values)
            value_span = high - low
            # Equal-valued keys still need a nonzero vertical viewing range.
            value_padding = value_span * 0.15 if value_span > 0 else max(0.5, abs(low) * 0.05)
            self._range = (frames[0] - padding, frames[-1] + padding,
                           low - value_padding, high + value_padding)
        self.update()

    def set_selected_frames(self, frames):
        selected = set(frames)
        if selected != self._selected_frames:
            self._selected_handles.clear()
            self._selected_frames = selected
            self._graph_view = None
            self.update()

    @property
    def selected_frames(self):
        return frozenset(self._selected_frames)

    def selected_keys(self):
        return [key for key in self._keys if key.frame() in self._selected_frames]

    @property
    def selected_handles(self):
        return frozenset(self._selected_handles)

    def _set_handle_selection(self, directions):
        self._selected_handles = set(directions)
        self._graph_view = None
        self.selectionChanged.emit(sorted(self._selected_frames))
        self.update()

    def select_all(self):
        self._set_selection(key.frame() for key in self._keys)

    def set_playhead(self, frame):
        if frame == self._current_frame:
            return
        previous = self._current_frame
        self._current_frame = frame
        left, right, bottom, _ = self._range
        rect = self._plot_rect()
        for marker in (previous, frame):
            if left <= marker <= right:
                x = math.floor(self._to_point(marker, bottom).x())
                self.update(QtCore.QRect(x - 28, rect.top() - 2, 57, rect.height() + 4))

    @staticmethod
    def _curve_range(keys):
        if keys:
            frames = [key.frame() for key in keys]
            values = [key.value() for key in keys]
            frame_pad = max(1.0, (max(frames) - min(frames)) * 0.1)
            value_pad = max(0.5, (max(values) - min(values)) * 0.15)
            return (min(frames) - frame_pad, max(frames) + frame_pad,
                    min(values) - value_pad, max(values) + value_pad)
        return (1.0, 24.0, -1.0, 1.0)

    def _plot_rect(self):
        return self.rect().adjusted(self._MARGIN, 12, -12, -self._MARGIN)

    def _to_point(self, frame, value):
        left, right, bottom, top = self._range
        rect = self._plot_rect()
        x = rect.left() + (frame - left) / (right - left) * rect.width()
        y = rect.bottom() - (value - bottom) / (top - bottom) * rect.height()
        return QtCore.QPointF(x, y)

    def _from_point(self, point):
        left, right, bottom, top = self._range
        rect = self._plot_rect()
        x_ratio = (point.x() - rect.left()) / max(1, rect.width())
        y_ratio = (rect.bottom() - point.y()) / max(1, rect.height())
        return (left + x_ratio * (right - left), bottom + y_ratio * (top - bottom))

    def _curve_path(self):
        view = (self._range, self.size())
        if self._path_view != view:
            self._cached_path = self._build_curve_path()
            self._path_view = view
        return self._cached_path

    def _build_curve_path(self):
        if not self._keys:
            return None
        left, right, bottom, top = self._range
        path = QtGui.QPainterPath()
        # Split at every visible key, then sample in screen pixels, not frames.
        # This hits sharp extrema exactly even at subframe zoom levels.
        key_frames = [key.frame() for key in self._keys]
        boundaries = [left, *(frame for frame in key_frames if left < frame < right), right]
        rect = self._plot_rect()
        x_origin, y_origin = rect.left(), rect.bottom()
        pixels_per_frame = max(1, rect.width()) / (right - left)
        pixels_per_value = rect.height() / (top - bottom)
        has_point = False
        for start, end in zip(boundaries, boundaries[1:]):
            index = bisect_right(key_frames, start) - 1
            active_key = self._keys[index] if index >= 0 else None
            constant = False
            if active_key is not None:
                try:
                    constant = (active_key.expressionLanguage() == hou.exprLanguage.Hscript
                                and active_key.expression().strip() == "constant()")
                except hou.Error:
                    pass
            sample_count = max(2, int(math.ceil((end - start) * pixels_per_frame)) + 1)
            frames = [start, end] if constant else [
                start + (end - start) * index / (sample_count - 1)
                for index in range(sample_count)
            ]
            start_y = None
            for frame in frames:
                try:
                    value = float(self._sample(frame))
                    if not math.isfinite(value):
                        has_point = False
                        continue
                except (hou.Error, ValueError, TypeError):
                    has_point = False
                    continue
                x = x_origin + (frame - left) * pixels_per_frame
                y = y_origin - (value - bottom) * pixels_per_value
                if frame == start:
                    start_y = y
                if has_point:
                    if constant and frame == end and start_y is not None:
                        # Draw an exact horizontal hold followed by a vertical step.
                        path.lineTo(x, start_y)
                    path.lineTo(x, y)
                else:
                    path.moveTo(x, y)
                    has_point = True
        return path if has_point else None

    def _is_scrub_gesture(self, position):
        left, right = self._range[:2]
        if not left <= self._current_frame <= right:
            return False
        rect = self._plot_rect()
        x = self._to_point(self._current_frame, self._range[2]).x()
        return QtCore.QRectF(x - 26, rect.top() + 2, 52, 16).contains(position)

    def _set_frame_from_position(self, position):
        frame, _ = self._from_point(position)
        target = round(frame)
        if target != self._current_frame:
            hou.setFrame(target)
            self.set_playhead(hou.frame())

    def _time_grid_frames(self):
        left, right, _, _ = self._range
        target_step = max(1.0, (right - left) * 100 / max(1, self._plot_rect().width()))
        magnitude = 10 ** math.floor(math.log10(target_step))
        step = next(factor * magnitude for factor in (1, 2, 5, 10)
                    if factor * magnitude >= target_step)
        first = math.ceil(left / step)
        last = math.floor(right / step)
        return [index * step for index in range(first, last + 1)]

    def _handle_data(self):
        selected = self.selected_keys()
        if len(selected) != 1:
            return []
        key = selected[0]
        index = next((i for i, item in enumerate(self._keys) if item.frame() == key.frame()), None)
        if index is None:
            return []
        handles = []
        for direction, neighbor_index in (("in", index - 1), ("out", index + 1)):
            if neighbor_index < 0 or neighbor_index >= len(self._keys):
                continue
            neighbor = self._keys[neighbor_index]
            direction_sign = -1.0 if direction == "in" else 1.0
            segment_frames = abs(neighbor.frame() - key.frame())
            try:
                slope = key.inSlope() if direction == "in" and not key.isSlopeTied() else key.slope()
            except hou.Error:
                slope = hou.fps() * (neighbor.value() - key.value()) / (neighbor.frame() - key.frame())
            try:
                acceleration = key.inAccel() if direction == "in" and not key.isAccelTied() else key.accel()
            except hou.Error:
                acceleration = 0.0
            if acceleration > 0.0001:
                weight = math.sqrt(
                    acceleration ** 2 * (slope ** 2 + hou.fps() ** 2) / (slope ** 2 + 1.0)
                )
                frame_delta = weight / math.sqrt(1.0 + (slope / hou.fps()) ** 2)
            else:
                frame_delta = segment_frames * 0.33
            frame_delta = direction_sign * frame_delta
            frame = key.frame() + frame_delta
            value = key.value() + slope * frame_delta / hou.fps()
            handles.append({
                "direction": direction,
                "key": key,
                "point": self._to_point(frame, value),
            })
        return handles

    def _handle_at(self, position):
        for handle in self._handle_data():
            point = handle["point"]
            if (point.x() - position.x()) ** 2 + (point.y() - position.y()) ** 2 <= 9 * 9:
                return handle
        return None

    def _set_tangent_rotation_lock(self, locked):
        if self._active_handle is None:
            return
        if locked:
            if self._locked_handle_slope is None:
                self._locked_handle_slope = self._handle_start_slope
        else:
            self._locked_handle_slope = None

    def _tangent_drag_values(self, position, shift_held):
        self._set_tangent_rotation_lock(shift_held)
        key = self._active_handle["key"]
        sign = -1.0 if self._active_handle["direction"] == "in" else 1.0
        frame, value = self._from_point(position)
        frame_delta = sign * max(0.01, sign * (frame - key.frame()))
        value_delta = value - key.value()
        if self._locked_handle_slope is not None:
            slope = self._locked_handle_slope
            # Project the cursor onto the handle's ray in screen coordinates.
            origin = self._to_point(key.frame(), key.value())
            direction = self._to_point(key.frame() + sign,
                                       key.value() + sign * slope / hou.fps()) - origin
            offset = position - origin
            length_squared = direction.x() ** 2 + direction.y() ** 2
            distance = ((offset.x() * direction.x() + offset.y() * direction.y())
                        / max(1e-12, length_squared))
            frame_delta = sign * max(0.01, distance)
            value_delta = slope * frame_delta / hou.fps()
        else:
            slope = hou.fps() * value_delta / frame_delta
        self._handle_slope = slope
        acceleration = math.hypot(frame_delta / hou.fps(), value_delta)
        return slope, acceleration

    def keyPressEvent(self, event):
        if self._tangent_transform is not None and event.key() == QtCore.Qt.Key.Key_Escape:
            self.finish_tangent_transform(cancel=True)
            event.accept()
            return
        if event.key() == QtCore.Qt.Key.Key_Shift and self._active_handle is not None:
            self._set_tangent_rotation_lock(True)
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_Shift and self._active_handle is not None:
            if not event.isAutoRepeat():
                self._set_tangent_rotation_lock(False)
            event.accept()
            return
        super().keyReleaseEvent(event)

    def start_tangent_transform(self, mode):
        if (self._tangent_transform is not None or self._active_handle is not None
                or self._press_position is not None or self._panning or self._scrubbing):
            return
        handles = [handle for handle in self._handle_data()
                   if not self._selected_handles or handle["direction"] in self._selected_handles]
        if not handles:
            return
        key = handles[0]["key"]
        origin = self._to_point(key.frame(), key.value())
        position = QtCore.QPointF(self.mapFromGlobal(QtGui.QCursor.pos()))
        anchor = position - origin
        if math.hypot(anchor.x(), anchor.y()) < 4:
            anchor = None
        self._tangent_transform = {
            "mode": mode, "handles": handles, "origin": origin, "anchor": anchor,
        }
        self._set_handle_selection(handle["direction"] for handle in handles)
        self.setFocus(QtCore.Qt.FocusReason.ShortcutFocusReason)
        self.setMouseTracking(True)
        self.grabMouse()
        self.tangentTransformStarted.emit()

    def _update_tangent_transform(self, position):
        state = self._tangent_transform
        origin = state["origin"]
        offset = position - origin
        anchor = state["anchor"]
        if anchor is None:
            if math.hypot(offset.x(), offset.y()) >= 4:
                state["anchor"] = offset
            return
        angle = 0.0
        scale = 1.0
        if state["mode"] == "rotate":
            angle = math.atan2(offset.y(), offset.x()) - math.atan2(anchor.y(), anchor.x())
        else:
            scale = max(0.001, math.hypot(offset.x(), offset.y()) / math.hypot(anchor.x(), anchor.y()))
        cosine, sine = math.cos(angle), math.sin(angle)
        for handle in state["handles"]:
            vector = handle["point"] - origin
            x = scale * (vector.x() * cosine - vector.y() * sine)
            y = scale * (vector.x() * sine + vector.y() * cosine)
            # Time handles stay on their own side of the key.
            sign = -1 if handle["direction"] == "in" else 1
            x = sign * max(0.1, sign * x)
            frame, value = self._from_point(origin + QtCore.QPointF(x, y))
            key = handle["key"]
            frame_delta = frame - key.frame()
            value_delta = value - key.value()
            slope = hou.fps() * value_delta / frame_delta
            acceleration = math.hypot(frame_delta / hou.fps(), value_delta)
            self.tangentChanged.emit(key.frame(), handle["direction"], slope, acceleration)

    def finish_tangent_transform(self, cancel=False):
        if self._tangent_transform is None:
            return
        self._tangent_transform = None
        self.releaseMouse()
        self.setMouseTracking(False)
        self.tangentTransformFinished.emit(cancel)
        self.update()

    def paintEvent(self, event):
        ratio = self.devicePixelRatioF()
        view = (self._range, self.size(), ratio, self.font())
        if view != self._graph_view:
            pixmap = QtGui.QPixmap(math.ceil(self.width() * ratio), math.ceil(self.height() * ratio))
            pixmap.setDevicePixelRatio(ratio)
            with QtGui.QPainter(pixmap) as painter:
                painter.setFont(self.font())
                self._paint_graph(painter)
            self._graph_pixmap = pixmap
            self._graph_view = view

        with QtGui.QPainter(self) as painter:
            painter.drawPixmap(0, 0, self._graph_pixmap)
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
            self._paint_playhead(painter)
            if self._drag_rect is not None:
                painter.setBrush(QtGui.QColor(92, 200, 255, 35))
                painter.setPen(QtGui.QPen(QtGui.QColor("#5cc8ff"), 1, QtCore.Qt.PenStyle.DashLine))
                painter.drawRect(self._drag_rect)
            if self._selection_text:
                rect = QtCore.QRectF(self._plot_rect()).adjusted(8, 0, -8, -8)
                painter.setPen(QtGui.QColor("#e6e6e6"))
                painter.drawText(rect, QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignBottom,
                                 self._selection_text)

    def _paint_graph(self, painter):
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QtGui.QColor("#252525"))
        rect = self._plot_rect()
        painter.fillRect(rect, QtGui.QColor("#1a1a1a"))

        grid_pen = QtGui.QPen(QtGui.QColor("#383838"), 1)
        painter.setPen(grid_pen)
        grid_frames = self._time_grid_frames()
        for frame in grid_frames:
            x = self._to_point(frame, self._range[2]).x()
            painter.drawLine(QtCore.QPointF(x, rect.top() + 18), QtCore.QPointF(x, rect.bottom()))
        for ratio in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = rect.top() + ratio * rect.height()
            painter.drawLine(QtCore.QPointF(rect.left(), y), QtCore.QPointF(rect.right(), y))

        painter.setPen(QtGui.QColor("#a6a6a6"))
        left, right, bottom, top = self._range
        painter.drawText(4, 18, "{:.2f}".format(top))
        painter.drawText(4, rect.bottom(), "{:.2f}".format(bottom))
        if self._keys:
            path = self._curve_path()
            if path is not None:
                painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                first_frame = self._keys[0].frame()
                last_frame = self._keys[-1].frame()
                regions = (
                    (left, min(right, first_frame), True),
                    (max(left, first_frame), min(right, last_frame), False),
                    (max(left, last_frame), right, True),
                )
                for start, end, extension in regions:
                    if end <= start:
                        continue
                    x_start = self._to_point(start, bottom).x()
                    x_end = self._to_point(end, bottom).x()
                    painter.save()
                    painter.setClipRect(QtCore.QRectF(x_start, rect.top(), x_end - x_start, rect.height()))
                    color = self._curve_color.darker(145) if extension else self._curve_color
                    pen = QtGui.QPen(color, 2)
                    if extension:
                        pen.setStyle(QtCore.Qt.PenStyle.DashLine)
                    painter.setPen(pen)
                    painter.drawPath(path)
                    painter.restore()
            painter.setBrush(QtGui.QColor("#ffd16a"))
            painter.setPen(QtGui.QPen(QtGui.QColor("#151515"), 1))
            for key in self._keys:
                point = self._to_point(key.frame(), key.value())
                if key.frame() in self._selected_frames:
                    painter.setBrush(QtGui.QColor("#5cc8ff"))
                    painter.setPen(QtGui.QPen(QtGui.QColor("#e6f7ff"), 2))
                    painter.drawEllipse(point, 6, 6)
                else:
                    painter.setBrush(QtGui.QColor("#ffd16a"))
                    painter.setPen(QtGui.QPen(QtGui.QColor("#151515"), 1))
                    painter.drawEllipse(point, 4, 4)

            for handle in self._handle_data():
                key_point = self._to_point(handle["key"].frame(), handle["key"].value())
                handle_point = handle["point"]
                painter.setPen(QtGui.QPen(QtGui.QColor("#8de5ff"), 1))
                painter.drawLine(key_point, handle_point)
                selected = handle["direction"] in self._selected_handles
                painter.setBrush(QtGui.QColor("#fff1a8" if selected else "#4cbde5"))
                if selected:
                    painter.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 2))
                painter.drawRect(QtCore.QRectF(handle_point.x() - 4, handle_point.y() - 4, 8, 8))

        ruler = QtCore.QRectF(rect.left(), rect.top(), rect.width(), 18)
        painter.fillRect(ruler, QtGui.QColor("#1a1a1a"))
        painter.setPen(QtGui.QColor("#a6a6a6"))
        for frame in grid_frames:
            x = self._to_point(frame, bottom).x()
            label_rect = QtCore.QRectF(x - 30, rect.top() + 2, 60, 16)
            painter.drawText(label_rect, QtCore.Qt.AlignmentFlag.AlignCenter, "{:.0f}".format(frame))

    def _paint_playhead(self, painter):
        left, right, bottom, _ = self._range
        if left <= self._current_frame <= right:
            rect = self._plot_rect()
            x = self._to_point(self._current_frame, bottom).x()
            painter.setPen(QtGui.QPen(QtGui.QColor("#62d6ff"), 1))
            painter.drawLine(QtCore.QPointF(x, rect.top() + 18), QtCore.QPointF(x, rect.bottom()))
            label = QtCore.QRectF(x - 26, rect.top() + 2, 52, 16)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor("#176b89"))
            painter.drawRoundedRect(label, 3, 3)
            painter.setPen(QtGui.QColor("#e6f7ff"))
            painter.drawText(label, QtCore.Qt.AlignmentFlag.AlignCenter, "{:.3g}".format(self._current_frame))

    def _set_selection(self, frames):
        frames = set(frames)
        had_handles = bool(self._selected_handles)
        self._selected_handles.clear()
        if frames != self._selected_frames or had_handles:
            self._graph_view = None
            self._selected_frames = frames
            self.selectionChanged.emit(sorted(frames))
        self.update()

    def _key_at(self, position):
        nearest = None
        nearest_distance = 10 * 10
        for key in self._keys:
            point = self._to_point(key.frame(), key.value())
            distance = (point.x() - position.x()) ** 2 + (point.y() - position.y()) ** 2
            if distance <= nearest_distance:
                nearest = key
                nearest_distance = distance
        return nearest

    def mousePressEvent(self, event):
        self.setFocus(QtCore.Qt.FocusReason.MouseFocusReason)
        if self._tangent_transform is not None:
            if event.button() == QtCore.Qt.MouseButton.LeftButton:
                self._update_tangent_transform(event.position())
                self.finish_tangent_transform()
                self._consume_transform_release = True
            event.accept()
            return
        if event.button() == QtCore.Qt.MouseButton.MiddleButton:
            self._panning = True
            self._pan_start_position = event.position()
            self._pan_start_range = self._range
            self.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            handle = self._handle_at(event.position())
            if handle is not None:
                directions = {handle["direction"]}
                if event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier:
                    directions |= self._selected_handles
                self._set_handle_selection(directions)
                self._active_handle = handle
                frame, value = self._from_point(handle["point"])
                self._handle_slope = hou.fps() * (value - handle["key"].value()) / (frame - handle["key"].frame())
                self._handle_start_slope = self._handle_slope
                self._locked_handle_slope = None
                self._set_tangent_rotation_lock(bool(event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier))
                event.accept()
                return
            if self._is_scrub_gesture(event.position()) and self._key_at(event.position()) is None:
                self._scrubbing = True
                self._set_frame_from_position(event.position())
                event.accept()
                return
            self._press_position = event.position()
            self._pressed_key = self._key_at(event.position())
            self._key_dragging = False
            self._drag_axis = None
            self._dragging = False
            self._drag_rect = None
            self._shift_at_press = bool(event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._tangent_transform is not None:
            self._update_tangent_transform(event.position())
            event.accept()
            return
        if self._panning and event.buttons() & QtCore.Qt.MouseButton.MiddleButton:
            rect = self._plot_rect()
            left, right, bottom, top = self._pan_start_range
            frames_per_pixel = (right - left) / max(1, rect.width())
            offset = (event.position().x() - self._pan_start_position.x()) * frames_per_pixel
            self._range = (left - offset, right - offset, bottom, top)
            self.update()
            event.accept()
            return
        if self._scrubbing and event.buttons() & QtCore.Qt.MouseButton.LeftButton:
            self._set_frame_from_position(event.position())
            event.accept()
            return
        if self._active_handle is not None and event.buttons() & QtCore.Qt.MouseButton.LeftButton:
            key = self._active_handle["key"]
            slope, acceleration = self._tangent_drag_values(
                event.position(), bool(event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier)
            )
            self.tangentChanged.emit(key.frame(), self._active_handle["direction"], slope, acceleration)
            event.accept()
            return
        if self._press_position is not None and event.buttons() & QtCore.Qt.MouseButton.LeftButton:
            offset = event.position() - self._press_position
            if self._pressed_key is not None:
                if not self._key_dragging and offset.manhattanLength() >= 4:
                    self._key_dragging = True
                    self._drag_axis = "horizontal" if abs(offset.x()) >= abs(offset.y()) else "vertical"
                    selection = set(self._selected_frames)
                    if self._pressed_key.frame() not in selection:
                        if not self._shift_at_press:
                            selection.clear()
                        selection.add(self._pressed_key.frame())
                        self._set_selection(selection)
                    self._key_drag_start_range = self._range
                    self.keyDragStarted.emit(sorted(self._selected_frames))
                if self._key_dragging:
                    left, right, bottom, top = self._key_drag_start_range
                    rect = self._plot_rect()
                    frame_delta = 0.0
                    value_delta = 0.0
                    if self._drag_axis == "horizontal":
                        frame_delta = offset.x() * (right - left) / max(1, rect.width())
                    else:
                        value_delta = -offset.y() * (top - bottom) / max(1, rect.height())
                    self.keyDragMoved.emit(frame_delta, value_delta)
                    event.accept()
                    return
            if (event.position() - self._press_position).manhattanLength() >= 4:
                self._dragging = True
                self._drag_rect = QtCore.QRectF(self._press_position, event.position()).normalized()
                self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._consume_transform_release and event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._consume_transform_release = False
            event.accept()
            return
        if event.button() == QtCore.Qt.MouseButton.MiddleButton and self._panning:
            self._panning = False
            self._pan_start_position = None
            self._pan_start_range = None
            self.setCursor(QtCore.Qt.CursorShape.CrossCursor)
            event.accept()
            return
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self._scrubbing:
            self._set_frame_from_position(event.position())
            self._scrubbing = False
            event.accept()
            return
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self._active_handle is not None:
            self._active_handle = None
            self._handle_slope = None
            self._handle_start_slope = None
            self._locked_handle_slope = None
            event.accept()
            return
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self._key_dragging:
            self.keyDragFinished.emit()
            self._press_position = None
            self._pressed_key = None
            self._key_dragging = False
            self._drag_axis = None
            self._key_drag_start_range = None
            event.accept()
            return
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self._press_position is not None:
            if self._dragging and self._drag_rect is not None:
                box_frames = {
                    key.frame() for key in self._keys
                    if self._drag_rect.contains(self._to_point(key.frame(), key.value()))
                }
                box_handles = {
                    handle["direction"] for handle in self._handle_data()
                    if self._drag_rect.contains(handle["point"])
                }
                if not box_frames and box_handles:
                    directions = self._selected_handles | box_handles if self._shift_at_press else box_handles
                    self._set_handle_selection(directions)
                else:
                    selection = self._selected_frames | box_frames if self._shift_at_press else box_frames
                    self._set_selection(selection)
            else:
                key = self._key_at(event.position())
                if key is None:
                    if not self._shift_at_press:
                        self._set_selection(set())
                elif self._shift_at_press:
                    selection = set(self._selected_frames)
                    if key.frame() in selection:
                        selection.remove(key.frame())
                    else:
                        selection.add(key.frame())
                    self._set_selection(selection)
                else:
                    self._set_selection({key.frame()})
            self._press_position = None
            self._pressed_key = None
            self._drag_rect = None
            self._dragging = False
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):
        if self._tangent_transform is not None:
            event.accept()
            return
        delta = event.angleDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return
        left, right, bottom, top = self._range
        old_span = right - left
        steps = delta / 120.0
        new_span = max(0.25, min(100000.0, old_span * (0.85 ** steps)))
        frame_at_cursor, _ = self._from_point(event.position())
        cursor_ratio = (frame_at_cursor - left) / old_span
        self._range = (
            frame_at_cursor - new_span * cursor_ratio,
            frame_at_cursor + new_span * (1.0 - cursor_ratio),
            bottom,
            top,
        )
        self.update()
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if self._plot_rect().contains(event.position().toPoint()) and not self._is_scrub_gesture(event.position()):
            frame, value = self._from_point(event.position())
            self.keyRequested.emit(round(frame), value)
        super().mouseDoubleClickEvent(event)


class AnimationGraphEditor(QtWidgets.QWidget):
    """Panel UI and safe wrappers around Houdini parameter keyframe operations."""

    _CHANNEL_EVENTS = (hou.nodeEventType.ParmTupleAnimated, hou.nodeEventType.ParmTupleChannelChanged)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._node = None
        self._copied_keys = []
        self._building = False
        self._listening = False
        self._watched_node = None
        self._refresh_timer = QtCore.QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self.refresh)
        # Scripted time changes can bypass the playbar's FrameChanged event.
        self._frame_timer = QtCore.QTimer(self)
        self._frame_timer.setTimerType(QtCore.Qt.TimerType.PreciseTimer)
        self._frame_timer.setInterval(16)
        self._frame_timer.timeout.connect(self._sync_frame)
        self.setProperty("houdiniStyle", True)
        self._build_ui()
        self.set_node(self._selected_node())

    def showEvent(self, event):
        super().showEvent(event)
        if not self._listening:
            if hou.isUIAvailable():
                hou.playbar.addEventCallback(self._on_playbar_event)
            self._listening = True
            self._watch_node(self._node)
            self._sync_frame()
            self._frame_timer.start()
            self.refresh()

    def hideEvent(self, event):
        self.stop_listening()
        super().hideEvent(event)

    def stop_listening(self):
        self._frame_timer.stop()
        if self._listening:
            self._listening = False
            if hou.isUIAvailable():
                try:
                    hou.playbar.removeEventCallback(self._on_playbar_event)
                except hou.Error:
                    pass  # The host may already have cleared callbacks while closing.
        self._watch_node(None)
        self._refresh_timer.stop()

    def _on_playbar_event(self, event_type, frame):
        if event_type == hou.playbarEvent.FrameChanged:
            self.canvas.set_playhead(frame)

    def _sync_frame(self):
        self.canvas.set_playhead(hou.frame())

    def _watch_node(self, node):
        if self._watched_node == node:
            return
        if self._watched_node is not None:
            try:
                self._watched_node.removeEventCallback(self._CHANNEL_EVENTS, self._on_channel_edit)
            except hou.Error:
                pass  # A deleted node no longer owns its callback.
        self._watched_node = None
        if node is not None:
            try:
                node.addEventCallback(self._CHANNEL_EVENTS, self._on_channel_edit)
                self._watched_node = node
            except hou.ObjectWasDeleted:
                pass

    def _on_channel_edit(self, parm_tuple=None, **kwargs):
        self._refresh_timer.start(0)

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)

        header = QtWidgets.QHBoxLayout()
        self.parm_combo = QtWidgets.QComboBox()
        self.parm_combo.currentIndexChanged.connect(self._on_parm_changed)
        header.addWidget(self.parm_combo, 1)
        self.mode_buttons = {}
        for mode, label, hotkey in (("automatic", "Bezier", 1), ("linear", "Linear", 2),
                                    ("constant", "Constant", 3)):
            button = QtWidgets.QToolButton()
            button.setAccessibleName(label)
            button.setToolTip(f"{label} ({hotkey})")
            button.setCheckable(True)
            button.setIcon(self._mode_icon(mode))
            button.setIconSize(QtCore.QSize(24, 20))
            button.setStyleSheet("QToolButton:checked { background: #176b89; border: 1px solid #62d6ff; }")
            button.clicked.connect(lambda checked=False, mode=mode: self.set_selected_interpolation(mode))
            self.mode_buttons[mode] = button
            header.addWidget(button)
        root.addLayout(header)

        self.canvas = CurveCanvas()
        self.canvas.keyRequested.connect(self._add_key_at)
        self.canvas.selectionChanged.connect(self._on_graph_selection_changed)
        self.canvas.keyDragStarted.connect(self._begin_key_drag)
        self.canvas.keyDragMoved.connect(self._move_selected_keys)
        self.canvas.keyDragFinished.connect(self._finish_key_drag)
        self.canvas.tangentChanged.connect(self._set_key_tangent)
        self.canvas.tangentTransformStarted.connect(self._begin_tangent_transform)
        self.canvas.tangentTransformFinished.connect(self._finish_tangent_transform)
        root.addWidget(self.canvas, 1)

        self.delete_shortcut = QtGui.QShortcut(QtGui.QKeySequence("Delete"), self)
        self.delete_shortcut.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.delete_shortcut.activated.connect(self.delete_selected_keys)
        self.x_shortcut = QtGui.QShortcut(QtGui.QKeySequence("X"), self)
        self.x_shortcut.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.x_shortcut.activated.connect(self.delete_selected_keys)
        self.select_all_shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+A"), self)
        self.select_all_shortcut.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.select_all_shortcut.activated.connect(self.select_all_keys)
        self.copy_shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+C"), self)
        self.copy_shortcut.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.copy_shortcut.activated.connect(self.copy_selected_keys)
        self.paste_shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+V"), self)
        self.paste_shortcut.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.paste_shortcut.activated.connect(self.paste_keys)
        self.fit_shortcut = QtGui.QShortcut(QtGui.QKeySequence("F"), self)
        self.fit_shortcut.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.fit_shortcut.activated.connect(self.fit_view)
        self.interpolation_shortcuts = []
        for hotkey, mode in (("1", "automatic"), ("2", "linear"), ("3", "constant")):
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(hotkey), self)
            shortcut.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.setAutoRepeat(False)
            shortcut.activated.connect(lambda mode=mode: self.set_selected_interpolation(mode))
            self.interpolation_shortcuts.append(shortcut)
        self.transform_shortcuts = []
        for hotkey, mode in (("R", "rotate"), ("S", "scale")):
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(hotkey), self)
            shortcut.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.setAutoRepeat(False)
            shortcut.activated.connect(lambda mode=mode: self.canvas.start_tangent_transform(mode))
            self.transform_shortcuts.append(shortcut)

        self.transport_shortcuts = []
        for sequence, callback in (("Space", self.toggle_playback),
                                   ("Shift+Space", self.go_to_start_frame),
                                   ("Ctrl+Space", self.toggle_maximized)):
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(sequence), self)
            shortcut.setContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.setAutoRepeat(False)
            shortcut.activated.connect(callback)
            self.transport_shortcuts.append(shortcut)

    def toggle_playback(self):
        if hou.playbar.isPlaying():
            hou.playbar.stop()
        else:
            hou.playbar.play()

    def go_to_start_frame(self):
        hou.playbar.stop()
        hou.setFrame(hou.playbar.playbackRange()[0])
        self.canvas.set_playhead(hou.frame())

    def toggle_maximized(self):
        for tab in hou.ui.paneTabs():
            if tab.type() == hou.paneTabType.PythonPanel and tab.activeInterfaceRootWidget() is self:
                pane = tab.pane()
                if pane is not None:
                    pane.setIsMaximized(not pane.isMaximized())
                return

    @staticmethod
    def _selected_node():
        nodes = hou.selectedNodes()
        return nodes[0] if nodes else None

    @staticmethod
    def _numeric_parms(node):
        if node is None:
            return []
        numeric_types = {hou.parmTemplateType.Float, hou.parmTemplateType.Int}
        return [parm for parm in node.parms()
                if parm.parmTemplate().type() in numeric_types and parm.keyframes()]

    @staticmethod
    def _parm_label(parm):
        label = parm.description() or parm.name()
        components = list(parm.tuple())
        if len(components) > 1:
            index = components.index(parm)
            base = parm.tuple().name()
            suffix = parm.name()[len(base):] if parm.name().startswith(base) else ""
            component = suffix.upper() if suffix else str(index + 1)
            label = "{} {}".format(label, component)
        return label

    def _sync_parm_list(self):
        parms = self._numeric_parms(self._node)
        paths = [parm.path() for parm in parms]
        if paths == [self.parm_combo.itemData(i) for i in range(self.parm_combo.count())]:
            return
        previous = self.parm_combo.currentData()
        blocker = QtCore.QSignalBlocker(self.parm_combo)
        self.parm_combo.clear()
        for parm in parms:
            self.parm_combo.addItem(self._parm_label(parm), parm.path())
        index = self.parm_combo.findData(previous)
        if index >= 0:
            self.parm_combo.setCurrentIndex(index)
        if self.parm_combo.currentData() != previous:
            self.canvas.reset_view()
        del blocker

    def set_node(self, node):
        self.canvas.finish_tangent_transform(cancel=True)
        self._node = node
        if self._listening:
            self._watch_node(node)
        self._building = True
        self.parm_combo.clear()
        if node is not None:
            for parm in self._numeric_parms(node):
                self.parm_combo.addItem(self._parm_label(parm), parm.path())
        self._building = False
        self.canvas.reset_view()
        self.refresh()

    def _current_parm(self):
        path = self.parm_combo.currentData()
        return hou.parm(path) if path else None

    def _on_parm_changed(self):
        if not self._building:
            self.canvas.finish_tangent_transform(cancel=True)
            self.canvas.reset_view()
            self.refresh()

    @staticmethod
    def _channel_color(parm):
        node_type = parm.node().type().nameComponents()[2]
        if node_type in ("xform", "transform") and parm.name() in (
                "tx", "ty", "tz", "rx", "ry", "rz", "sx", "sy", "sz"):
            return {"x": "#ef5350", "y": "#66bb6a", "z": "#529cff"}[parm.name()[-1]]
        return "#ff9d36"

    def refresh(self):
        self._refresh_timer.stop()
        self._sync_parm_list()
        parm = self._current_parm()
        if parm is None:
            self.canvas.set_curve([], lambda frame: 0.0)
            self._update_selection_info()
            return
        try:
            keys = list(parm.keyframes())
            self.canvas.set_curve(keys, parm.evalAtFrame, self._channel_color(parm))
            self._update_selection_info()
        except hou.Error as error:
            self.canvas.set_curve([], lambda frame: 0.0)
            self._update_selection_info()
            self._report_error("Cannot read animation", error)

    def _add_key_at(self, frame, value):
        parm = self._current_parm()
        if parm is None:
            return
        try:
            key = hou.Keyframe()
            key.setFrame(frame)
            key.setValue(value)
            key.unsetInSlope()
            key.unsetInAccel()
            parm.setKeyframe(key)
            hou.setFrame(frame)
            self.refresh()
        except hou.Error as error:
            self._report_error("Could not set keyframe", error)

    def _begin_key_drag(self, frames):
        parm = self._current_parm()
        if parm is None:
            return
        selected_frames = set(frames)
        keys = [key for key in parm.keyframes() if key.frame() in selected_frames]
        self._drag_state = {
            "parm": parm,
            "keys": [(key, key.frame(), key.value()) for key in keys],
            "current_frames": [key.frame() for key in keys],
        }

    def _move_selected_keys(self, frame_delta, value_delta):
        state = getattr(self, "_drag_state", None)
        if not state:
            return
        try:
            parm = state["parm"]
            for frame in state["current_frames"]:
                parm.deleteKeyframeAtFrame(frame)
            new_frames = []
            for key, start_frame, start_value in state["keys"]:
                new_frame = round(start_frame + frame_delta)
                key.setFrame(new_frame)
                key.setValue(start_value + value_delta)
                parm.setKeyframe(key)
                new_frames.append(new_frame)
            state["current_frames"] = new_frames
            self.canvas.set_selected_frames(new_frames)
            self.refresh()
        except hou.Error as error:
            self._drag_state = None
            self._report_error("Could not move selected keyframes", error)

    def _finish_key_drag(self):
        self._drag_state = None

    def _modal_shortcuts(self):
        return ([self.delete_shortcut, self.x_shortcut, self.select_all_shortcut, self.fit_shortcut,
                   self.copy_shortcut, self.paste_shortcut]
                + self.interpolation_shortcuts + self.transform_shortcuts)

    def _begin_tangent_transform(self):
        parm = self._current_parm()
        self._tangent_transform_snapshot = (parm, [hou.Keyframe(key) for key in parm.keyframes()]) if parm else None
        for shortcut in self._modal_shortcuts():
            shortcut.setEnabled(False)

    def _finish_tangent_transform(self, cancel):
        snapshot = getattr(self, "_tangent_transform_snapshot", None)
        self._tangent_transform_snapshot = None
        for shortcut in self._modal_shortcuts():
            shortcut.setEnabled(True)
        if cancel and snapshot is not None:
            parm, keys = snapshot
            try:
                for key in keys:
                    parm.setKeyframe(key)
            except hou.Error as error:
                self._report_error("Could not restore tangent transform", error)
        self.refresh()

    def _set_key_tangent(self, frame, direction, slope, acceleration):
        parm = self._current_parm()
        if parm is None:
            return
        try:
            keys = list(parm.keyframes())
            key_index = next(i for i, key in enumerate(keys) if key.frame() == frame)
            key = keys[key_index]
            slope_tied = key.isSlopeTied()
            accel_tied = key.isAccelTied()
            key.useSlope(True)
            key.useAccel(True)
            key.interpretAccelAsRatio(False)
            if slope_tied:
                key.setSlopeAuto(False)
                key.setInSlopeAuto(False)
                key.setSlope(slope)
                key.unsetInSlope()
            elif direction == "in":
                key.setInSlopeAuto(False)
                key.setInSlope(slope)
            else:
                key.setSlopeAuto(False)
                key.setSlope(slope)
            if accel_tied:
                key.setAccel(acceleration)
                key.unsetInAccel()
            elif direction == "in":
                key.setInAccel(acceleration)
            else:
                key.setAccel(acceleration)
            # A tied edit affects the segments on both sides of the key.
            if key_index > 0 and (direction == "in" or slope_tied or accel_tied):
                keys[key_index - 1].setExpression("bezier()", hou.exprLanguage.Hscript)
                parm.setKeyframe(keys[key_index - 1])
            if direction == "out" or slope_tied or accel_tied:
                key.setExpression("bezier()", hou.exprLanguage.Hscript)
            parm.setKeyframe(key)
            self.refresh()
        except (hou.Error, StopIteration) as error:
            self._report_error("Could not edit Bezier handle", error)

    def set_selected_interpolation(self, mode):
        parm = self._current_parm()
        if (parm is None or not self.canvas.selected_frames
                or self.canvas._active_handle is not None or self.canvas._key_dragging):
            return
        if self.canvas.selected_handles:
            self._set_selected_handle_interpolation(parm, mode)
            return
        try:
            keys = list(parm.keyframes())
            selected = [i for i, key in enumerate(keys) if key.frame() in self.canvas.selected_frames]
            changed = set()
            with hou.undos.group("Set animation graph {}".format(mode)):
                for index in selected:
                    key = keys[index]
                    if mode == "automatic":
                        key.useSlope(True)
                        key.setSlopeAuto(True)
                        key.setInSlopeAuto(True)
                        key.unsetInSlope()
                        key.unsetInAccel()
                        key.interpretAccelAsRatio(True)
                    elif mode == "linear":
                        key.useSlope(True)
                        key.setSlopeAuto(False)
                        key.setInSlopeAuto(False)
                        previous = keys[max(0, index - 1)]
                        following = keys[min(len(keys) - 1, index + 1)]
                        in_slope = (hou.fps() * (key.value() - previous.value())
                                    / (key.frame() - previous.frame())) if index > 0 else None
                        out_slope = (hou.fps() * (following.value() - key.value())
                                     / (following.frame() - key.frame())) if index + 1 < len(keys) else None
                        key.setSlope(out_slope if out_slope is not None else (in_slope or 0.0))
                        key.setInSlope(in_slope if in_slope is not None else (out_slope or 0.0))
                        if key.isAccelTied():
                            try:
                                accel = key.accel()
                            except hou.KeyframeValueNotSet:
                                duration = (following.frame() - previous.frame()) / hou.fps()
                                accel = duration / 3.0
                            key.setAccel(accel)
                            key.setInAccel(accel)
                        key.interpretAccelAsRatio(False)
                    expression = {"automatic": "bezier()", "linear": "linear()",
                                  "constant": "constant()"}[mode]
                    key.setExpression(expression, hou.exprLanguage.Hscript)
                    changed.add(index)
                    # Incoming slopes belong to the segment on the preceding key.
                    if mode != "constant" and index > 0:
                        keys[index - 1].setExpression(expression, hou.exprLanguage.Hscript)
                        changed.add(index - 1)
                for index in sorted(changed):
                    parm.setKeyframe(keys[index])
            self.refresh()
        except hou.Error as error:
            self._report_error("Could not set selected interpolation", error)

    def _set_selected_handle_interpolation(self, parm, mode):
        """Edit one half of a key, preserving the opposite half's settings."""
        try:
            keys = list(parm.keyframes())
            index = next(i for i, key in enumerate(keys) if key.frame() in self.canvas.selected_frames)
            key = keys[index]
            changed = {index}
            with hou.undos.group("Set selected tangent {}".format(mode)):
                if mode != "constant":
                    # Materialize tied components before editing either half.
                    out_auto, in_auto = key.isSlopeAuto(), key.isInSlopeAuto()
                    epsilon = 0.001
                    def slope_for(direction):
                        try:
                            if direction == "in" and not key.isSlopeTied():
                                return key.inSlope()
                            return key.slope()
                        except hou.KeyframeValueNotSet:
                            offset = -epsilon if direction == "in" else epsilon
                            return (parm.evalAtFrame(key.frame() + offset) - key.value()) * hou.fps() / offset
                    outgoing, incoming = slope_for("out"), slope_for("in")
                    if key.isSlopeTied():
                        key.setSlope(outgoing)
                        key.setInSlope(incoming)
                        key.setSlopeAuto(out_auto)
                        key.setInSlopeAuto(in_auto)
                    if key.isAccelTied():
                        try:
                            acceleration = key.accel()
                        except hou.KeyframeValueNotSet:
                            neighbor = keys[index + 1] if index + 1 < len(keys) else keys[max(0, index - 1)]
                            acceleration = abs(neighbor.frame() - key.frame()) / (3 * hou.fps())
                        key.setAccel(acceleration)
                        key.setInAccel(acceleration)
                    key.interpretAccelAsRatio(False)
                    key.useSlope(True)
                for direction in self.canvas.selected_handles:
                    neighbor_index = index - 1 if direction == "in" else index + 1
                    if not 0 <= neighbor_index < len(keys):
                        continue
                    neighbor = keys[neighbor_index]
                    segment_key = neighbor if direction == "in" else key
                    if mode == "constant":
                        segment_key.setExpression("constant()", hou.exprLanguage.Hscript)
                    else:
                        segment_key.setExpression("bezier()", hou.exprLanguage.Hscript)
                        if direction == "in":
                            key.setInSlopeAuto(mode == "automatic")
                        else:
                            key.setSlopeAuto(mode == "automatic")
                        if mode == "linear":
                            slope = hou.fps() * (neighbor.value() - key.value()) / (neighbor.frame() - key.frame())
                            if direction == "in":
                                key.setInSlope(slope)
                            else:
                                key.setSlope(slope)
                    changed.add(index - 1 if direction == "in" else index)
                for changed_index in sorted(changed):
                    parm.setKeyframe(keys[changed_index])
            self.refresh()
        except (hou.Error, StopIteration) as error:
            self._report_error("Could not set selected tangent", error)

    def delete_selected_keys(self):
        parm = self._current_parm()
        frames = self.canvas.selected_frames
        if parm is None or not frames:
            return
        try:
            for frame in frames:
                parm.deleteKeyframeAtFrame(frame)
            self.canvas.set_selected_frames(set())
            self.refresh()
        except hou.Error as error:
            self._report_error("Could not delete selected keyframes", error)

    def copy_selected_keys(self):
        keys = self.canvas.selected_keys()
        if keys:
            self._copied_keys = [hou.Keyframe(key) for key in sorted(keys, key=lambda key: key.frame())]

    def paste_keys(self):
        parm = self._current_parm()
        if parm is None or not self._copied_keys:
            return
        if self.canvas._active_handle is not None or self.canvas._key_dragging:
            return
        try:
            offset = round(hou.frame()) - self._copied_keys[0].frame()
            pasted = []
            for source in self._copied_keys:
                key = hou.Keyframe(source)
                key.setFrame(round(source.frame() + offset))
                pasted.append(key)
            with hou.undos.group("Paste animation graph keys"):
                parm.setKeyframes(pasted)
            self.canvas.set_selected_frames(key.frame() for key in pasted)
            self.refresh()
        except hou.Error as error:
            self._report_error("Could not paste keyframes", error)

    def select_all_keys(self):
        self.canvas.select_all()

    def fit_view(self):
        self.canvas.fit_selected()

    def _on_graph_selection_changed(self, frames):
        self._update_selection_info()

    @staticmethod
    def _mode_icon(mode):
        pixmap = QtGui.QPixmap(48, 40)
        pixmap.setDevicePixelRatio(2)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        with QtGui.QPainter(pixmap) as painter:
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            painter.setPen(QtGui.QPen(QtGui.QColor("#ffd16a"), 1.8))
            path = QtGui.QPainterPath(QtCore.QPointF(2, 16))
            if mode == "automatic":
                path.cubicTo(13, 16, 11, 4, 22, 4)
            elif mode == "constant":
                path.lineTo(13, 16)
                path.lineTo(13, 4)
            path.lineTo(22, 4)
            painter.drawPath(path)
        return QtGui.QIcon(pixmap)

    def _tangent_mode(self, index, side):
        keys = self.canvas._keys
        incoming = side == "in"
        neighbor = keys[index - 1 if incoming else index + 1]
        key = keys[index]
        segment = neighbor if incoming else key
        try:
            if segment.expressionLanguage() != hou.exprLanguage.Hscript:
                return None
            expression = segment.expression().strip()
            if expression in ("constant()", "linear()"):
                return expression[:-2]
            if expression != "bezier()":
                return None
            auto = key.isInSlopeAuto() if incoming and not key.isSlopeTied() else key.isSlopeAuto()
            if not auto:
                slope = key.inSlope() if incoming and not key.isSlopeTied() else key.slope()
                chord = hou.fps() * (neighbor.value() - key.value()) / (neighbor.frame() - key.frame())
                if math.isclose(slope, chord, rel_tol=1e-6, abs_tol=1e-7):
                    return "linear"
            return "automatic"
        except hou.KeyframeValueNotSet:
            return None

    def _update_mode_buttons(self):
        frames = self.canvas.selected_frames
        sides = self.canvas.selected_handles
        keys = self.canvas._keys
        modes = set()
        for index, key in enumerate(keys):
            if key.frame() in frames:
                if index > 0 and (not sides or "in" in sides):
                    modes.add(self._tangent_mode(index, "in"))
                if index + 1 < len(keys) and (not sides or "out" in sides):
                    modes.add(self._tangent_mode(index, "out"))
        for mode, button in self.mode_buttons.items():
            button.setEnabled(bool(frames))
            button.setChecked(modes == {mode})

    def _update_selection_info(self):
        self._update_mode_buttons()
        keys = self.canvas.selected_keys()
        if len(keys) == 1:
            key = keys[0]
            text = "f: {:.0f}   v: {:.6g}".format(key.frame(), key.value())
        elif keys:
            text = "{} keys selected".format(len(keys))
        else:
            text = ""
        self.canvas._selection_text = text
        self.canvas.update()

    @staticmethod
    def _report_error(action, error):
        hou.ui.displayMessage("{}: {}".format(action, error), severity=hou.severityType.Error)

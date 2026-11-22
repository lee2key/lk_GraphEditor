"""Undoable edits and short mouse gestures for the focused graph canvas."""

import math
import time
import bpy
from bpy.props import EnumProperty, FloatProperty
from mathutils import Vector

from . import canvas, curves


clipboard = []
MODES = ('BEZIER', 'LINEAR', 'CONSTANT')


def mouse_in(region, event):
    return Vector((event.mouse_x - region.x, event.mouse_y - region.y))


def plot_for(context, view):
    return canvas.Plot(view, context.region, max(0.8, context.preferences.system.ui_scale))


def indices(points, view):
    return [i for i, key in enumerate(points) if key.co.x in view.selected]


class ActiveGraph:
    @classmethod
    def poll(cls, context):
        space = context.space_data
        return (space and space.type == 'INFO'
                and space.as_pointer() in canvas.views)


class AGE_OT_edit(ActiveGraph, bpy.types.Operator):
    bl_idname = 'age.edit'
    bl_label = 'Edit Animation Graph Keys'
    bl_options = {'UNDO', 'INTERNAL'}

    action: EnumProperty(items=[(v, v.title(), '') for v in (*MODES, 'INSERT', 'PASTE', 'DELETE')])
    frame: FloatProperty()
    value: FloatProperty()

    def execute(self, context):
        view = canvas.views[context.space_data.as_pointer()]
        curve = view.sync(context)
        obj = context.active_object
        if not curves.editable(obj, curve):
            return {'CANCELLED'}
        if self.action in {'INSERT', 'PASTE'}:
            if self.action == 'PASTE' and not clipboard:
                return {'CANCELLED'}
            token = view.token
            curve = curve or curves.ensure_curve(obj, token)
            view.sync(context)
            view.token = token
            points = curve.keyframe_points
            if self.action == 'INSERT':
                key = points.insert(self.frame, self.value)
                key.interpolation = 'BEZIER'
                key.handle_left_type = key.handle_right_type = 'AUTO_CLAMPED'
                view.select([key.co.x])
                context.scene.frame_set(round(self.frame))
            else:
                offset = context.scene.frame_current_final - clipboard[0].center[0]
                pasted = []
                for source in clipboard:
                    k = points.insert(source.center[0] + offset, source.center[1])
                    source.write(k, offset)
                    pasted.append(k.co.x)
                view.select(pasted)
        else:
            points = curve.keyframe_points if curve else []
            selected = indices(points, view)
            if not selected:
                return {'CANCELLED'}
            if self.action == 'DELETE':
                for i in reversed(selected):
                    points.remove(points[i], fast=True)
                view.select(())
            else:
                curves.set_mode(curve, selected, view.sides, self.action)
        curves.notify(context, curve)
        return {'FINISHED'}


class AGE_OT_gesture(ActiveGraph, bpy.types.Operator):
    bl_idname = 'age.gesture'
    bl_label = 'Move Animation Graph Keys or Handles'
    bl_options = {'UNDO', 'BLOCKING', 'INTERNAL'}

    mode: EnumProperty(items=[(v, v.title(), '') for v in ('PICK', 'PAN', 'ROTATE', 'SCALE')])

    def invoke(self, context, event):
        self.view = canvas.views[context.space_data.as_pointer()]
        self.curve = self.view.sync(context)
        self.plot = plot_for(context, self.view)
        self.start = mouse_in(context.region, event)
        self.original_bounds = self.view.bounds
        self.original_selection = (self.view.selected.copy(), self.view.sides.copy())
        self.original_frame = context.scene.frame_current, context.scene.frame_subframe
        self.shift = event.shift
        self.moved = self.changed = False
        self.axis = None
        self.kind = self.mode
        points = self.curve.keyframe_points if self.curve else []
        can_edit = curves.editable(context.active_object, self.curve)
        if self.kind != 'PICK' or self.shift:
            self.view.last_click = None
        else:
            now = time.monotonic()
            last = self.view.last_click
            interval = context.preferences.inputs.mouse_double_click_time / 1000
            double = (event.value == 'DOUBLE_CLICK' or last is not None
                      and now - last[0] <= interval and (self.start - last[1]).length < 5 * self.plot.scale)
            self.view.last_click = None if double else (now, self.start)
            if double and self.plot.contains(self.start) and can_edit:
                frame, value = self.plot.coord(self.start)
                return bpy.ops.age.edit(action='INSERT', frame=round(frame), value=value)
        if self.kind in {'ROTATE', 'SCALE'}:
            handles = list(canvas.handle_points(self.view, points))
            if not handles or not can_edit:
                return {'CANCELLED'}
            self.index = handles[0][0]
            self.target_sides = [side for _, side, _ in handles if not self.view.sides or side in self.view.sides]
        elif self.kind == 'PICK':
            idx, side = canvas.hit(self.view, points, self.plot, self.start)
            if self.start.y >= self.plot.y + self.plot.h or self.start.y < self.plot.y:
                self.kind = 'SCRUB'
            elif idx is None or not self.plot.contains(self.start):
                self.kind = 'BOX'
            elif side:
                self.kind = 'HANDLE'
                self.index, self.target_sides = idx, [side]
                self.view.select([points[idx].co.x], self.view.sides | {side} if self.shift else {side})
            else:
                self.kind = 'KEY'
                self.index = idx
                frame = points[idx].co.x
                if frame not in self.view.selected:
                    self.view.select(self.view.selected | {frame} if self.shift else {frame})
                else:
                    self.view.sides.clear()
        if self.kind in {'HANDLE', 'ROTATE', 'SCALE'}:
            self.original = curves.Key.read(points[self.index])
            self.incoming = points[self.index - 1].interpolation if self.index else None
            self.pivot = self.plot.pixel(self.original.center)
            self.anchor = self.start - self.pivot
        elif self.kind == 'KEY':
            self.saved = {i: curves.Key.read(points[i]) for i in indices(points, self.view)}
            self.limits = curves.frame_limits(points, self.saved)
        elif self.kind == 'SCRUB':
            self.scrub(context, self.start)
        self.can_edit = can_edit
        context.window_manager.modal_handler_add(self)
        context.area.tag_redraw()
        return {'RUNNING_MODAL'}

    def scrub(self, context, pos):
        frame = round(self.plot.coord(pos).x)
        if frame != context.scene.frame_current or context.scene.frame_subframe:
            context.scene.frame_set(frame)

    def update_handles(self, pos, shift):
        targets = {}
        if self.kind == 'HANDLE':
            side = self.target_sides[0]
            initial = self.plot.pixel(getattr(self.original, side)) - self.pivot
            vector = initial + (pos - self.start)
            if shift and initial.length:
                direction = initial.normalized()
                vector = direction * max(0.01, vector.dot(direction))
            targets[side] = self.plot.coord(self.pivot + vector)
        else:
            current = pos - self.pivot
            if self.anchor.length < 3:
                if current.length >= 3:
                    self.anchor = current
                return
            angle = math.atan2(current.y, current.x) - math.atan2(self.anchor.y, self.anchor.x)
            factor = max(0.001, current.length / self.anchor.length)
            for side in self.target_sides:
                v = self.plot.pixel(getattr(self.original, side)) - self.pivot
                if self.kind == 'ROTATE':
                    c, s = math.cos(angle), math.sin(angle)
                    v = Vector((v.x * c - v.y * s, v.x * s + v.y * c))
                else:
                    v *= factor
                targets[side] = self.plot.coord(self.pivot + v)
        curves.move_handles(self.curve, self.index, self.original, targets)
        self.changed = True

    def update(self, context, pos, shift):
        delta = pos - self.start
        self.moved |= delta.length >= 3 * self.plot.scale
        if self.kind == 'SCRUB':
            self.scrub(context, pos)
        elif self.kind == 'PAN':
            a, b, low, high = self.original_bounds
            dx = delta.x * self.plot.dx
            self.view.bounds = (a - dx, b - dx, low, high)
        elif self.kind == 'BOX':
            if self.moved:
                self.view.box = (self.start, pos)
        elif self.can_edit and self.moved:
            if self.kind == 'KEY':
                self.axis = self.axis or ('X' if abs(delta.x) >= abs(delta.y) else 'Y')
                dx = max(self.limits[0], min(self.limits[1], round(delta.x * self.plot.dx))) if self.axis == 'X' else 0
                dy = delta.y * self.plot.dy if self.axis == 'Y' else 0
                points = self.curve.keyframe_points
                for i, key in self.saved.items():
                    key.place(points[i], dx, dy)
                self.view.select(points[i].co.x for i in self.saved)
                self.changed = True
            else:
                self.update_handles(pos, shift)
            if self.changed:
                curves.notify(context, self.curve)
        context.area.tag_redraw()

    def select_box(self, pos):
        points = self.curve.keyframe_points if self.curve else []
        def contains(co):
            x, y = self.plot.pixel(co)
            a, b = self.start, pos
            return min(a.x,b.x) <= x <= max(a.x,b.x) and min(a.y,b.y) <= y <= max(a.y,b.y)
        frames = {k.co.x for k in points if contains(k.co)} if self.moved else set()
        sides = {s for _, s, co in canvas.handle_points(self.view, points) if contains(co)} if self.moved else set()
        if sides and not frames:
            self.view.select(self.view.selected, (self.view.sides | sides) if self.shift else sides)
        else:
            self.view.select(self.view.selected | frames if self.shift else frames)

    def cancel(self, context):
        if self.changed:
            points = self.curve.keyframe_points
            if self.kind == 'KEY':
                for i, key in self.saved.items():
                    key.write(points[i])
            else:
                self.original.write(points[self.index])
                if self.incoming is not None:
                    points[self.index - 1].interpolation = self.incoming
            curves.notify(context, self.curve)
        if self.kind == 'SCRUB':
            context.scene.frame_set(self.original_frame[0], subframe=self.original_frame[1])
        self.view.bounds = self.original_bounds
        self.view.select(*self.original_selection)
        self.view.box = None
        context.area.tag_redraw()

    def modal(self, context, event):
        if event.type in {'ESC', 'RIGHTMOUSE', 'WINDOW_DEACTIVATE'}:
            self.cancel(context)
            return {'CANCELLED'}
        pos = mouse_in(context.region, event)
        if event.type in {'MOUSEMOVE', 'LEFT_SHIFT', 'RIGHT_SHIFT'}:
            self.update(context, pos, event.shift)
        confirm = ('MIDDLEMOUSE', 'RELEASE') if self.kind == 'PAN' else ('LEFTMOUSE', 'RELEASE')
        if self.kind in {'ROTATE', 'SCALE'}:
            confirm = ('LEFTMOUSE', 'PRESS')
        if (event.type, event.value) == confirm:
            self.update(context, pos, event.shift)
            if self.moved:
                self.view.last_click = None
            if self.kind == 'BOX':
                self.select_box(pos)
            elif self.kind == 'KEY' and not self.moved:
                frame = self.curve.keyframe_points[self.index].co.x
                selected, _ = self.original_selection
                self.view.select(selected ^ {frame} if self.shift else {frame})
            self.view.box = None
            context.area.tag_redraw()
            return {'FINISHED'} if self.changed else {'CANCELLED'}
        return {'RUNNING_MODAL'}

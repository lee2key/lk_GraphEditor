"""Transient per-editor view, hit testing and drawing in Blender's graph region."""

import math
from itertools import chain

import blf
import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

from . import curves


views = {}
BACKGROUND = (0.105, 0.105, 0.105, 1)
GRID = (0.22, 0.22, 0.22, 1)
TEXT = (0.8, 0.8, 0.8, 1)
BLUE = (0.25, 0.75, 0.95, 1)
GOLD = (1, 0.74, 0.36, 1)


class View:
    def __init__(self):
        self.owner = self.token = None
        self.initial_token = None
        self.selected = set()
        self.sides = set()
        self.bounds = (0, 24, -1, 1)
        self.box = None
        self.last_click = None

    def sync(self, context):
        obj = context.active_object
        action = curves.action_of(obj)
        owner = (obj.session_uid if obj else None, action.session_uid if action else None,
                 obj.animation_data.action_slot_handle if action else None)
        available = curves.channels(obj)
        if self.owner != owner or (self.token not in available and
                                  (not obj or self.token not in curves.transforms(obj))):
            self.owner = owner
            default = next(iter(available), (0, 0, 'location', 0) if obj else None)
            self.token = self.initial_token if self.initial_token in available else default
            self.initial_token = None
            self.select(())
            self.fit(context, available.get(self.token))
        curve = available.get(self.token)
        if self.selected:
            frames = (k.co.x for k in curve.keyframe_points) if curve else ()
            self.selected.intersection_update(frames)
            if len(self.selected) != 1:
                self.sides.clear()
        return curve

    def select(self, frames, sides=()):
        self.selected = set(frames)
        self.sides = set(sides) if len(self.selected) == 1 else set()

    def fit(self, context, curve, selection=False):
        coords = [k.co for k in curve.keyframe_points] if curve else []
        if selection:
            coords = [co for co in coords if co.x in self.selected] or coords
        if coords:
            left, right = min(c.x for c in coords), max(c.x for c in coords)
            low, high = min(c.y for c in coords), max(c.y for c in coords)
            pad = max(1, (right - left) * 0.1) if left != right else 8
            dy = max(0.5, (high - low) * 0.15)
            self.bounds = (left - pad, right + pad, low - dy, high + dy)
        else:
            scene = context.scene
            value = 0
            if self.token and context.active_object and self.token in curves.transforms(context.active_object):
                value = getattr(context.active_object, self.token[2])[self.token[3]]
            self.bounds = (scene.frame_start - 1, max(scene.frame_start + 1, scene.frame_end + 1),
                           value - 1, value + 1)


class Plot:
    def __init__(self, view, region, scale):
        self.scale = scale
        self.x, self.y = 56 * scale, 25 * scale
        self.w = max(1, region.width - self.x - 12 * scale)
        self.h = max(1, region.height - self.y - 26 * scale)
        self.left, right, self.low, high = view.bounds
        self.dx, self.dy = (right - self.left) / self.w, (high - self.low) / self.h

    def pixel(self, co):
        return Vector((self.x + (co[0] - self.left) / self.dx, self.y + (co[1] - self.low) / self.dy))

    def coord(self, pixel):
        return Vector((self.left + (pixel[0] - self.x) * self.dx, self.low + (pixel[1] - self.y) * self.dy))

    def contains(self, point):
        return self.x <= point.x <= self.x + self.w and self.y <= point.y <= self.y + self.h


def handle_points(view, points):
    if len(view.selected) == 1:
        for i, key in enumerate(points):
            if key.co.x in view.selected:
                if i:
                    yield i, 'left', key.handle_left
                if i + 1 < len(points):
                    yield i, 'right', key.handle_right
                break


def hit(view, points, plot, mouse):
    hits = [(i, side, co) for i, side, co in handle_points(view, points)]
    hits.extend((i, '', k.co) for i, k in enumerate(points))
    for i, side, co in sorted(hits, key=lambda item: (plot.pixel(item[2]) - mouse).length_squared):
        if (plot.pixel(co) - mouse).length <= 8 * plot.scale:
            return i, side
        break
    return None, ''


def step(span, pixels, spacing):
    target = max(1e-12, span * spacing / pixels)
    power = 10 ** math.floor(math.log10(target))
    return next(f * power for f in (1, 2, 5, 10) if f * power >= target)


def ticks(low, high, interval):
    return (i * interval for i in range(math.ceil(low / interval), math.floor(high / interval) + 1))


def geometry(primitive, coords, color, width=1):
    coords = list(coords)
    if not coords:
        return
    is_line = primitive in {'LINES', 'LINE_STRIP'}
    shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR' if is_line else 'UNIFORM_COLOR')
    batch = batch_for_shader(shader, primitive, {'pos': [(x, y, 0) for x, y in coords]})
    shader.bind()
    shader.uniform_float('color', color)
    if is_line:
        shader.uniform_float('viewportSize', gpu.state.viewport_get()[2:])
        shader.uniform_float('lineWidth', width)
    batch.draw(shader)


def rect(x, y, w, h, color):
    geometry('TRIS', [(x,y), (x+w,y), (x+w,y+h), (x,y), (x+w,y+h), (x,y+h)], color)


def caption(x, y, text, size, color=TEXT):
    blf.position(0, x, y, 0)
    blf.size(0, max(8, round(size)))
    blf.color(0, *color)
    blf.draw(0, text)


def curve_lines(curve, plot, bounds, color):
    left, right = bounds[:2]
    points = curve.keyframe_points
    frames = [k.co.x for k in points]
    boundaries = [left, *(f for f in frames if left < f < right), right]
    holds = {frames[i + 1]: k.co.y for i, k in enumerate(points[:-1])
             if k.interpolation == 'CONSTANT'} if not curve.modifiers else {}
    solid, dashed = [], []
    for a, b in zip(boundaries, boundaries[1:]):
        extension = bool(frames) and (b <= frames[0] or a >= frames[-1])
        count = max(1, math.ceil((b - a) / plot.dx / ((5 if extension else 2) * plot.scale)))
        previous = None
        for j in range(count + 1):
            f = a + (b - a) * j / count if j < count else b
            y = curve.evaluate(f)
            if not math.isfinite(y):
                previous = None
                continue
            pixel = plot.pixel((f, y))
            if previous is not None:
                if extension:
                    if j % 2:
                        dashed.extend((previous, pixel))
                else:
                    if j == count and b in holds:
                        corner = plot.pixel((b, holds[b]))
                        solid.extend((previous, corner))
                        previous = corner
                    solid.extend((previous, pixel))
            previous = pixel
    geometry('LINES', dashed, tuple(v * 0.6 for v in color[:3]) + (1,), 1.5 * plot.scale)
    geometry('LINES', solid, color, 2 * plot.scale)


def draw():
    context = bpy.context
    view = views.get(context.space_data.as_pointer())
    if not view:
        return
    curve = view.sync(context)
    region = context.region
    scale = max(0.8, context.preferences.system.ui_scale)
    plot = Plot(view, region, scale)
    left, right, low, high = view.bounds
    x, y, w, h = plot.x, plot.y, plot.w, plot.h
    frames = list(ticks(left, right, max(1, step(right - left, w, 82 * scale))))
    values = list(ticks(low, high, step(high - low, h, 65 * scale)))
    grid = []
    for frame in frames:
        px = plot.pixel((frame, low)).x
        grid.extend(((px, y), (px, y + h)))
    for value in values:
        py = plot.pixel((left, value)).y
        grid.extend(((x, py), (x + w, py)))
    rect(0, 0, region.width, region.height, BACKGROUND)
    geometry('LINES', grid, GRID)
    points = curve.keyframe_points if curve else []
    if curve:
        color = (1, 0.61, 0.22, 1)
        if curve.color_mode == 'CUSTOM':
            color = (*curve.color, 1)
        elif curve.data_path.rsplit('.', 1)[-1] in {'location', 'scale', 'rotation_euler'}:
            color = ((0.95, .34, .31, 1), (.41, .78, .42, 1), (.34, .62, 1, 1))[curve.array_index % 3]
        curve_lines(curve, plot, view.bounds, color)
    handles = list(handle_points(view, points))
    geometry('LINES', chain.from_iterable((plot.pixel(points[i].co), plot.pixel(co))
                                          for i, _, co in handles), GOLD)
    # Batch diamonds so drawing many keys doesn't create a GPU batch per key.
    markers = {GOLD: [], TEXT: [], BLUE: []}
    for co, selected, is_handle in chain(((k.co, k.co.x in view.selected, False) for k in points),
                                          ((co, side in view.sides, True) for _, side, co in handles)):
        px, py = plot.pixel(co)
        r = (4 if is_handle else 5) * scale
        color = BLUE if selected else GOLD if is_handle else TEXT
        markers[color].extend(((px-r,py), (px,py+r), (px+r,py), (px-r,py), (px+r,py), (px,py-r)))
    for color, vertices in markers.items():
        geometry('TRIS', vertices, color)
    if view.box:
        a, b = view.box
        geometry('LINE_STRIP', [a, (b.x,a.y), b, (a.x,b.y), a], BLUE)
    for bounds in ((0,0,x,region.height), (x+w,0,region.width-x-w,region.height),
                   (x,0,w,y), (x,y+h,w,region.height-y-h)):
        rect(*bounds, BACKGROUND)
    ruler_y = (y + h + region.height) / 2
    blf.size(0, max(8, round(11 * scale)))
    for frame in frames:
        label = f'{frame:g}'
        tw, th = blf.dimensions(0, label)
        caption(plot.pixel((frame, low)).x - tw / 2, ruler_y - th / 2, label, 11 * scale)
    for value in values:
        caption(4 * scale, plot.pixel((left,value)).y, f'{value:.4g}', 11 * scale)
    frame = context.scene.frame_current_final
    px = plot.pixel((frame, low)).x
    if x <= px <= x + w:
        label = f'{frame:g}'
        blf.size(0, max(8, round(12 * scale)))
        tw, th = blf.dimensions(0, label)
        width = max(32 * scale, tw + 12 * scale)
        height = max(18 * scale, th + 4 * scale)
        cx = min(max(px, width / 2), region.width - width / 2)
        base = ruler_y - height / 2
        geometry('LINES', [(px, y), (px, base)], BLUE, 1.5 * scale)
        rect(cx - width / 2, base, width, height, BLUE)
        rect(cx - width / 2 + scale, base + scale, width - 2 * scale, height - 2 * scale,
             (0.09, 0.39, 0.51, 1))
        caption(cx - tw / 2, ruler_y - th / 2, label, 12 * scale, (1, 1, 1, 1))
    selected = [k for k in points if k.co.x in view.selected]
    if not context.active_object:
        info = 'Select an object to edit its animation'
    elif not curves.editable(context.active_object, curve):
        anim = context.active_object.animation_data
        info = 'Exit NLA Tweak Mode to edit' if anim and anim.use_tweak_mode else 'Read only'
    elif not points:
        info = 'Double-click to add the first key'
    elif len(selected) == 1:
        info = f'f {selected[0].co.x:g}    v {selected[0].co.y:.6g}'
    else:
        info = f'{len(selected)} keys selected' if selected else ''
    if info:
        caption(x + 6 * scale, y + 7 * scale, info, 12 * scale)

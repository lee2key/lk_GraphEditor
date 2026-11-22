"""Access the active object's Action slot and edit native Blender keyframes."""

import math
from dataclasses import dataclass

import bpy
from mathutils import Vector


def action_of(obj):
    anim = obj.animation_data if obj else None
    return anim.action if anim else None


def channels(obj):
    action = action_of(obj)
    if not action or not obj.animation_data.action_slot:
        return {}
    slot = obj.animation_data.action_slot
    result = {}
    for layer_index, layer in enumerate(action.layers):
        for strip_index, strip in enumerate(layer.strips):
            bag = strip.channelbag(slot) if strip.type == 'KEYFRAME' else None
            if bag:
                for curve in bag.fcurves:
                    token = (layer_index, strip_index, curve.data_path, curve.array_index)
                    result[token] = curve
    return result


def transforms(obj):
    rotation = {'QUATERNION': 'rotation_quaternion', 'AXIS_ANGLE': 'rotation_axis_angle'}
    for path in ('location', rotation.get(obj.rotation_mode, 'rotation_euler'), 'scale'):
        for index in range(len(getattr(obj, path))):
            yield (0, 0, path, index)


def label(token):
    layer, strip, path, index = token
    axes = {'location': 'XYZ', 'scale': 'XYZ', 'rotation_euler': 'XYZ',
            'rotation_quaternion': 'WXYZ', 'rotation_axis_angle': 'AXYZ'}
    prop = path.rsplit('.', 1)[-1]
    if prop in axes and index < len(axes[prop]):
        suffix = 'Angle' if prop == 'rotation_axis_angle' and index == 0 else axes[prop][index]
        name = path.removesuffix(prop) + prop.replace('_', ' ').title() + ' ' + suffix
    else:
        name = f'{path} [{index}]'
    return f'{name} (Layer {layer + 1}, Strip {strip + 1})' if layer or strip else name


def editable(obj, curve):
    action = action_of(obj)
    return (obj and obj.is_editable and (not action or action.is_editable)
            and not (obj.animation_data and obj.animation_data.use_tweak_mode)
            and (curve is None or not (curve.lock or (curve.group and curve.group.lock)
                                       or len(curve.sampled_points))))


def ensure_curve(obj, token):
    action = action_of(obj)
    if not action:
        anim = obj.animation_data_create()
        action = bpy.data.actions.new(name=f'{obj.name} Action')
        anim.action = action
    return action.fcurve_ensure_for_datablock(obj, token[2], index=token[3])


def notify(context, curve):
    curve.update()
    curve.id_data.update_tag()
    obj = context.active_object
    if obj:
        obj.update_tag(refresh={'TIME'})
    for area in context.screen.areas:
        area.tag_redraw()


@dataclass(frozen=True)
class Key:
    center: tuple
    left: tuple
    right: tuple
    left_type: str
    right_type: str
    interpolation: str
    kind: str
    easing: str
    back: float
    amplitude: float
    period: float

    @classmethod
    def read(cls, k):
        return cls(tuple(k.co), tuple(k.handle_left), tuple(k.handle_right),
                   k.handle_left_type, k.handle_right_type, k.interpolation,
                   k.type, k.easing, k.back, k.amplitude, k.period)

    def place(self, k, dx=0, dy=0):
        k.co = self.center[0] + dx, self.center[1] + dy
        k.handle_left = self.left[0] + dx, self.left[1] + dy
        k.handle_right = self.right[0] + dx, self.right[1] + dy

    def write(self, k, dx=0):
        k.interpolation, k.type, k.easing = self.interpolation, self.kind, self.easing
        k.back, k.amplitude, k.period = self.back, self.amplitude, self.period
        k.handle_left_type, k.handle_right_type = self.left_type, self.right_type
        self.place(k, dx)


def frame_limits(points, selected):
    """Keep groups ordered and avoid Blender merging keys on occupied frames."""
    low, high = -math.inf, math.inf
    for i in selected:
        frame = points[i].co.x
        if i and i - 1 not in selected:
            low = max(low, min(0, math.ceil(points[i - 1].co.x - frame + 0.011)))
        if i + 1 < len(points) and i + 1 not in selected:
            high = min(high, max(0, math.floor(points[i + 1].co.x - frame - 0.011)))
    return low, high


def preserve_handle(curve, index, side, original):
    # Blender may recompute even the untouched automatic handle when the
    # adjacent segment changes. Keep it automatic whenever its position holds.
    curve.update()
    k = curve.keyframe_points[index]
    attr = 'handle_' + side
    if (getattr(k, attr) - Vector(original)).length > 1e-6:
        setattr(k, attr + '_type', 'FREE')
        setattr(k, attr, original)


def set_mode(curve, indices, sides, mode):
    points = curve.keyframe_points
    for i in indices:
        k = points[i]
        if not sides:
            k.interpolation = mode
            if mode != 'CONSTANT':
                if i:
                    points[i - 1].interpolation = mode
                kind = 'AUTO_CLAMPED' if mode == 'BEZIER' else 'VECTOR'
                k.handle_left_type = k.handle_right_type = kind
            continue
        untouched = None
        if len(sides) == 1:
            untouched = 'right' if 'left' in sides else 'left'
            old_position = tuple(getattr(k, 'handle_' + untouched))
        for side in sides:
            segment = points[i - 1] if side == 'left' and i else k
            if mode == 'CONSTANT':
                segment.interpolation = mode
                continue
            segment.interpolation = 'BEZIER'
            other = 'right' if side == 'left' else 'left'
            if getattr(k, f'handle_{other}_type') == 'ALIGNED':
                setattr(k, f'handle_{other}_type', 'FREE')
            setattr(k, f'handle_{side}_type', 'VECTOR' if mode == 'LINEAR' else 'AUTO_CLAMPED')
        if untouched:
            preserve_handle(curve, i, untouched, old_position)


def move_handles(curve, index, original, targets):
    points = curve.keyframe_points
    k = points[index]
    tied_types = {'AUTO', 'AUTO_CLAMPED', 'ALIGNED'}
    tied = original.left_type in tied_types and original.right_type in tied_types
    origin = Vector(original.center)
    positions = {}
    for side, position in targets.items():
        sign = -1 if side == 'left' else 1
        vector = Vector(position) - origin
        vector.x = sign * max(0.0001, sign * vector.x)
        positions[side] = origin + vector
        (points[index - 1] if side == 'left' and index else k).interpolation = 'BEZIER'
    if tied:
        if len(positions) == 1:
            side = next(iter(positions))
            other = 'right' if side == 'left' else 'left'
            length = (Vector(getattr(original, other)) - origin).length
            positions[other] = origin - (positions[side] - origin).normalized() * length
        k.handle_left_type = k.handle_right_type = 'ALIGNED'
    for side, position in positions.items():
        if not tied:
            setattr(k, f'handle_{side}_type', 'FREE')
        setattr(k, f'handle_{side}', position)
    if not tied and len(targets) == 1:
        other = 'right' if 'left' in targets else 'left'
        preserve_handle(curve, index, other, getattr(original, other))

"""A single-channel F-Curve editor in a Blender area."""

import json

import bpy
from bl_ui import space_dopesheet
from bpy.app.handlers import persistent
from bpy.props import EnumProperty, IntProperty, StringProperty

from . import canvas, curves, operators


_draw_handle = None
_layout_key = 'animation_graph_editor_layout'


def close_view(area, space):
    view = canvas.views.pop(space.as_pointer(), None)
    if view:
        if area.spaces.active == space and view.previous_type:
            area.ui_type = view.previous_type
        area.tag_redraw()


class AGE_OT_channel(operators.ActiveGraph, bpy.types.Operator):
    bl_idname = 'age.channel'
    bl_label = 'Choose Animation Channel'
    bl_options = {'INTERNAL'}

    path: StringProperty()
    index: IntProperty()
    layer: IntProperty()
    strip: IntProperty()

    def execute(self, context):
        view = canvas.views[context.space_data.as_pointer()]
        view.sync(context)
        view.token = self.layer, self.strip, self.path, self.index
        view.select(())
        view.fit(context, curves.channels(context.active_object).get(view.token))
        context.area.tag_redraw()
        return {'FINISHED'}


class AGE_MT_channels(bpy.types.Menu):
    bl_idname = 'AGE_MT_channels'
    bl_label = 'Animation Channels'

    def draw(self, context):
        obj = context.active_object
        if not obj:
            self.layout.label(text='Select an object')
            return
        available = curves.channels(obj)
        extra = [t for t in curves.transforms(obj) if t not in available]
        for title, tokens in (('Action Channels', available), ('Add Transform Channel', extra)):
            if tokens:
                self.layout.label(text=title)
                for token in tokens:
                    op = self.layout.operator('age.channel', text=curves.label(token))
                    op.layer, op.strip, op.path, op.index = token


# Timeline shares its playback controls with other animation editors. Wrap only
# the Dope Sheet module's binding, and only decorate the Timeline layout.
class TimelineLayout:
    def __init__(self, layout):
        self.layout = layout

    def __getattr__(self, name):
        return getattr(self.layout, name)

    def popover(self, **kwargs):
        result = self.layout.popover(**kwargs)
        if kwargs.get('panel') == 'TIME_PT_playback':
            self.layout.operator('age.open', text='', icon='FCURVE')
        return result


def timeline_playback(layout, context):
    if context.area.ui_type == 'TIMELINE':
        layout = TimelineLayout(layout)
    return _native_playback(layout, context)


def timeline_button(self, context):
    if context.area.ui_type == 'TIMELINE':
        self.layout.operator('age.open', text='', icon='FCURVE')


def header(self, context):
    space = context.space_data
    view = canvas.views.get(space.as_pointer())
    if not view:
        return
    row = self.layout.row(align=True)
    row.operator('age.open', text='', icon='FCURVE', depress=bool(view))
    view.sync(context)
    row.menu('AGE_MT_channels', text=curves.label(view.token) if view.token else 'Channels', icon='DOWNARROW_HLT')


def tangent_buttons(self, context):
    view = canvas.views.get(context.space_data.as_pointer())
    if not view:
        return
    curve = view.sync(context)
    self.layout.separator_spacer()
    modes = self.layout.row(align=True)
    points = curve.keyframe_points if curve else []
    selected = operators.indices(points, view)
    modes.enabled = bool(selected and curves.editable(context.active_object, curve))
    active_modes = set()
    for i in selected:
        for side, neighbor in (('left', i - 1), ('right', i + 1)):
            if not 0 <= neighbor < len(points) or (view.sides and side not in view.sides):
                continue
            mode = (points[i-1] if side == 'left' else points[i]).interpolation
            if mode == 'BEZIER' and getattr(points[i], f'handle_{side}_type') == 'VECTOR':
                mode = 'LINEAR'
            active_modes.add(mode)
    for mode in operators.MODES:
        modes.operator('age.edit', text='', icon='IPO_' + mode, depress=active_modes == {mode}).action = mode


class AGE_OT_open(bpy.types.Operator):
    bl_idname = 'age.open'
    bl_label = 'Animation Graph Editor'
    bl_description = 'Open or close the focused animation editor in this area'

    @classmethod
    def poll(cls, context):
        return context.area and context.area.type not in {'TOPBAR', 'STATUSBAR'}

    def execute(self, context):
        area = context.area
        space = area.spaces.active
        if space.as_pointer() in canvas.views:
            close_view(area, space)
            return {'FINISHED'}
        previous = area.ui_type
        # The Info space provides a lightweight drawable region without the
        # native Graph Editor painting a second playhead above our canvas.
        area.type = 'INFO'
        space = area.spaces.active
        view = canvas.View()
        view.previous_type = previous
        space_id = space.as_pointer()
        canvas.views[space_id] = view
        area.tag_redraw()
        return {'FINISHED'}


class AGE_OT_control(operators.ActiveGraph, bpy.types.Operator):
    bl_idname = 'age.control'
    bl_label = 'Animation Graph Navigation'
    bl_options = {'INTERNAL'}

    action: EnumProperty(items=[(v, v.title(), '') for v in
                                ('FIT', 'ALL', 'COPY', 'ZOOM_IN', 'ZOOM_OUT', 'START', 'PLAY', 'CLOSE')])

    def invoke(self, context, event):
        view = canvas.views[context.space_data.as_pointer()]
        curve = view.sync(context)
        points = curve.keyframe_points if curve else []
        if self.action == 'CLOSE':
            close_view(context.area, context.space_data)
        elif self.action == 'FIT':
            view.fit(context, curve, selection=True)
        elif self.action == 'ALL':
            view.select(k.co.x for k in points)
        elif self.action == 'COPY':
            copied = [curves.Key.read(k) for k in points if k.co.x in view.selected]
            if copied:
                operators.clipboard[:] = copied
        elif self.action == 'START':
            if context.screen.is_animation_playing:
                bpy.ops.screen.animation_cancel(restore_frame=False)
            scene = context.scene
            scene.frame_set(scene.frame_preview_start if scene.use_preview_range else scene.frame_start)
        elif self.action == 'PLAY':
            bpy.ops.screen.animation_play()
        else:
            pos = operators.mouse_in(context.region, event)
            frame = operators.plot_for(context, view).coord(pos).x
            a, b, low, high = view.bounds
            span = max(0.25, min(1000000, (b - a) * (0.85 if self.action == 'ZOOM_IN' else 1 / 0.85)))
            start = frame - (frame - a) * span / (b - a)
            view.bounds = start, start + span, low, high
        context.area.tag_redraw()
        return {'FINISHED'}


classes = (operators.AGE_OT_edit, operators.AGE_OT_gesture, AGE_OT_channel, AGE_MT_channels,
           AGE_OT_open, AGE_OT_control)
_bindings = []


def register_keys():
    config = bpy.context.window_manager.keyconfigs.addon
    if not config:
        return
    km = config.keymaps.new(name='Info', space_type='INFO', region_type='WINDOW')

    def bind(op, key, value='PRESS', **modifiers):
        item = km.keymap_items.new('age.' + op, key, value, head=True, **modifiers)
        _bindings.append((km, item))
        return item.properties

    for key, action in (('ONE', 'BEZIER'), ('TWO', 'LINEAR'), ('THREE', 'CONSTANT'),
                        ('DEL', 'DELETE'), ('X', 'DELETE'), ('V', 'PASTE')):
        bind('edit', key, ctrl=key == 'V').action = action
    for key, action in (('A', 'ALL'), ('C', 'COPY'), ('F', 'FIT'), ('SPACE', 'PLAY'), ('ESC', 'CLOSE')):
        bind('control', key, ctrl=key in {'A', 'C'}).action = action
    bind('control', 'SPACE', shift=True).action = 'START'
    for key, mode in (('LEFTMOUSE', 'PICK'), ('R', 'ROTATE'), ('S', 'SCALE')):
        bind('gesture', key).mode = mode
    bind('gesture', 'LEFTMOUSE', shift=True).mode = 'PICK'
    bind('gesture', 'LEFTMOUSE', 'DOUBLE_CLICK').mode = 'PICK'
    # Blender handles region navigation before editor-specific keymaps.
    # The operator polls restrict these bindings to an open animation canvas.
    km = config.keymaps.new(name='View2D', space_type='EMPTY', region_type='WINDOW')
    bind('gesture', 'MIDDLEMOUSE').mode = 'PAN'
    bind('control', 'WHEELUPMOUSE').action = 'ZOOM_IN'
    bind('control', 'WHEELDOWNMOUSE').action = 'ZOOM_OUT'


@persistent
def redraw_views(*_):
    if canvas.views:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.spaces.active.as_pointer() in canvas.views:
                    area.tag_redraw()


@persistent
def save_layout(_):
    for screen in bpy.data.screens:
        if not screen.is_editable:
            continue
        layout = []
        for index, area in enumerate(screen.areas):
            view = canvas.views.get(area.spaces.active.as_pointer())
            if view:
                layout.append((index, view.previous_type, view.token))
        if layout:
            screen[_layout_key] = json.dumps(layout)
        elif _layout_key in screen:
            del screen[_layout_key]


@persistent
def load_layout(_):
    for screen in bpy.data.screens:
        if _layout_key not in screen:
            continue
        try:
            for index, previous, token in json.loads(screen[_layout_key]):
                if 0 <= index < len(screen.areas) and screen.areas[index].type == 'INFO':
                    area = screen.areas[index]
                    view = canvas.View()
                    view.previous_type = previous
                    view.initial_token = tuple(token) if token else None
                    canvas.views[area.spaces.active.as_pointer()] = view
                    area.tag_redraw()
        except (ValueError, TypeError):
            continue


@persistent
def reset_views(_):
    for screen in bpy.data.screens:
        for area in screen.areas:
            for space in list(area.spaces):
                if space.as_pointer() in canvas.views:
                    close_view(area, space)
    canvas.views.clear()
    operators.clipboard.clear()


def register():
    global _draw_handle
    for cls in classes:
        bpy.utils.register_class(cls)
    register_keys()
    _draw_handle = bpy.types.SpaceInfo.draw_handler_add(canvas.draw, (), 'WINDOW', 'POST_PIXEL')
    global _native_playback
    _native_playback = getattr(space_dopesheet, 'playback_controls', None)
    if _native_playback:
        space_dopesheet.playback_controls = timeline_playback
    else:
        bpy.types.DOPESHEET_HT_header.append(timeline_button)
    bpy.types.INFO_HT_header.prepend(header)
    bpy.types.INFO_HT_header.append(tangent_buttons)
    bpy.app.handlers.load_pre.append(reset_views)
    bpy.app.handlers.load_post.append(load_layout)
    bpy.app.handlers.save_pre.append(save_layout)
    bpy.app.handlers.depsgraph_update_post.append(redraw_views)
    bpy.app.handlers.frame_change_post.append(redraw_views)


def unregister():
    for km, item in _bindings:
        km.keymap_items.remove(item)
    _bindings.clear()
    reset_views(None)
    save_layout(None)
    bpy.app.handlers.load_pre.remove(reset_views)
    bpy.app.handlers.load_post.remove(load_layout)
    bpy.app.handlers.save_pre.remove(save_layout)
    bpy.app.handlers.depsgraph_update_post.remove(redraw_views)
    bpy.app.handlers.frame_change_post.remove(redraw_views)
    if _native_playback:
        space_dopesheet.playback_controls = _native_playback
    else:
        bpy.types.DOPESHEET_HT_header.remove(timeline_button)
    bpy.types.INFO_HT_header.remove(header)
    bpy.types.INFO_HT_header.remove(tangent_buttons)
    bpy.types.SpaceInfo.draw_handler_remove(_draw_handle, 'WINDOW')
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

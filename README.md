# lk_GraphEditor

Animation graph editors for Houdini and Blender.

## Blender 4.5+

Install the Blender ZIP from [GitHub Releases](https://github.com/lee2key/lk_GraphEditor/releases) using **Edit → Preferences → Get Extensions → Install from Disk**. Select an object and open **F3 → Animation Graph Editor**, or use the curve icon in the Timeline header.

Edit one F-Curve at a time from the active Action slot, including object and bone channels. Double-click to add keys, box-select keys or handles, drag keys along time or value, and adjust Bézier handles with the mouse or R/S. Use 1/2/3 for Bézier, Linear, and Constant, Ctrl+C/Ctrl+V to paste at the playhead, and F to frame keys. Space plays or pauses, and Shift+Space returns to the start of the playback range. Animation is stored in the scene's Action.

See the [Blender guide](blender/README.md) for all shortcuts and build instructions.

## Houdini

### Install

1. Extract the Houdini ZIP from [GitHub Releases](https://github.com/lee2key/lk_GraphEditor/releases) to a permanent folder, for example `C:/tools/lk_GraphEditor`. The folder should directly contain `package`, `python_panels`, and `scripts`.
2. Copy `package/houdini_animation_editor.json` into your Houdini preferences `packages` directory (for example, `Documents/houdini22.0/packages`).
3. Edit the copied JSON so its `hpath` value points to the folder you extracted. Example:

   ```json
   { "hpath": "C:/tools/lk_GraphEditor" }
   ```

4. Restart Houdini. In any pane's type menu, choose **Animation Graph Editor**.

## Current functionality

- Follows the selected node and lists its float and integer parameters.
- Displays that parameter's keyframe curve and a draggable current-frame playhead. MMB pans time; the mouse wheel zooms time horizontally.
- Curve extensions before the first and after the last key are dimmed and dashed. Frame grid lines and labels follow pan and zoom.
- Double-clicking the graph adds a key at the clicked frame/value.
- Click or box-select keys; hold `Shift` to add to the selection. Press `Ctrl+A` to select all, or `Delete`/`X` to remove selected keys.
- Drag a key to move the selected keys. The first movement locks the drag to either frames (horizontal) or values (vertical).
- A single selected key shows draggable weighted Bezier tangent handles; handle angle changes slope and length changes tension.
- Hold `Shift` during a tangent drag to lock its rotation to the original angle at drag start and change only length. This also applies when pressing Shift mid-drag; releasing Shift restores free rotation.
- Press `R` to rotate selected tangent handles with the mouse, or `S` to scale their lengths. LMB confirms; `Esc` cancels. If only the key is selected, its visible handles are targeted. These shortcuts work only in this editor and preserve existing tangent ties.
- New keys have tied slope and tension. Handle drags preserve native tie states. `1` ties selected tangents; `2` unties them. Existing scene keys retain their saved tie states until edited.
- Press `F` to fit the full curve on both axes, or focus selected keys when a selection exists.
- The Bezier, Linear, and Constant buttons in the header show the selected tangent mode. Editor-only shortcuts: `1` sets automatic Bezier with tied tangents, `2` makes incoming/outgoing segments linear with untied tangents, and `3` sets the outgoing segment to Constant (hold).
- Click or box-select a visible tangent handle on a highlighted key to target that side with `1`/`2`/`3`. Shift adds handles. Side-only Automatic/Linear unties the key to preserve its opposite tangent; Linear sets the selected tangent's slope toward its neighbor. Constant holds the adjacent segment. Selecting a key again returns to whole-key shortcuts.

## Intentional starter limits

Multiple channels and undo grouping for drag gestures are left for the next iteration.

The panel uses Houdini's bundled PySide6 and HOM (`hou`) APIs, so it has no third-party dependencies.

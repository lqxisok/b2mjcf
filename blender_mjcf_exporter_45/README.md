# Blender 4.5 MJCF Exporter

This add-on is a Blender 4.5 rewrite of `ref_codes/blender-mjcf-main.zip`.
It exports a portable MJCF bundle without changing the source Blender scene.

## Install

1. In Blender 4.5, open `Edit > Preferences > Add-ons > Install...`.
2. Select `tools/blender_mjcf_exporter_45_addon.zip`. The archive contains
   `blender_mjcf_exporter_45/` as its top-level add-on package.
3. Enable **Import-Export: MJCF Exporter 4.5**.

The exporter is available in `File > Export > MuJoCo MJCF (.xml)` and in the
3D View sidebar under the **MJCF** tab.

Export runs as a cancellable modal task. Blender's data API and OBJ/STL
operators must stay on Blender's main thread; moving them to a Python worker
thread is unsafe and can crash Blender. Instead, the add-on processes one
material or scene object per timer tick, updates Blender's progress bar and
status bar, and keeps the UI responsive. Press `Esc` to cancel.
The final XML is written atomically after all assets are exported; the
completion message reports the exact `.xml` path that can be opened directly
with MuJoCo.

## Export workflow

1. Select the station collection or the objects to export.
2. Enable **Selection Only**. A selected parent exports its complete child
   hierarchy; selecting a leaf exports that leaf subtree using its world pose.
3. Keep **OBJ** for textured scenes. The add-on copies Base Color images from
   Principled BSDF nodes into `textures/` and writes MJCF materials.
4. In the **Collision Geometry** box, choose **Convex Hull**, **Exact Mesh**,
   **Bounding Box**, or **None**. The setting is available both in the MJCF
   sidebar and in the export file dialog. Collision is emitted separately
   from the visual geom for every mesh object.
5. Enable **Add Robot**, keep **Robot Source** set to **Built-in AS2**, and set
   the robot position, for example `0 0 0.33`. The AS2 XML and all 17 STL
   meshes are packaged inside the add-on, so this mode does not open a second
   file selector and does not depend on the project checkout. Select
   **External MJCF** only when adding another robot from disk.
   The field accepts any standalone MJCF with a `worldbody` containing a
   `base_link` body, so another robot can be tested by selecting its XML and
   changing the prefix.

The output directory contains the XML plus `meshes/`, `collision_meshes/`,
`textures/`, and (when selected) `robot_meshes/`. Verify it with:

```bash
python -m mujoco.viewer --mjcf=/path/to/exported_scene.xml
```

The selected robot XML is merged into the root model. Its asset names and
default classes are prefixed, and mesh/texture files are copied into
`robot_meshes/`, so the exported scene does not depend on the original
repository paths. Robot body, joint, actuator, and sensor names are preserved
for existing controllers; use one robot per export until explicit body-name
namespacing is added.

## Collision guidance

Convex hulls are generated per Blender mesh object. Avoid putting an entire
station into one disconnected Blender object; split floors, walls, columns,
and props into separate objects so each receives an appropriately sized
collision hull. Imported zero-thickness floors and decals are automatically
given a tiny thickness in the collision copy, while the visual mesh remains
unchanged. Use **Exact Mesh** only when the triangle count is suitable for
MuJoCo contact simulation.

Scene mesh assets are emitted with `inertia="shell"`, the valid MuJoCo MJCF
form for shell inertia. `shellinertia="true"` is a USD-side property and is
not a valid MuJoCo 3.11 XML attribute.

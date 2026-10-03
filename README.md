# Blender 4.5 MJCF Exporter

This repository contains a Blender 4.5 add-on for exporting selected Blender
scene objects to a portable MuJoCo MJCF bundle. It was created for station
scenes and AS2 testing, while keeping external MJCF support for other robots.

## Features

- Exports selected objects and their child hierarchy.
- Copies OBJ/STL meshes and Base Color textures into the output bundle.
- Generates separate visual and collision geometry.
- Supports convex hull, exact mesh, bounding box, and no-collision modes.
- Runs as a cancellable modal task with Blender progress feedback.
- Includes AS2 XML and all 17 STL meshes inside the add-on package.
- Supports external robot MJCF files through the `External MJCF` source mode.

## Install

Build the installable archive:

```bash
python tools/build_addon_zip.py
```

Then install `dist/blender_mjcf_exporter_45_addon.zip` in Blender 4.5 with
`Edit > Preferences > Add-ons > Install...` and enable **MJCF Exporter 4.5**.

The exporter appears in `File > Export > MuJoCo MJCF (.xml)` and in the 3D
View sidebar under **MJCF**.

## AS2 export

Enable **Add Robot** and keep **Robot Source** set to **Built-in AS2**. The
exporter uses the packaged model and does not open a second file selector.
After export, load the generated XML directly:

```bash
python -m mujoco.viewer --mjcf=/path/to/exported_scene.xml
```

See `blender_mjcf_exporter_45/README.md` for collision and scene-authoring
guidance.

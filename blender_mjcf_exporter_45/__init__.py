"""Blender 4.5 MJCF exporter.

Install this directory as a Blender add-on.  The exporter intentionally keeps
all scene changes temporary: objects, selections, materials and transforms are
restored before the operator returns.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Iterable
import xml.etree.ElementTree as ET

import bpy
import bmesh
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup
from bpy_extras.io_utils import ExportHelper
from mathutils import Matrix, Quaternion, Vector


bl_info = {
    "name": "MJCF Exporter 4.5",
    "author": "AS2 MuJoCo project",
    "version": (1, 1, 0),
    "blender": (4, 5, 0),
    "location": "File > Export > MuJoCo MJCF (.xml)",
    "description": "Export selected Blender objects and optional robot assets as a textured MJCF scene",
    "category": "Import-Export",
}


_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
_SUPPORTED_GEOM_ATTRIBUTES = {
    "contype", "conaffinity", "condim", "priority", "solref", "solimp",
    "friction", "margin", "gap", "group", "density", "rgba",
}


def _safe_name(value: str, fallback: str = "object") -> str:
    result = _SAFE_NAME.sub("_", value).strip("._")
    return result or fallback


def _fmt(values: Iterable[float]) -> str:
    return " ".join(f"{float(value):.9g}" for value in values)


def _copy_xml(element: ET.Element) -> ET.Element:
    return ET.fromstring(ET.tostring(element, encoding="unicode"))


def _relative_file(path: Path, root: Path) -> str:
    return Path(os.path.relpath(path, root)).as_posix()


def _find_base_color_image(material: bpy.types.Material):
    if not material.use_nodes or material.node_tree is None:
        return None
    principled = next(
        (node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"),
        None,
    )
    if principled is None:
        return None
    socket = principled.inputs.get("Base Color")
    if socket is None or not socket.is_linked:
        return None
    link = socket.links[0]
    return getattr(link.from_node, "image", None)


def _material_rgba(material: bpy.types.Material) -> tuple[float, float, float, float]:
    if material.use_nodes and material.node_tree:
        principled = next(
            (node for node in material.node_tree.nodes if node.type == "BSDF_PRINCIPLED"),
            None,
        )
        if principled is not None:
            socket = principled.inputs.get("Base Color")
            if socket is not None and not socket.is_linked:
                color = socket.default_value
                return tuple(float(value) for value in color[:4])
    return tuple(float(value) for value in material.diffuse_color[:4])


class _Bundle:
    def __init__(self, output_xml: Path):
        self.output_xml = output_xml
        self.root_dir = output_xml.parent
        self.mesh_dir = self.root_dir / "meshes"
        self.collision_dir = self.root_dir / "collision_meshes"
        self.texture_dir = self.root_dir / "textures"
        self.mesh_dir.mkdir(parents=True, exist_ok=True)
        self.collision_dir.mkdir(parents=True, exist_ok=True)
        self.texture_dir.mkdir(parents=True, exist_ok=True)
        self.mesh_assets: dict[str, str] = {}
        self.material_assets: dict[str, str] = {}
        self.texture_assets: dict[str, str] = {}

    def add_material(self, material: bpy.types.Material) -> str | None:
        if material is None:
            return None
        key = material.as_pointer().__str__()
        if key in self.material_assets:
            return self.material_assets[key]
        material_name = _safe_name(material.name, "material")
        # Blender permits duplicate names; MJCF does not.  Use a stable suffix
        # only when another material has already claimed this name.
        used = set(self.material_assets.values())
        base = material_name
        suffix = 2
        while material_name in used:
            material_name = f"{base}_{suffix}"
            suffix += 1
        image = _find_base_color_image(material)
        texture_name = None
        if image is not None:
            texture_name = self.add_texture(image, material_name)
        self.material_assets[key] = material_name
        return material_name

    def add_texture(self, image, hint: str) -> str:
        key = image.as_pointer().__str__()
        if key in self.texture_assets:
            return self.texture_assets[key]
        source = bpy.path.abspath(image.filepath) if image.filepath else ""
        extension = Path(source).suffix.lower() or ".png"
        filename = _safe_name(Path(source).stem if source else hint, "texture") + extension
        destination = self.texture_dir / filename
        index = 2
        while destination.exists() and key not in self.texture_assets:
            filename = f"{_safe_name(Path(source).stem if source else hint)}_{index}{extension}"
            destination = self.texture_dir / filename
            index += 1
        if source and Path(source).is_file():
            shutil.copy2(source, destination)
        else:
            # Packed/generated Blender images have no useful filesystem path.
            # save_render works for generated images and preserves the selected
            # image's pixels without unpacking the user's .blend file.
            image.save_render(str(destination))
        texture_name = _safe_name(Path(filename).stem, "texture")
        self.texture_assets[key] = texture_name
        return texture_name

    def texture_file(self, texture_name: str) -> str:
        for image_file in self.texture_dir.iterdir():
            if _safe_name(image_file.stem, "texture") == texture_name:
                # The MJCF compiler uses texturedir="."; keep this path
                # relative to the XML bundle root.
                return _relative_file(image_file, self.root_dir)
        raise FileNotFoundError(f"Texture was not exported: {texture_name}")


def _set_temp_export_state(obj, mesh, scale: Vector):
    """Create a temporary object whose mesh is in body-local coordinates."""
    temp = obj.copy()
    temp.data = mesh.copy()
    temp.animation_data_clear()
    temp.matrix_world = Matrix.Identity(4)
    temp.data.transform(Matrix.Diagonal((scale.x, scale.y, scale.z, 1.0)))
    collection = bpy.data.collections.new("__mjcf_export_tmp__")
    bpy.context.scene.collection.children.link(collection)
    collection.objects.link(temp)
    return temp, collection


def _delete_temp_object(temp, collection) -> None:
    bpy.data.objects.remove(temp, do_unlink=True)
    bpy.data.collections.remove(collection)


def _export_object_mesh(obj, destination: Path, collision_mode: str, scale: Vector) -> None:
    """Export one object in its body-local frame using Blender 4.5 operators."""
    source_mesh = obj.data
    temp, collection = _set_temp_export_state(obj, source_mesh, scale)
    original_selected = list(bpy.context.selected_objects)
    original_active = bpy.context.view_layer.objects.active
    try:
        bpy.ops.object.select_all(action="DESELECT")
        temp.select_set(True)
        bpy.context.view_layer.objects.active = temp
        extension = destination.suffix.lower()
        if extension == ".stl":
            bpy.ops.wm.stl_export(
                filepath=str(destination),
                export_selected_objects=True,
                apply_modifiers=True,
            )
        else:
            bpy.ops.wm.obj_export(
                filepath=str(destination),
                export_selected_objects=True,
                apply_modifiers=True,
                export_uv=True,
                export_normals=True,
                export_materials=False,
                forward_axis="Y",
                up_axis="Z",
            )
    finally:
        bpy.ops.object.select_all(action="DESELECT")
        for selected in original_selected:
            if selected and selected.name in bpy.data.objects:
                selected.select_set(True)
        bpy.context.view_layer.objects.active = original_active
        _delete_temp_object(temp, collection)


def _convex_mesh(obj) -> bpy.types.Mesh:
    mesh = obj.data.copy()
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        result = bmesh.ops.convex_hull(bm, input=list(bm.verts))
        unused = result.get("geom_unused", [])
        unused_verts = [element for element in unused if isinstance(element, bmesh.types.BMVert)]
        if unused_verts:
            bmesh.ops.delete(bm, geom=unused_verts, context="VERTS")
        bm.to_mesh(mesh)
        mesh.update()
    finally:
        bm.free()
    return mesh


def _box_mesh(obj) -> bpy.types.Mesh:
    """Build an object-local bounding box mesh for cheap robust collision."""
    corners = [Vector(corner) for corner in obj.bound_box]
    vertices = [tuple(corner) for corner in corners]
    faces = [
        (0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1),
        (1, 5, 6, 2), (2, 6, 7, 3), (4, 0, 3, 7),
    ]
    mesh = bpy.data.meshes.new(f"{obj.name}_collision_box")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    return mesh


def _collision_mesh_for(obj, mode: str):
    if mode == "MESH":
        return obj.data.copy()
    if mode == "CONVEX_HULL":
        return _convex_mesh(obj)
    if mode == "BOX":
        return _box_mesh(obj)
    return None


def _object_transform(obj, parent_exported: bool) -> tuple[Vector, Quaternion, Vector]:
    if parent_exported:
        return obj.location.copy(), obj.rotation_quaternion.copy(), obj.scale.copy()
    location, rotation, scale = obj.matrix_world.decompose()
    return location, rotation, scale


def _custom_attrs(obj, allowed: set[str]) -> dict[str, str]:
    attrs = {}
    for key, value in obj.items():
        if not key.startswith("mjcf_"):
            continue
        name = key[5:]
        if name in allowed:
            attrs[name] = str(value)
    return attrs


class _Writer:
    def __init__(self, bundle: _Bundle, export_format: str, collision_mode: str, scale: float):
        self.bundle = bundle
        self.extension = ".obj" if export_format == "OBJ" else ".stl"
        self.collision_mode = collision_mode
        self.scene_scale = scale
        self.root = ET.Element("mujoco", {"model": "blender_scene"})
        ET.SubElement(self.root, "compiler", {
            "angle": "radian",
            "meshdir": ".",
            "texturedir": ".",
            "autolimits": "true",
        })
        self.default = ET.SubElement(self.root, "default")
        ET.SubElement(self.default, "default", {"class": "mjcf_visual"}).append(
            ET.Element("geom", {"type": "mesh", "contype": "0", "conaffinity": "0", "group": "2"})
        )
        ET.SubElement(self.default, "default", {"class": "mjcf_collision"}).append(
            ET.Element("geom", {
                "type": "mesh", "contype": "1", "conaffinity": "1", "group": "3",
                "condim": "3", "friction": "1 0.5 0.02",
            })
        )
        self.asset = ET.SubElement(self.root, "asset")
        self.worldbody = ET.SubElement(self.root, "worldbody")
        self._mesh_names: set[str] = set()
        self._texture_names: set[str] = set()
        self._object_names: set[str] = set()

    def _unique_object_name(self, name: str) -> str:
        base = _safe_name(name)
        result = base
        index = 2
        while result in self._object_names:
            result = f"{base}_{index}"
            index += 1
        self._object_names.add(result)
        return result

    def _add_mesh_asset(self, name: str, path: Path):
        if name in self._mesh_names:
            return
        self._mesh_names.add(name)
        ET.SubElement(self.asset, "mesh", {"name": name, "file": _relative_file(path, self.bundle.root_dir)})

    def _add_material_assets(self, objects: Iterable[bpy.types.Object]):
        for obj in objects:
            if obj.type != "MESH":
                continue
            for material in obj.data.materials:
                material_name = self.bundle.add_material(material)
                if material_name is None:
                    continue
                if any(element.get("name") == material_name for element in self.asset.findall("material")):
                    continue
                image = _find_base_color_image(material)
                rgba = _material_rgba(material)
                attributes = {"name": material_name, "rgba": _fmt(rgba)}
                if image is not None:
                    texture_name = self.bundle.texture_assets[image.as_pointer().__str__()]
                    attributes["texture"] = texture_name
                    if texture_name not in self._texture_names:
                        self._texture_names.add(texture_name)
                        ET.SubElement(self.asset, "texture", {
                            "name": texture_name,
                            "type": "2d",
                            "file": self.bundle.texture_file(texture_name),
                        })
                ET.SubElement(self.asset, "material", attributes)

    def _export_meshes(self, obj: bpy.types.Object, visual_name: str, collision_name: str | None,
                       scale: Vector):
        visual_path = self.bundle.mesh_dir / f"{visual_name}{self.extension}"
        _export_object_mesh(obj, visual_path, self.collision_mode, scale)
        self._add_mesh_asset(visual_name, visual_path)
        if collision_name is None:
            return
        collision_mesh = _collision_mesh_for(obj, self.collision_mode)
        if collision_mesh is None:
            return
        collision_path = self.bundle.collision_dir / f"{collision_name}{self.extension}"
        temp = obj.copy()
        temp.data = collision_mesh
        try:
            _export_object_mesh(temp, collision_path, self.collision_mode, scale)
        finally:
            # ``temp`` is only a data carrier for the exporter.  It is not linked
            # to a collection, so explicitly remove it as well as the generated
            # collision mesh to avoid leaking datablocks on repeated exports.
            bpy.data.objects.remove(temp, do_unlink=True)
            bpy.data.meshes.remove(collision_mesh)
        self._add_mesh_asset(collision_name, collision_path)

    def write_object(
        self,
        obj: bpy.types.Object,
        parent: ET.Element,
        exported: set[str],
        parent_exported: bool,
        recurse: bool = True,
    ):
        if obj.name in exported:
            return None
        exported.add(obj.name)
        object_name = self._unique_object_name(obj.name)
        position, rotation, scale = _object_transform(obj, parent_exported)
        position = position * self.scene_scale
        scaled = Vector((scale.x * self.scene_scale, scale.y * self.scene_scale, scale.z * self.scene_scale))
        body = ET.SubElement(parent, "body", {
            "name": object_name,
            "pos": _fmt(position),
            "quat": _fmt((rotation.w, rotation.x, rotation.y, rotation.z)),
        })
        if obj.type == "MESH":
            mesh_name = f"scene_{object_name}"
            collision_name = None if self.collision_mode == "NONE" else f"collision_{object_name}"
            self._export_meshes(obj, mesh_name, collision_name, scaled)
            visual = ET.SubElement(body, "geom", {
                "name": f"{object_name}_visual",
                "class": "mjcf_visual",
                "mesh": mesh_name,
            })
            material = next((mat for mat in obj.data.materials if mat is not None), None)
            if material is not None:
                visual.set("material", self.bundle.material_assets[material.as_pointer().__str__()])
            for key, value in _custom_attrs(obj, _SUPPORTED_GEOM_ATTRIBUTES).items():
                visual.set(key, value)
            if collision_name is not None:
                collision = ET.SubElement(body, "geom", {
                    "name": f"{object_name}_collision",
                    "class": "mjcf_collision",
                    "mesh": collision_name,
                })
                for key, value in _custom_attrs(obj, _SUPPORTED_GEOM_ATTRIBUTES).items():
                    collision.set(key, value)
        if recurse:
            for child in obj.children:
                if child.name in exported:
                    continue
                self.write_object(child, body, exported, True)
        return body

    def write_scene(self, objects: list[bpy.types.Object]):
        self._add_material_assets(objects)
        exported: set[str] = set()
        selected = {obj.name for obj in objects}
        for obj in objects:
            if obj.parent is None or obj.parent.name not in selected:
                self.write_object(obj, self.worldbody, exported, False)


def _scene_export_objects(context, only_selected: bool) -> list[bpy.types.Object]:
    objects = list(context.selected_objects) if only_selected else list(context.scene.objects)
    if only_selected:
        expanded = {obj.name: obj for obj in objects}
        for root in list(objects):
            for descendant in root.children_recursive:
                expanded.setdefault(descendant.name, descendant)
        objects = list(expanded.values())
    return [obj for obj in objects if obj.type in {"MESH", "EMPTY"}]


def _scene_export_roots(objects: list[bpy.types.Object]) -> list[bpy.types.Object]:
    selected = {obj.name for obj in objects}
    return [obj for obj in objects if obj.parent is None or obj.parent.name not in selected]


def _is_exportable_object(obj: bpy.types.Object) -> bool:
    return obj.type in {"MESH", "EMPTY"}


def _resolve_robot_path(raw_path: str, asset_prefix: str = "as2") -> Path:
    """Resolve a robot XML from Blender, the .blend file, or this project.

    Blender stores file-browser paths as either absolute paths or ``//`` paths
    relative to the current .blend file.  An empty field is useful for the
    built-in AS2 test model, so it falls back to the repository's reference
    model when this add-on is used from this checkout.
    """
    raw = (raw_path or "").strip()
    candidates: list[Path] = []
    if raw:
        raw_candidates = [Path(bpy.path.abspath(raw)).expanduser(), Path(raw).expanduser()]
        for candidate in raw_candidates:
            candidates.append(candidate)
            if candidate.suffix.lower() != ".xml":
                candidates.append(candidate / f"{_safe_name(asset_prefix, 'robot')}.xml")
                candidates.append(candidate / "as2.xml")
        blend_file = getattr(getattr(bpy, "data", None), "filepath", "")
        if blend_file:
            candidates.append(Path(blend_file).expanduser().parent / raw)
    addon_root = Path(__file__).resolve().parents[2]
    if _safe_name(asset_prefix, "as2").lower() == "as2":
        search_roots = [addon_root, Path.cwd()]
        blend_file = getattr(getattr(bpy, "data", None), "filepath", "")
        if blend_file:
            search_roots.append(Path(blend_file).expanduser().parent)
        for root in search_roots:
            for parent in (root, *root.parents):
                candidates.append(parent / "ref_codes" / "as2_description" / "as2.xml")

    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file():
            return candidate
    checked = ", ".join(str(path.resolve()) for path in candidates) or "<no path provided>"
    raise FileNotFoundError(f"Robot MJCF does not exist. Checked: {checked}")


def _bundled_as2_path() -> Path:
    path = Path(__file__).resolve().parent / "assets" / "as2" / "as2.xml"
    if not path.is_file():
        raise FileNotFoundError(
            f"Bundled AS2 model is missing: {path}. Reinstall the complete exporter zip."
        )
    return path


def _namespace_robot_defaults(robot_root: ET.Element, prefix: str) -> dict[str, str]:
    mapping = {}
    defaults = robot_root.find("default")
    if defaults is None:
        return mapping
    for element in defaults.iter("default"):
        old = element.get("class")
        if old:
            new = f"{prefix}_{old}"
            mapping[old] = new
            element.set("class", new)
    for element in robot_root.iter():
        old = element.get("class")
        if old in mapping:
            element.set("class", mapping[old])
    return mapping


def _merge_robot(
    writer: _Writer,
    robot_path: Path,
    position: tuple[float, float, float],
    asset_prefix: str = "as2",
):
    """Merge one robot MJCF while making its external assets self-contained.

    The body/joint names are intentionally preserved because controllers and
    policies commonly address AS2 joints by their established names.  Asset
    and default-class names are prefixed to avoid clashes with scene assets.
    A future multi-robot exporter can namespace body names independently.
    """
    if not robot_path.is_file():
        raise FileNotFoundError(f"Robot MJCF does not exist: {robot_path}")
    robot_root = ET.fromstring(robot_path.read_text(encoding="utf-8"))
    asset_prefix = _safe_name(asset_prefix, "robot")
    _namespace_robot_defaults(robot_root, asset_prefix)
    robot_asset = robot_root.find("asset")
    robot_mesh_dir = robot_path.parent
    robot_texture_dir = robot_path.parent
    compiler = robot_root.find("compiler")
    if compiler is not None and compiler.get("meshdir"):
        robot_mesh_dir = robot_mesh_dir / compiler.get("meshdir")
    if compiler is not None and compiler.get("texturedir"):
        robot_texture_dir = robot_path.parent / compiler.get("texturedir")
    target_dir = writer.bundle.root_dir / "robot_meshes"
    target_dir.mkdir(parents=True, exist_ok=True)
    asset_renames: dict[str, str] = {}
    if robot_asset is not None:
        for element in robot_asset:
            item = _copy_xml(element)
            old_name = item.get("name")
            if old_name:
                new_name = f"{asset_prefix}_{_safe_name(old_name)}"
                asset_renames[old_name] = new_name
                item.set("name", new_name)
            file_name = item.get("file")
            if file_name:
                source_root = robot_texture_dir if item.tag == "texture" else robot_mesh_dir
                source = source_root / file_name
                if not source.is_file():
                    raise FileNotFoundError(f"Robot asset does not exist: {source}")
                destination = target_dir / source.name
                suffix = destination.suffix
                stem = destination.stem
                index = 2
                while destination.exists() and destination.resolve() != source.resolve():
                    destination = target_dir / f"{stem}_{index}{suffix}"
                    index += 1
                shutil.copy2(source, destination)
                item.set("file", _relative_file(destination, writer.bundle.root_dir))
            writer.asset.append(item)
    robot_body_root = robot_root.find("worldbody")
    if robot_body_root is None:
        raise ValueError("Robot MJCF has no worldbody")
    body = next((item for item in robot_body_root.findall("body") if item.get("name") == "base_link"), None)
    if body is None:
        raise ValueError("Robot MJCF has no base_link body")
    body = _copy_xml(body)
    body.set("pos", _fmt(position))
    for element in body.iter():
        for attribute in ("mesh", "material", "texture", "hfield", "skin"):
            value = element.get(attribute)
            if value in asset_renames:
                element.set(attribute, asset_renames[value])
    writer.worldbody.append(body)
    for section in ("default", "actuator", "sensor"):
        source = robot_root.find(section)
        if source is None:
            continue
        if section == "default":
            for item in source:
                writer.default.append(_copy_xml(item))
        else:
            target = writer.root.find(section)
            if target is None:
                target = ET.SubElement(writer.root, section)
            for item in source:
                copied = _copy_xml(item)
                for attribute in ("mesh", "material", "texture", "hfield", "skin"):
                    value = copied.get(attribute)
                    if value in asset_renames:
                        copied.set(attribute, asset_renames[value])
                target.append(copied)


class MJCFExportSettings(PropertyGroup):
    only_selected: BoolProperty(name="Selection Only", default=True)
    export_format: EnumProperty(
        name="Mesh Format", items=[("OBJ", "OBJ", "Export textured OBJ meshes"), ("STL", "STL", "Export STL meshes")],
        default="OBJ",
    )
    collision_mode: EnumProperty(
        name="Collision Geometry",
        items=[
            ("CONVEX_HULL", "Convex Hull Per Object", "Fast robust collision per child object"),
            ("MESH", "Exact Mesh", "Use the exported child mesh for collision"),
            ("BOX", "Bounding Box", "Cheap box collision per child object"),
            ("NONE", "No Scene Collision", "Export visual geometry only"),
        ],
        default="CONVEX_HULL",
    )
    scene_scale: FloatProperty(name="Scene Scale", default=1.0, min=0.0001)
    add_robot: BoolProperty(name="Add Robot", default=False)
    robot_source: EnumProperty(
        name="Robot Source",
        items=[
            ("BUNDLED_AS2", "Built-in AS2", "Use the AS2 model packaged with this add-on"),
            ("EXTERNAL", "External MJCF", "Use a robot MJCF selected from disk"),
        ],
        default="BUNDLED_AS2",
    )
    robot_mjcf: StringProperty(
        name="Robot MJCF", subtype="FILE_PATH", default="",
        description="Any standalone robot MJCF, for example ref_codes/as2_description/as2.xml",
    )
    robot_prefix: StringProperty(
        name="Robot Asset Prefix", default="as2",
        description="Prefix for robot mesh, texture, and default-class names",
    )
    robot_position: FloatVectorProperty(name="Robot Position", size=3, default=(0.0, 0.0, 0.33))


class EXPORT_OT_mjcf_45(Operator, ExportHelper):
    bl_idname = "export_scene.mjcf_45"
    bl_label = "Export MJCF 4.5"
    bl_options = {"PRESET", "UNDO"}
    filename_ext = ".xml"
    filter_glob: StringProperty(default="*.xml", options={"HIDDEN"})

    def invoke(self, context, event):
        settings = context.scene.mjcf_export_settings
        self.only_selected = settings.only_selected
        self.export_format = settings.export_format
        self.collision_mode = settings.collision_mode
        self.scene_scale = settings.scene_scale
        self.add_robot = settings.add_robot
        self.robot_source = settings.robot_source
        self.robot_mjcf = settings.robot_mjcf
        self.robot_prefix = settings.robot_prefix
        self.robot_position = settings.robot_position
        return super().invoke(context, event)

    only_selected: BoolProperty(default=True)
    export_format: EnumProperty(items=[("OBJ", "OBJ", ""), ("STL", "STL", "")], default="OBJ")
    collision_mode: EnumProperty(
        name="Collision Geometry",
        description="Choose the collision representation generated for each scene mesh",
        items=[
            ("CONVEX_HULL", "Convex Hull", "One robust convex collision hull per object"),
            ("MESH", "Exact Mesh", "Use the source mesh for collision"),
            ("BOX", "Bounding Box", "Use a cheap box collision"),
            ("NONE", "None", "Export visual geometry only"),
        ],
        default="CONVEX_HULL",
    )
    scene_scale: FloatProperty(default=1.0, min=0.0001)
    add_robot: BoolProperty(default=False)
    robot_source: EnumProperty(
        name="Robot Source",
        items=[
            ("BUNDLED_AS2", "Built-in AS2", "Use the AS2 model packaged with this add-on"),
            ("EXTERNAL", "External MJCF", "Use a robot MJCF selected from disk"),
        ],
        default="BUNDLED_AS2",
    )
    robot_mjcf: StringProperty(subtype="FILE_PATH", default="")
    robot_prefix: StringProperty(default="as2")
    robot_position: FloatVectorProperty(size=3, default=(0.0, 0.0, 0.33))

    _timer = None
    _writer = None
    _bundle = None
    _export_objects = None
    _export_queue = None
    _exported = None
    _material_index = 0
    _done = 0
    _total = 1
    _phase = ""
    _xml_temp_path = None

    def _set_status(self, context, message: str):
        workspace = getattr(context, "workspace", None)
        if workspace is not None and hasattr(workspace, "status_text_set"):
            workspace.status_text_set(message or "")

    def _finish_timer(self, context):
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        context.window_manager.progress_end()
        self._set_status(context, "")

    def _fail(self, context, message: str):
        self._finish_timer(context)
        if self._xml_temp_path is not None:
            try:
                self._xml_temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._xml_temp_path = None
        self.report({"ERROR"}, message)
        return {"CANCELLED"}

    def _finish(self, context):
        output_xml = self._bundle.output_xml
        ET.indent(self._writer.root, space="  ")
        xml_text = ET.tostring(
            self._writer.root, encoding="unicode", xml_declaration=True
        )
        # Write to a temporary sibling first.  MuJoCo will either see the
        # complete previous export or the complete new export, never a partial
        # XML file if Blender is interrupted during the final write.
        temp_path = output_xml.with_name(f".{output_xml.name}.tmp")
        temp_path.write_text(
            xml_text,
            encoding="utf-8",
        )
        temp_path.replace(output_xml)
        self._xml_temp_path = None
        if not output_xml.is_file() or output_xml.stat().st_size == 0:
            raise OSError(f"MJCF XML was not created: {output_xml}")
        self._done = self._total
        context.window_manager.progress_update(self._total)
        self._finish_timer(context)
        self.report({"INFO"}, f"Exported MJCF bundle: {output_xml}")
        return {"FINISHED"}

    def _update_progress(self, context, label: str):
        self._done += 1
        context.window_manager.progress_update(self._done)
        self._set_status(context, f"MJCF export: {label} ({self._done}/{self._total})")

    def modal(self, context, event):
        if event.type in {"ESC", "RIGHTMOUSE"}:
            return self._fail(context, "MJCF export cancelled")
        if event.type != "TIMER":
            return {"RUNNING_MODAL"}

        try:
            if self._phase == "materials":
                if self._material_index < len(self._export_objects):
                    obj = self._export_objects[self._material_index]
                    self._writer._add_material_assets([obj])
                    self._material_index += 1
                    self._update_progress(context, f"materials: {obj.name}")
                    return {"RUNNING_MODAL"}
                self._phase = "scene"
                return {"RUNNING_MODAL"}

            if self._phase == "scene":
                if self._export_queue:
                    obj, parent, parent_exported = self._export_queue.pop(0)
                    body = self._writer.write_object(
                        obj, parent, self._exported, parent_exported, recurse=False
                    )
                    if body is not None:
                        for child in obj.children:
                            if _is_exportable_object(child) and child.name not in self._exported:
                                self._export_queue.append((child, body, True))
                    self._update_progress(context, f"scene: {obj.name}")
                    return {"RUNNING_MODAL"}
                self._phase = "robot" if self.add_robot else "write"
                return {"RUNNING_MODAL"}

            if self._phase == "robot":
                robot_path = (
                    _bundled_as2_path()
                    if self.robot_source == "BUNDLED_AS2"
                    else _resolve_robot_path(self.robot_mjcf, self.robot_prefix)
                )
                _merge_robot(
                    self._writer,
                    robot_path,
                    tuple(self.robot_position),
                    self.robot_prefix,
                )
                self._update_progress(context, "robot assets")
                self._phase = "write"
                return {"RUNNING_MODAL"}

            if self._phase == "write":
                return self._finish(context)
        except Exception as exc:
            return self._fail(context, str(exc))
        return {"RUNNING_MODAL"}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "only_selected")
        layout.prop(self, "export_format")
        box = layout.box()
        box.label(text="Collision Geometry")
        box.prop(self, "collision_mode", expand=True)
        layout.prop(self, "scene_scale")
        layout.separator()
        layout.prop(self, "add_robot")
        if self.add_robot:
            layout.prop(self, "robot_source")
            if self.robot_source == "EXTERNAL":
                layout.prop(self, "robot_mjcf")
            layout.prop(self, "robot_prefix")
            layout.prop(self, "robot_position")

    def execute(self, context):
        objects = _scene_export_objects(context, self.only_selected)
        if not objects:
            self.report({"ERROR"}, "No mesh or empty objects selected")
            return {"CANCELLED"}
        output_xml = Path(bpy.path.abspath(self.filepath)).resolve()
        if output_xml.suffix.lower() != ".xml":
            output_xml = output_xml.with_suffix(".xml")
        output_xml.parent.mkdir(parents=True, exist_ok=True)
        self._bundle = _Bundle(output_xml)
        self._writer = _Writer(self._bundle, self.export_format, self.collision_mode, self.scene_scale)
        roots = _scene_export_roots(objects)
        self._export_objects = objects
        self._export_queue = [(obj, self._writer.worldbody, False) for obj in roots]
        self._exported = set()
        self._material_index = 0
        self._done = 0
        self._xml_temp_path = output_xml.with_name(f".{output_xml.name}.tmp")
        # One timer tick handles one material/object (plus the final XML
        # write), keeping the Blender event loop responsive between steps.
        self._total = (2 * len(objects)) + (1 if self.add_robot else 0) + 1
        self._phase = "materials"
        try:
            context.window_manager.progress_begin(0, self._total)
            self._timer = context.window_manager.event_timer_add(0.01, window=context.window)
            context.window_manager.modal_handler_add(self)
            self._set_status(context, f"MJCF export: preparing (0/{self._total})")
        except Exception as exc:
            return self._fail(context, str(exc))
        return {"RUNNING_MODAL"}


class VIEW3D_PT_mjcf_exporter_45(Panel):
    bl_label = "MJCF Exporter 4.5"
    bl_idname = "VIEW3D_PT_mjcf_exporter_45"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MJCF"

    def draw(self, context):
        settings = context.scene.mjcf_export_settings
        layout = self.layout
        layout.prop(settings, "only_selected")
        layout.prop(settings, "export_format")
        box = layout.box()
        box.label(text="Collision Geometry")
        box.prop(settings, "collision_mode", expand=True)
        layout.prop(settings, "scene_scale")
        layout.separator()
        layout.prop(settings, "add_robot")
        if settings.add_robot:
            layout.prop(settings, "robot_source")
            if settings.robot_source == "EXTERNAL":
                layout.prop(settings, "robot_mjcf")
            layout.prop(settings, "robot_prefix")
            layout.prop(settings, "robot_position")
        operator = layout.operator(EXPORT_OT_mjcf_45.bl_idname, text="Export MJCF")
        operator.only_selected = settings.only_selected
        operator.export_format = settings.export_format
        operator.collision_mode = settings.collision_mode
        operator.scene_scale = settings.scene_scale
        operator.add_robot = settings.add_robot
        operator.robot_source = settings.robot_source
        operator.robot_mjcf = settings.robot_mjcf
        operator.robot_prefix = settings.robot_prefix
        operator.robot_position = settings.robot_position


def _menu_export(self, context):
    self.layout.operator(EXPORT_OT_mjcf_45.bl_idname, text="MuJoCo MJCF (.xml)")


_CLASSES = (MJCFExportSettings, EXPORT_OT_mjcf_45, VIEW3D_PT_mjcf_exporter_45)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.mjcf_export_settings = bpy.props.PointerProperty(type=MJCFExportSettings)
    bpy.types.TOPBAR_MT_file_export.append(_menu_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(_menu_export)
    del bpy.types.Scene.mjcf_export_settings
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()

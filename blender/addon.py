# This file was based on https://github.com/KhronosGroup/glTF-Blender-IO/blob/master/addons/io_scene_gltf2/__init__.py

import bpy
import os
from bpy.props import PointerProperty
from bpy.props import (
    StringProperty,
    CollectionProperty,
    IntProperty,
    BoolProperty,
)
from bpy.types import PropertyGroup
from bpy_extras.io_utils import ImportHelper
from bpy.types import Operator

from .importer.image_relink import YakuzaImageRelink, menu_func_yk_image_relink
from .materials import YakuzaPropertyGroup, YakuzaPropertyPanel, YakuzaTexturePropertyGroup, \
    MATERIAL_OT_yakuza_update_expected_layers
from .common import YakuzaHierarchyNodeData, OBJECT_PT_yakuza_hierarchy_node_data_panel, \
    BONE_PT_yakuza_hierarchy_node_data_panel, YakuzaFileRootData, OBJECT_PT_yakuza_file_root_data_panel
from .exporter.gmd_exporter import ExportSkinnedGMD, menu_func_export_skinned, menu_func_export_unskinned, \
    ExportUnskinnedGMD
from .importer.gmd_importers import ImportSkinnedGMD, menu_func_import_skinned, menu_func_import_unskinned, \
    ImportUnskinnedGMD, menu_func_import_animation_unskinned, menu_func_import_animation_skinned, \
    ImportAnimationSkinnedGMD, ImportAnimationUnskinnedGMD
from .importer.par_importer import ImportPARAsAssets, menu_func_import_par
from .importer.par_importer import YKPAR_OT_export_par_as_assetlib
from .importer.par_browser import (
    YKPAR_NodeItem,
    YKPAR_UL_nodes,
    YKPAR_ExpandedItem,
    YKPAR_OT_refresh,
    YKPAR_OT_toggle_node,
    YKPAR_OT_import_file,
    YKPAR_OT_place_file,
    YKPAR_OT_import_selected,
    YKPAR_OT_place_selected,
    YKPAR_OT_import_selected_multiple,
    YKPAR_OT_import_visible_all,
    YKPAR_OT_import_par_armature,
    YKPAR_OT_import_par_animation,
    YKPAR_OT_relink_preserved_tmp,
    YKPAR_OT_debug_dds_path,
    YKPAR_PT_browser,
)
from .importer import par_browser as par_browser_module


class YKPAR_PreferenceItem(PropertyGroup):
    """A single .par entry stored in addon preferences"""
    path: StringProperty(name="PAR Path", subtype='FILE_PATH')


class YKPAR_OT_add_par_file(Operator, ImportHelper):
    """Add one or more .par files to the addon preferences"""
    bl_idname = "yk_par_lib_tool.add_par_file"
    bl_label = "Add PAR File to Preferences"

    files: CollectionProperty(type=bpy.types.OperatorFileListElement)
    # ImportHelper will set .directory when invoked from the file selector
    directory: StringProperty(subtype='DIR_PATH')

    def execute(self, context):
        prefs = context.preferences.addons["yk_par_lib_tool"].preferences
        base = self.directory
        for f in self.files:
            fp = os.path.join(base, f.name)
            item = prefs.par_files.add()
            item.path = fp
        return {'FINISHED'}


class YKPAR_OT_remove_par_file(Operator):
    """Remove selected .par from addon preferences"""
    bl_idname = "yk_par_lib_tool.remove_par_file"
    bl_label = "Remove PAR File from Preferences"

    index: IntProperty()

    def execute(self, context):
        prefs = context.preferences.addons["yk_par_lib_tool"].preferences
        if 0 <= self.index < len(prefs.par_files):
            prefs.par_files.remove(self.index)
            prefs.par_index = min(max(0, self.index - 1), max(0, len(prefs.par_files) - 1))
        return {'FINISHED'}


class YKPAR_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = "yk_par_lib_tool"

    par_files: CollectionProperty(type=YKPAR_PreferenceItem)
    par_index: IntProperty(name="Active PAR Index", default=0)
    # Optional folder where extracted DDS/PNG files will be written.
    # If empty, a temporary directory will be created per-extraction (preserved by default).
    dds_extract_path: StringProperty(name="DDS Extract Path", subtype='DIR_PATH', default="")
    # Opt-in flag: allow adding the heavy bulk PAR importer to menus
    allow_bulk_par_import: BoolProperty(
        name="Allow bulk PAR import",
        description="If enabled, add the 'Import PAR' bulk importer to File > Import. Keep disabled to avoid accidental large imports.",
        default=False,
    )

    def draw(self, context):
        layout = self.layout
        row = layout.row()
        col = row.column()
        col.template_list("UI_UL_list", "yk_par_files", self, "par_files", self, "par_index")

        col = row.column(align=True)
        col.operator("yk_par_lib_tool.add_par_file", icon='ADD', text="Add...")
        op = col.operator("yk_par_lib_tool.remove_par_file", icon='REMOVE', text="Remove")
        op.index = self.par_index
        if self.par_files:
            layout.label(text=f"Selected: {self.par_files[self.par_index].path}")
        # DDS extraction folder preference
        layout.prop(self, 'dds_extract_path', text='DDS Extract Folder')

classes = (
    YKPAR_PreferenceItem,
    YKPAR_OT_add_par_file,
    YKPAR_OT_remove_par_file,
    YKPAR_AddonPreferences,
    ImportSkinnedGMD,
    ImportUnskinnedGMD,
    ImportAnimationSkinnedGMD,
    ImportAnimationUnskinnedGMD,
    ExportSkinnedGMD,
    ExportUnskinnedGMD,
    YakuzaImageRelink,
    ImportPARAsAssets,
    YKPAR_OT_export_par_as_assetlib,
    YKPAR_NodeItem,
    YKPAR_UL_nodes,
    YKPAR_ExpandedItem,
    YKPAR_OT_refresh,
    YKPAR_OT_toggle_node,
    YKPAR_OT_import_file,
    YKPAR_OT_place_file,
    YKPAR_OT_import_selected,
    YKPAR_OT_place_selected,
    YKPAR_OT_import_selected_multiple,
    YKPAR_OT_import_visible_all,
    YKPAR_OT_import_par_armature,
    YKPAR_OT_import_par_animation,
    YKPAR_OT_relink_preserved_tmp,
    YKPAR_OT_debug_dds_path,
    YKPAR_PT_browser,
    YakuzaPropertyGroup,
    YakuzaPropertyPanel,
    YakuzaTexturePropertyGroup,
    YakuzaHierarchyNodeData,
    OBJECT_PT_yakuza_hierarchy_node_data_panel,
    BONE_PT_yakuza_hierarchy_node_data_panel,
    YakuzaFileRootData,
    OBJECT_PT_yakuza_file_root_data_panel,
    MATERIAL_OT_yakuza_update_expected_layers,
    # PAR browser removed - exporter bypasses the browser UI
)

# Diagnostic tool
from .importer.par_importer import YKPAR_OT_diagnose
classes = classes + (YKPAR_OT_diagnose,)


def register():
    for c in classes:
        try:
            bpy.utils.register_class(c)
        except Exception as e:
            # Registering can fail if a class is already registered or if
            # dependencies are missing; report and continue so addon stays usable.
            print(f"Warning: failed to register class {getattr(c, '__name__', str(c))}: {e}")

    # Ensure PAR browser scene properties exist (helps the UI list display)
    try:
        par_browser_module._ensure_scene_properties()
    except Exception:
        pass

    # add to the export / import menu (wrap in try/except to be robust)
    try:
        bpy.types.TOPBAR_MT_file_export.append(menu_func_export_skinned)
    except Exception:
        pass
    try:
        bpy.types.TOPBAR_MT_file_export.append(menu_func_export_unskinned)
    except Exception:
        pass
    try:
        prefs = bpy.context.preferences.addons.get('yk_par_lib_tool')
        enabled = False
        if prefs and getattr(prefs, 'preferences', None):
            enabled = getattr(prefs.preferences, 'allow_bulk_par_import', False)
        if enabled:
            bpy.types.TOPBAR_MT_file_import.append(menu_func_import_par)
    except Exception:
        pass
    # add export operator to import menu for convenience (removed unnecessary labels)
    try:
        bpy.types.TOPBAR_MT_file_external_data.append(menu_func_yk_image_relink)
    except Exception:
        pass
    # Diagnose menu entry removed

    # Attach custom PointerProperty fields (wrap in try/except)
    try:
        bpy.types.Material.yakuza_data = PointerProperty(type=YakuzaPropertyGroup)
    except Exception:
        pass
    try:
        bpy.types.Image.yakuza_data = PointerProperty(type=YakuzaTexturePropertyGroup)
    except Exception:
        pass
    try:
        bpy.types.Object.yakuza_hierarchy_node_data = PointerProperty(type=YakuzaHierarchyNodeData)
    except Exception:
        pass
    try:
        bpy.types.Object.yakuza_file_root_data = PointerProperty(type=YakuzaFileRootData)
    except Exception:
        pass
    try:
        bpy.types.Bone.yakuza_hierarchy_node_data = PointerProperty(type=YakuzaHierarchyNodeData)
    except Exception:
        pass


def unregister():
    # Remove PointerProperties if present
    try:
        del bpy.types.Bone.yakuza_hierarchy_node_data
    except Exception:
        pass
    try:
        del bpy.types.Object.yakuza_file_root_data
    except Exception:
        pass
    try:
        del bpy.types.Object.yakuza_hierarchy_node_data
    except Exception:
        pass
    try:
        del bpy.types.Image.yakuza_data
    except Exception:
        pass
    try:
        del bpy.types.Material.yakuza_data
    except Exception:
        pass

    # Unregister classes in reverse order and ignore failures
    for c in reversed(classes):
        try:
            bpy.utils.unregister_class(c)
        except Exception as e:
            # If a class wasn't registered or already removed, ignore it
            print(f"Info: could not unregister class {getattr(c, '__name__', str(c))}: {e}")

    # remove from the export / import menu (wrap removals in try/except)
    try:
        bpy.types.TOPBAR_MT_file_external_data.remove(menu_func_yk_image_relink)
    except Exception:
        pass
    try:
        bpy.types.TOPBAR_MT_file_export.remove(menu_func_export_unskinned)
    except Exception:
        pass
    try:
        bpy.types.TOPBAR_MT_file_export.remove(menu_func_export_skinned)
    except Exception:
        pass
    try:
        bpy.types.TOPBAR_MT_file_import.remove(menu_func_import_par)
    except Exception:
        pass
    try:
        bpy.types.TOPBAR_MT_file_import.remove(menu_func_import_animation_unskinned)
    except Exception:
        pass
    try:
        bpy.types.TOPBAR_MT_file_import.remove(menu_func_import_animation_skinned)
    except Exception:
        pass
    try:
        bpy.types.TOPBAR_MT_file_import.remove(menu_func_import_unskinned)
    except Exception:
        pass
    try:
        bpy.types.TOPBAR_MT_file_import.remove(menu_func_import_skinned)
    except Exception:
        pass

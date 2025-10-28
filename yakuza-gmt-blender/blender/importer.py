from copy import deepcopy
from os.path import basename
from typing import Dict

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy.types import Action, Operator
from bpy_extras.io_utils import ImportHelper
from mathutils import Quaternion, Vector

from ..gmt_lib import *
from ..gmt_lib.gmt.gmt_reader import read_cmt, read_ifa
from ..gmt_lib.gmt.structure.cmt import *
from ..gmt_lib.gmt.structure.ifa import *
from ..gmt_lib.gmt.structure.enums.gmt_enum import OEDEFaceTarget
from .bone_props import GMTBlenderBoneProps, get_edit_bones_props
from .coordinate_converter import (convert_cmt_anm_to_blender,
                                   convert_gmt_curve_to_blender,
                                   pattern1_to_blender, pattern2_to_blender,
                                   transform_location_to_blender,
                                   transform_rotation_to_blender)
from .error import GMTError
from types import SimpleNamespace

# Import Blender 5.0 animation utilities for channelbag API
try:
    from bpy_extras import anim_utils
    HAS_ANIM_UTILS = True
    print("[yakuza_gmt] bpy_extras.anim_utils available - using Blender 5.0 channelbag API")
except ImportError:
    HAS_ANIM_UTILS = False
    print("[yakuza_gmt] WARNING: bpy_extras.anim_utils not available - legacy API mode")

# Check if we're actually in Blender 5.0+
BLENDER_VERSION = bpy.app.version
IS_BLENDER_5_0_PLUS = BLENDER_VERSION >= (5, 0, 0)
print(f"[yakuza_gmt] Blender version: {BLENDER_VERSION}, 5.0+ mode: {IS_BLENDER_5_0_PLUS}")


def _get_or_create_channelbag(action, armature_obj):
    """
    Get or create a channelbag for the given action and armature object.
    This is the Blender 5.0+ way to access fcurves and groups.
    
    Returns: channelbag object that has .fcurves and .groups properties
    """
    if not HAS_ANIM_UTILS or not IS_BLENDER_5_0_PLUS:
        # Fallback for Blender <5.0: action itself has fcurves/groups
        print(f"[yakuza_gmt] Using legacy action mode for {action.name}")
        return action
    
    try:
        # Ensure the armature has animation_data
        if not armature_obj.animation_data:
            armature_obj.animation_data_create()
            print(f"[yakuza_gmt] Created animation_data for {armature_obj.name}")
        
        # Create and configure action slot for Blender 5.0+
        if not hasattr(action, 'slots') or len(action.slots) == 0:
            # Try different approaches for slot creation
            try:
                # Method 1: Try with just id_type
                slot = action.slots.new(id_type='OBJECT')
                print(f"[yakuza_gmt] Created new action slot with id_type=OBJECT (method 1)")
            except Exception as e1:
                print(f"[yakuza_gmt] Method 1 failed: {e1}")
                try:
                    # Method 2: Try with name parameter
                    slot_name = f"{action.name}_slot"
                    slot = action.slots.new(name=slot_name, id_type='OBJECT')
                    print(f"[yakuza_gmt] Created new action slot with name and id_type (method 2)")
                except Exception as e2:
                    print(f"[yakuza_gmt] Method 2 failed: {e2}")
                    try:
                        # Method 3: Try with minimal parameters
                        slot = action.slots.new()
                        print(f"[yakuza_gmt] Created new action slot with no parameters (method 3)")
                    except Exception as e3:
                        print(f"[yakuza_gmt] Method 3 failed: {e3}")
                        raise Exception(f"All slot creation methods failed: {e1}, {e2}, {e3}")
            
            print(f"[yakuza_gmt] Slot properties: {[attr for attr in dir(slot) if not attr.startswith('_')]}")
        else:
            # Use the first existing slot
            slot = action.slots[0]
            print(f"[yakuza_gmt] Using existing slot for action {action.name}")
        
        # Assign the action to the armature's animation_data
        armature_obj.animation_data.action = action
        
        # Bind the slot to this armature object
        armature_obj.animation_data.action_slot = slot
        print(f"[yakuza_gmt] Bound slot to armature '{armature_obj.name}'")
        
        # Get or create channelbag for this slot
        try:
            channelbag = anim_utils.action_ensure_channelbag_for_slot(action, slot)
            print(f"[yakuza_gmt] Got channelbag from anim_utils for action '{action.name}'")
        except Exception as channelbag_error:
            print(f"[yakuza_gmt] anim_utils.action_ensure_channelbag_for_slot failed: {channelbag_error}")
            # Try alternative approach - get channelbag directly from slot
            if hasattr(slot, 'channelbag'):
                channelbag = slot.channelbag
                print(f"[yakuza_gmt] Using slot.channelbag directly")
            else:
                raise Exception(f"Cannot get channelbag: anim_utils failed and slot has no channelbag attribute")
        
        # Verify channelbag has required properties
        if not hasattr(channelbag, 'fcurves'):
            print(f"[yakuza_gmt] Channelbag properties: {[attr for attr in dir(channelbag) if not attr.startswith('_')]}")
            raise Exception(f"Channelbag does not have fcurves property. Available: {[attr for attr in dir(channelbag) if 'curve' in attr.lower()]}")
        if not hasattr(channelbag, 'groups'):
            print(f"[yakuza_gmt] WARNING: Channelbag does not have groups property")
            
        print(f"[yakuza_gmt] Successfully verified channelbag for action '{action.name}'")
        return channelbag
        
    except Exception as e:
        import traceback
        print(f"[yakuza_gmt] ERROR getting channelbag: {e}")
        print(f"[yakuza_gmt] Traceback: {traceback.format_exc()}")
        print(f"[yakuza_gmt] Action properties: {[attr for attr in dir(action) if 'fcurve' in attr.lower() or 'group' in attr.lower()]}")
        
        # In Blender 5.0, try an alternative approach
        if IS_BLENDER_5_0_PLUS:
            print(f"[yakuza_gmt] Trying alternative Blender 5.0 approach...")
            try:
                # Alternative: Try to get the first channelbag from the action directly
                if hasattr(action, 'channelbags') and len(action.channelbags) > 0:
                    channelbag = action.channelbags[0]
                    print(f"[yakuza_gmt] Using existing channelbag from action.channelbags[0]")
                    return channelbag
                
                # Alternative: Try to create slot and channelbag using basic API
                if hasattr(action, 'slots'):
                    # Create slot with proper id_type
                    if len(action.slots) == 0:
                        slot = action.slots.new(id_type='OBJECT')
                        print(f"[yakuza_gmt] Created alternative slot with id_type=OBJECT")
                    else:
                        slot = action.slots[0]
                        print(f"[yakuza_gmt] Using existing slot for alternative approach")
                    
                    # Try to get channelbag from slot
                    if hasattr(slot, 'channelbag') and slot.channelbag:
                        channelbag = slot.channelbag
                        print(f"[yakuza_gmt] Got channelbag from alternative slot approach")
                        return channelbag
                    
                print(f"[yakuza_gmt] No alternative methods available")
                raise GMTError(f"Failed to create channelbag for action {action.name} in Blender 5.0: {e}")
            except Exception as alt_error:
                print(f"[yakuza_gmt] Alternative approach also failed: {alt_error}")
                raise GMTError(f"All methods failed to create channelbag for action {action.name} in Blender 5.0: Original: {e}, Alternative: {alt_error}")
        else:
            print(f"[yakuza_gmt] Falling back to legacy action object")
            return action


def _ensure_fcurve(channelbag_or_action, data_path, index, group_name=None):
    """
    Ensure an fcurve exists for the given data_path and index.
    Works with both Blender 5.0 channelbags and legacy actions.
    
    Returns: fcurve object
    """
    try:
        print(f"[yakuza_gmt] _ensure_fcurve called with: {type(channelbag_or_action).__name__}, {data_path}[{index}], group={group_name}")
        print(f"[yakuza_gmt] Object attributes: {[attr for attr in dir(channelbag_or_action) if 'fcurve' in attr.lower()]}")
        
        # First, check what type of object we have
        obj_type = type(channelbag_or_action).__name__
        
        if obj_type == 'Action':
            # In Blender 5.0, Action objects don't have direct fcurves
            # We need to go through the animation system differently
            if IS_BLENDER_5_0_PLUS:
                print(f"[yakuza_gmt] ERROR: Received raw Action object in Blender 5.0, this should not happen!")
                print(f"[yakuza_gmt] Action properties: {[attr for attr in dir(channelbag_or_action) if not attr.startswith('_')]}")
                raise GMTError(f"Cannot create fcurves on raw Action object in Blender 5.0. Need channelbag instead of {obj_type}")
            else:
                print(f"[yakuza_gmt] Using legacy Action.fcurves API")
        
        # Check if this is a channelbag or has fcurves
        if not hasattr(channelbag_or_action, 'fcurves'):
            print(f"[yakuza_gmt] ERROR: Object {obj_type} has no fcurves attribute")
            raise GMTError(f"Object {obj_type} has no fcurves attribute. Available attributes: {[attr for attr in dir(channelbag_or_action) if not attr.startswith('_')]}")
        
        fcurves_obj = channelbag_or_action.fcurves
        print(f"[yakuza_gmt] FCurves object type: {type(fcurves_obj).__name__}")
        print(f"[yakuza_gmt] FCurves methods: {[attr for attr in dir(fcurves_obj) if not attr.startswith('_')]}")
        
        # Check if this is a Blender 5.0+ channelbag with the ensure method
        if hasattr(fcurves_obj, 'ensure'):
            # Blender 5.0+ channelbag API
            print(f"[yakuza_gmt] Using channelbag.fcurves.ensure: {data_path}[{index}]")
            fcurve = fcurves_obj.ensure(data_path, index=index, group_name=group_name)
            print(f"[yakuza_gmt] Successfully created fcurve via ensure: {type(fcurve).__name__}")
            return fcurve
        else:
            # Legacy API - manually handle groups
            print(f"[yakuza_gmt] Using legacy fcurves API: {data_path}[{index}]")
            
            # Try to find existing fcurve first
            if hasattr(fcurves_obj, 'find'):
                fcurve = fcurves_obj.find(data_path, index=index)
                if fcurve:
                    print(f"[yakuza_gmt] Found existing fcurve: {data_path}[{index}]")
                    return fcurve
            
            # Create new fcurve
            if not hasattr(fcurves_obj, 'new'):
                raise GMTError(f"FCurves object {type(fcurves_obj).__name__} has no 'new' method")
            
            if group_name and hasattr(channelbag_or_action, 'groups'):
                # Create or get the action group first
                group = None
                for g in channelbag_or_action.groups:
                    if g.name == group_name:
                        group = g
                        break
                if not group:
                    group = channelbag_or_action.groups.new(group_name)
                    print(f"[yakuza_gmt] Created action group: {group_name}")
                
                # Create fcurve and assign to group
                fcurve = fcurves_obj.new(data_path, index=index)
                fcurve.group = group
                print(f"[yakuza_gmt] Created fcurve with group: {data_path}[{index}] -> {group_name}")
            else:
                # Create fcurve without group
                fcurve = fcurves_obj.new(data_path, index=index)
                print(f"[yakuza_gmt] Created fcurve without group: {data_path}[{index}]")
            
            return fcurve
            
    except Exception as e:
        import traceback
        print(f"[yakuza_gmt] ERROR creating fcurve {data_path}[{index}]: {e}")
        print(f"[yakuza_gmt] Traceback: {traceback.format_exc()}")
        print(f"[yakuza_gmt] Input object type: {type(channelbag_or_action).__name__}")
        print(f"[yakuza_gmt] Input object repr: {repr(channelbag_or_action)}")
        raise GMTError(f"Failed to create fcurve {data_path}[{index}]: {e}")


def _action_groups_new(action, name: str):
    """Wrapper for action.groups.new that surfaces debug info on failure.

    If the underlying Action object does not have a 'groups' attribute (unexpected),
    raise a GMTError including the action type and a small debug dump so callers
    get a meaningful message instead of a cryptic AttributeError.
    """
    try:
        # If the Action has a groups collection, use it (normal case)
        if hasattr(action, 'groups'):
            return action.groups.new(name)
        # Some Blender environments or unassigned actions may present an object
        # that does not expose groups; fall back to a lightweight object that
        # provides a .name property so callers (which only use group.name)
        # continue to function.
        print(f"[yakuza_gmt] WARNING: Action object does not expose 'groups'; using fallback for group '{name}'")
        return SimpleNamespace(name=name)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        # Always print actionable debug info to the system console so users can copy it
        try:
            print(f"[yakuza_gmt] ERROR creating action group '{name}': {e}")
            try:
                print(f"[yakuza_gmt] action repr: {repr(action)}")
            except Exception:
                pass
            try:
                print(f"[yakuza_gmt] action type: {type(action)}")
            except Exception:
                pass
            try:
                has_groups = hasattr(action, 'groups')
                has_fcurves = hasattr(action, 'fcurves')
                print(f"[yakuza_gmt] has 'groups': {has_groups}, has 'fcurves': {has_fcurves}")
            except Exception:
                pass
            try:
                # show first 40 dir entries
                d = dir(action)
                print(f"[yakuza_gmt] action dir (first 40): {d[:40]}")
            except Exception:
                pass
            print(f"[yakuza_gmt] traceback:\n{tb}")
        except Exception:
            pass
        # Provide helpful debug output for the caller
    raise GMTError(f"Failed to create action group '{name}': {e} (action_type={type(action)})\nSee console for details")

# from .pattern import make_pattern_action
# from .pattern_lists import VERSION_STR


class ImportGMT(Operator, ImportHelper):
    """Loads a GMT file into blender"""
    bl_idname = "import_scene.gmt"
    bl_label = "Import Yakuza GMT"

    filter_glob: StringProperty(default="*.gmt;*.cmt;*.ifa", options={"HIDDEN"})

    def armature_callback(self, context):
        items = []
        ao = context.active_object
        ao_name = ao.name

        if ao and ao.type == 'ARMATURE':
            # Add the selected armature first so that it's the default value
            items.append((ao_name, ao_name, ""))

        for a in [arm for arm in bpy.data.objects if arm.type == 'ARMATURE' and arm.name != ao_name]:
            items.append((a.name, a.name, ""))
        return items

    armature_name: EnumProperty(
        items=armature_callback,
        name='Target Armature',
        description='The armature to use as a base for importing the animation. '
                    'This armature should be from a GMD from the same game as the animation'
    )

    merge_vector_curves: BoolProperty(
        name='Merge Vector',
        description='Merges vector_c_n animation into center_c_n, to allow for easier editing/previewing.\n'
                    'This option should not be disabled. Does not affect Y3-5 animations',
        default=True
    )

    is_auth: BoolProperty(
        name='Is Auth/Hact',
        description='Specify the animation\'s origin.\n'
                    'If this is enabled, then the animation should be from hact.par or auth folder. '
                    'Otherwise, it will be treated as being from motion folder.\n'
                    'Needed for proper vector merging for Y0/K1.\n'
                    'Does not affect Y3-Y5 or DE. Does not affect anything if Merge Vector is disabled',
        default=False
    )

    scale_object: BoolProperty(
        name='Use Scale',
        description='Scale the armature based on the height provided\n'
                    'This can be useful on Yakuza 5 and below for auth animations where characters have differing heights (the animation being offset differently because Haruka is 165cm in Yakuza 5 for example.)',
        default=False,
    )

    object_scale: bpy.props.IntProperty(
        name='Scale',
        description='Object scale in centimeters',
        default=185,
    )

    
    import_as_path: BoolProperty(
        name='Import As Path Animation',
        description='Import the GMT as path animation. Applied to root bone of selected armature.\n'
                    'This can be useful for animations that dont directly animate a model, but a path (which has no bones)',
        default=False,
    )

    additive: BoolProperty(
        name='Combine',
        description='Import the animation on top of existing one, not replacing it, this will convert the animation data into NLA strips.',
        default=False,
    )

    additive_adjust_len: BoolProperty(
        name='Adjust End Frame',
        description='Increase end frame when animation is combined',
        default=True,
    )


    def draw(self, context):
        layout = self.layout

        layout.use_property_split = True
        layout.use_property_decorate = True  # No animation.

        layout.prop(self, 'armature_name')
        layout.prop(self, 'merge_vector_curves')
        layout.prop(self, 'scale_object')
        object_scale = layout.row()
        object_scale.prop(self, 'object_scale')
        object_scale.enabled = self.scale_object

        is_auth_row = layout.row()
        is_auth_row.prop(self, 'is_auth')
        is_auth_row.enabled = self.merge_vector_curves

        layout.prop(self, 'import_as_path')
        layout.prop(self, 'additive')

        adjust_combine_len = layout.row()
        adjust_combine_len.prop(self, 'additive_adjust_len')
        adjust_combine_len.enabled = self.additive

    def execute(self, context):
        import time

        try:
            if self.filepath.endswith('.cmt'):
                importer_cls = CMTImporter
            else:
                arm = self.check_armature(context)
                if isinstance(arm, str):
                    raise GMTError(arm)

                importer_cls = IFAImporter if self.filepath.endswith('.ifa') else GMTImporter

            start_time = time.time()
            importer = importer_cls(context, self.filepath, self.as_keywords(ignore=("filter_glob",)))
            importer.read()

            elapsed_s = "{:.2f}s".format(time.time() - start_time)
            print("Import finished in " + elapsed_s)

            self.report({"INFO"}, f"Finished importing {basename(self.filepath)}")
            return {'FINISHED'}
        except GMTError as error:
            print("Catching Error")
            self.report({"ERROR"}, str(error))

        return {'CANCELLED'}

    def check_armature(self, context: bpy.context):
        """Sets the active object to be the armature chosen by the user"""

        if self.armature_name:
            armature = bpy.data.objects.get(self.armature_name)
            if armature:
                context.view_layer.objects.active = armature
                return 0

        # check the active object first
        ao = context.active_object
        if ao and ao.type == 'ARMATURE' and ao.data.bones[:]:
            return 0

        # if the active object isn't a valid armature, get its collection and check

        if ao:
            collection = ao.users_collection[0]
        else:
            collection = context.view_layer.active_layer_collection

        if collection and collection.name != 'Master Collection':
            meshObjects = [o for o in bpy.data.collections[collection.name].objects
                           if o.data in bpy.data.meshes[:] and o.find_armature()]

            armatures = [a.find_armature() for a in meshObjects]
            if meshObjects:
                armature = armatures[0]
                if armature.data.bones[:]:
                    context.view_layer.objects.active = armature
                    return 0

        return "No armature found to add animation to"
    
class ImportFaceTargetGMT(Operator, ImportHelper):
    """Loads a Face Target GMT file into blender"""
    bl_idname = "import_face_scene.gmt"
    bl_label = "Import Yakuza Face Target GMT"

    filter_glob: StringProperty(default="*.gmt;", options={"HIDDEN"})

    def armature_callback(self, context):
        items = []
        ao = context.active_object
        ao_name = ao.name

        if ao and ao.type == 'ARMATURE':
            # Add the selected armature first so that it's the default value
            items.append((ao_name, ao_name, ""))

        for a in [arm for arm in bpy.data.objects if arm.type == 'ARMATURE' and arm.name != ao_name]:
            items.append((a.name, a.name, ""))
        return items

    armature_name: EnumProperty(
        items=armature_callback,
        name='Target Armature',
        description='The armature to use as a base for importing the animation. '
                    'This armature should be from a GMD from the same game as the animation'
    )



    def draw(self, context):
        layout = self.layout

        layout.use_property_split = True
        layout.use_property_decorate = True  # No animation.

        layout.prop(self, 'armature_name')

    def execute(self, context):
        import time

        try:
            arm = self.check_armature(context)
            if isinstance(arm, str):
                raise GMTError(arm)

            importer_cls = GMTFaceTargetImporter

            start_time = time.time()
            importer = importer_cls(context, self.filepath, self.as_keywords(ignore=("filter_glob",)))
            importer.read()

            elapsed_s = "{:.2f}s".format(time.time() - start_time)
            print("Import finished in " + elapsed_s)

            self.report({"INFO"}, f"Finished importing {basename(self.filepath)}")
            return {'FINISHED'}
        except GMTError as error:
            print("Catching Error")
            self.report({"ERROR"}, str(error))

        return {'CANCELLED'}

    def check_armature(self, context: bpy.context):
        """Sets the active object to be the armature chosen by the user"""

        if self.armature_name:
            armature = bpy.data.objects.get(self.armature_name)
            if armature:
                context.view_layer.objects.active = armature
                return 0

        # check the active object first
        ao = context.active_object
        if ao and ao.type == 'ARMATURE' and ao.data.bones[:]:
            return 0

        # if the active object isn't a valid armature, get its collection and check

        if ao:
            collection = ao.users_collection[0]
        else:
            collection = context.view_layer.active_layer_collection

        if collection and collection.name != 'Master Collection':
            meshObjects = [o for o in bpy.data.collections[collection.name].objects
                           if o.data in bpy.data.meshes[:] and o.find_armature()]

            armatures = [a.find_armature() for a in meshObjects]
            if meshObjects:
                armature = armatures[0]
                if armature.data.bones[:]:
                    context.view_layer.objects.active = armature
                    return 0

        return "No armature found to add animation to"


def setup_armature(ao: bpy.types.Object) -> Dict[str, GMTBlenderBoneProps]:
    if not ao.animation_data:
        ao.animation_data_create()

    hidden = ao.hide_get()
    mode = ao.mode

    # Necessary steps to ensure proper importing
    ao.hide_set(False)
    bpy.ops.object.mode_set(mode='POSE')
    bpy.ops.pose.select_all(action='SELECT')
    bpy.ops.pose.transforms_clear()
    bpy.ops.pose.select_all(action='DESELECT')

    bone_props = get_edit_bones_props(ao)

    bpy.ops.object.mode_set(mode=mode)
    ao.hide_set(hidden)

    return bone_props


class IFAImporter:
    def __init__(self, context: bpy.context, filepath, import_settings: Dict):
        self.filepath = filepath
        self.context = context

    ifa: IFA

    def read(self):
        self.ifa = read_ifa(self.filepath)
        self.make_action()

    def make_action(self):
        ao = self.context.active_object

        bone_props = setup_armature(ao)

        # Ensure the armature has animation_data before assigning an action
        if not ao.animation_data:
            ao.animation_data_create()

        action = ao.animation_data.action = bpy.data.actions.new(name=f'{basename(self.filepath)}')

        # Instead of rewriting the curve importing functions, we can just convert the IFA bones to GMT curves
        for bone in self.ifa.bone_list:
            group = _action_groups_new(action, bone.name)

            for curve_values, curve_type in zip((bone.location, bone.rotation), (GMTCurveType.LOCATION, GMTCurveType.ROTATION)):
                curve = GMTCurve(curve_type)
                curve.keyframes.append(GMTKeyframe(0, curve_values))

                convert_gmt_curve_to_blender(curve)
                import_curve(self.context, curve, bone.name, action, group.name, bone_props)

        self.context.scene.frame_start = 0
        self.context.scene.frame_current = 0


class CMTImporter:
    def __init__(self, context: bpy.context, filepath, import_settings: Dict):
        self.filepath = filepath
        self.context = context
        self.combine = import_settings.get("additive")
        self.combine_adjust_len = import_settings.get("additive_adjust_len")

    cmt: CMT

    def read(self):
        self.cmt = read_cmt(self.filepath)
        self.animate_camera()

    def animate_camera(self):
        self.camera = self.context.scene.camera

        if not self.camera:
            camera_data = bpy.data.cameras.new(name='Camera')
            self.camera = bpy.data.objects.new('Camera', camera_data)
            self.context.scene.collection.objects.link(self.camera)

        if not self.camera.animation_data:
            self.camera.animation_data_create()

        # Set some properties that are needed for the imported action to look proper
        # While it might be possible to animate these, it will only complicate things
        self.camera.rotation_mode = 'QUATERNION'
        self.camera.data.lens_unit = 'MILLIMETERS'
        self.camera.data.sensor_fit = 'VERTICAL'
        self.camera.data.sensor_height = 100.0

        single = bool(self.cmt.animation)
        for i, anm in enumerate(self.cmt.animation_list):
            frame_rate = anm.frame_rate
            frame_count = len(anm.frames)
            self.make_action(anm, basename(self.filepath) + '' if single else f'({i})')

        self.context.scene.render.fps = int(frame_rate)
        self.context.scene.frame_start = 0
        self.context.scene.frame_current = 0

        if self.combine:
            if self.combine_adjust_len:
                self.context.scene.frame_end += frame_count 
        else:
            self.context.scene.frame_end = frame_count   

    def make_action(self, anm: CMTAnimation, action_name):

        if self.combine and self.camera.animation_data.action != None:
            action = self.camera.animation_data.action
            nla_track = action.nla_tracks.new()
            nla_strip = nla_track.strips.new(action.name, int(action.frame_range[0]), action)            
            self.camera.animation_data.action = None   

        # Create action and get channelbag
        action = bpy.data.actions.new(name=action_name)
        self.camera.animation_data.action = action
        channelbag = _get_or_create_channelbag(action, self.camera)
        
        # For legacy, we need to create the group explicitly
        if not HAS_ANIM_UTILS:
            group = _action_groups_new(channelbag, "Camera")
            group_name = group.name
        else:
            group_name = "Camera"

        # Convert the CMT frames before importing anything
        convert_cmt_anm_to_blender(anm, self.camera.data)
        dists, rotations = zip(*map(lambda x: x.to_dist_rotation(True), anm.frames))

        def import_curve(data_path, values):
            values = enumerate(zip(*values)) if hasattr(values[0], '__iter__') else [(-1, values)]

            for i, values_channel in values:
                fcurve = _ensure_fcurve(channelbag, data_path, i, group_name)
                fcurve.keyframe_points.add(len(values_channel))
                fcurve.keyframe_points.foreach_set('co', [x for co in zip(
                    range(len(values_channel)), values_channel) for x in co])

                fcurve.update()

        import_curve('location', list(map(lambda x: x.location[:], anm.frames)))
        import_curve('rotation_quaternion', list(map(lambda x: x[:], rotations)))
        import_curve('data.lens', list(map(lambda x: x.fov, anm.frames)))

        # Kenzan does not store the focus distance
        if self.cmt.version > CMTVersion.KENZAN:
            # TODO: This value for aperture_fstop is just an arbitrary value that gives a closer look to in-game DOF
            # This same fstop value should be used during animation or the resulting DOF in the game might look different
            import_curve('data.dof.use_dof', (True,))
            import_curve('data.dof.focus_distance', dists)
            import_curve('data.dof.aperture_fstop', (15.0,))

        if anm.has_clip_range():
            # CMTs that were read from a file will either have a clip range for all frames, or no clip ranges at all
            clip_starts, clip_ends = zip(*map(lambda x: x.clip_range, anm.frames))
            import_curve('data.clip_start', clip_starts)
            import_curve('data.clip_end', clip_ends)

        if(self.combine):
            hadAnimBefore = len(self.camera.animation_data.nla_tracks) > 0

        if(self.combine):
            if(hadAnimBefore):
                nla_track = self.camera.animation_data.nla_tracks[0]
                strips = [strip for track in self.camera.animation_data.nla_tracks for strip in track.strips]
                last_strip = max(strips, key=lambda s: s.frame_end)
                nla_strip = nla_track.strips.new(action.name, int(last_strip.frame_end), action)
            else:
                nla_track = self.camera.animation_data.nla_tracks.new()
                nla_strip = nla_track.strips.new(action.name, int(action.frame_range[0]), action)                 
            
            self.camera.animation_data.action = None



class GMTImporter:
    def __init__(self, context: bpy.context, filepath, import_settings: Dict):
        self.filepath = filepath
        self.context = context
        self.merge_vector_curves = import_settings.get('merge_vector_curves')
        self.is_auth = import_settings.get('is_auth')
        self.scale_object = import_settings.get('scale_object')
        self.object_scale = import_settings.get('object_scale')
        self.import_as_path = import_settings.get('import_as_path')
        self.combine = import_settings.get("additive")
        self.combine_adjust_len = import_settings.get("additive_adjust_len")

    gmt: GMT

    def read(self):
        try:
            self.gmt = read_gmt(self.filepath)
            self.make_actions()
            ftarget_result = apply_face_target_anim_to_shape_keys(self.context.active_object)

            action = self.context.active_object.animation_data.action

            # Face targets will be animated through shape keys. We no longer need this data.
            if(ftarget_result == True):
                to_remove = [fc for fc in action.fcurves if "pat2_unk" in fc.data_path]

                # Remove them safely
                for fc in to_remove:
                    action.fcurves.remove(fc)

            if(self.scale_object):
                # Linear mapping between (165 -> 0.890) and (185 -> 1.000)
                calculated_scale = scale_height(self.object_scale)
                self.context.active_object.scale = (calculated_scale,calculated_scale,calculated_scale)
                
        except Exception as e:
            raise GMTError(f'{e}')

    def make_actions(self):
        print(f'Importing file: {self.gmt.name}')

        ao = self.context.active_object
        bone_props = setup_armature(ao)

        vector_version = self.gmt.vector_version

        if self.combine and ao.animation_data.action != None:
            action = ao.animation_data.action
            nla_track = action.nla_tracks.new()
            nla_strip = nla_track.strips.new(action.name, int(action.frame_range[0]), action)            
            ao.animation_data.action = None

        end_frame = 1
        frame_rate = 30

        for anm in self.gmt.animation_list:
            anm_bone_props = dict() if (self.gmt.is_face_gmt and anm.is_face_anm()) else bone_props

            end_frame = max(end_frame, anm.end_frame)
            frame_rate = anm.frame_rate

            act_name = f'{anm.name}[{self.gmt.name}]'

            if(self.combine):
                hadAnimBefore = len(ao.animation_data.nla_tracks) > 0

            # Ensure animation_data exists on the armature before creating/assigning the action
            if not ao.animation_data:
                ao.animation_data_create()

            # Create action
            action = bpy.data.actions.new(act_name)
            print(f"[yakuza_gmt] Created action: {act_name}")
            
            # Get channelbag for Blender 5.0+ or use action directly for legacy
            channelbag = _get_or_create_channelbag(action, ao)

            bones: Dict[str, GMTBone] = dict()

            # Import the first "bone" of the animation. into the root bone of selected object
            if self.import_as_path == False:
                for bone_name in anm.bones:
                    if bone_name in ao.pose.bones:
                        bones[bone_name] = anm.bones[bone_name]
                    else:
                        print(f'WARNING: Skipped bone: "{bone_name}"')
            else:
                bones[ao.pose.bones[0].name] = anm.bones[next(iter(anm.bones))]

            # Convert curves early to allow for easier GMT modification before creating FCurves
            for bone_name in bones:
                for curve in bones[bone_name].curves:
                    convert_gmt_curve_to_blender(curve)

            # Try merging vector into center
            if self.merge_vector_curves:
                # Bone names are constant because vector does not exist pre-Ishin
                center_bone = bones.get('center_c_n')
                vector_bone = bones.get('vector_c_n')

                if(center_bone != None and vector_bone != None):
                    merge_vector(center_bone, vector_bone, vector_version, self.is_auth)

            for bone_name in bones:
                # Group handling - in both cases we use the bone_name as group_name
                # The _ensure_fcurve function handles the differences between Blender versions
                group_name = bone_name
                print(f'Importing ActionGroup: {group_name}')

                for curve in bones[bone_name].curves:
                    import_curve(self.context, curve, bone_name, channelbag, group_name, anm_bone_props)

            if(self.combine):
                if(hadAnimBefore):
                    nla_track = ao.animation_data.nla_tracks[0]
                    strips = [strip for track in ao.animation_data.nla_tracks for strip in track.strips]
                    last_strip = max(strips, key=lambda s: s.frame_end)
                    nla_strip = nla_track.strips.new(action.name, int(last_strip.frame_end), action)
                else:
                    nla_track = ao.animation_data.nla_tracks.new()
                    nla_strip = nla_track.strips.new(action.name, int(action.frame_range[0]), action)                 
            
                ao.animation_data.action = None

        # If pattern previewing is to be enabled later, this should be moved to the addon register function instead
        # Although that may require bone.par path in order to import the patterns with the basic skeleton GMDs
        # pattern_action = bpy.data.actions.get(f"GMT_Pattern{VERSION_STR[vector_version]}")
        # if not pattern_action and bpy.context.preferences.addons["yakuza_gmt"].preferences.get("use_patterns"):
        #     pattern_action = make_pattern_action(vector_version)

        self.context.scene.render.fps = int(frame_rate)
        self.context.scene.frame_start = 0
        self.context.scene.frame_current = 0

        if self.combine:
            if(self.combine_adjust_len):
                self.context.scene.frame_end += int(end_frame)   
        else:
            self.context.scene.frame_end = int(end_frame)       


# This is really bad. but the inconsistent height scaling has left me with no choice but to bully ChatGPT to solve this crisis.
def scale_height(measure):
    x1, y1 = 165, 0.890
    x2, y2 = 175, 0.940
    x3, y3 = 185, 1.000

    if measure <= x2:  # left segment (165 -> 175)
        slope = (y2 - y1) / (x2 - x1)  # 0.005 per unit
        return y1 + (measure - x1) * slope
    else:  # right segment (175 -> 185)
        slope = (y3 - y2) / (x3 - x2)  # 0.006 per unit
        return y2 + (measure - x2) * slope

class GMTFaceTargetImporter:
    def __init__(self, context: bpy.context, filepath, import_settings: Dict):
        self.filepath = filepath
        self.context = context
        
    gmt: GMT

    def read(self):
        try:
            self.gmt = read_gmt(self.filepath)
            self.make_actions()
                
        except Exception as e:
            raise GMTError(f'{e}')           

    def make_actions(self):
        print(f'Importing file: {self.gmt.name}')

        ao = self.context.active_object
        bone_props = setup_armature(ao)

        vector_version = self.gmt.vector_version

        end_frame = 1
        frame_rate = 30

        for anm in self.gmt.animation_list:
            anm_bone_props = dict() if (self.gmt.is_face_gmt and anm.is_face_anm()) else bone_props

            end_frame = max(end_frame, anm.end_frame)
            frame_rate = anm.frame_rate

            act_name = f'{anm.name}[{self.gmt.name}]'
            
            # Ensure animation_data exists
            if not ao.animation_data:
                ao.animation_data_create()
                
            # Create action and get channelbag
            action = bpy.data.actions.new(act_name)
            print(f"[yakuza_gmt] Created action: {act_name}")
            channelbag = _get_or_create_channelbag(action, ao)

            bones: Dict[str, GMTBone] = dict()

            # Import the first "bone" of the animation. into the root bone of selected object
            for bone_name in anm.bones:
                if bone_name in ao.pose.bones:
                    bones[bone_name] = anm.bones[bone_name]
                else:
                    print(f'WARNING: Skipped bone: "{bone_name}"')

            # Convert curves early to allow for easier GMT modification before creating FCurves
            for bone_name in bones:
                for curve in bones[bone_name].curves:
                    convert_gmt_curve_to_blender(curve)

            for bone_name in bones:
                # For Blender 5.0, groups are created implicitly when we use group_name in fcurve.ensure()
                # For legacy, we still need to create the group explicitly
                if not HAS_ANIM_UTILS:
                    group = _action_groups_new(channelbag, bone_name)
                    group_name = group.name
                else:
                    group_name = bone_name
                    
                print(f'Importing ActionGroup: {group_name}')

                for curve in bones[bone_name].curves:
                    import_curve(self.context, curve, bone_name, channelbag, group_name, anm_bone_props)

            for pbone in ao.pose.bones:
                # Clear location, rotation, scale
                pbone.location = (0.0, 0.0, 0.0)
                pbone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)  # Identity quaternion
                pbone.rotation_euler = (0.0, 0.0, 0.0)             # Also reset euler just in case
                pbone.scale = (1.0, 1.0, 1.0)

            create_shape_key_from_first_frame(ao, action)
            bpy.data.actions.remove(action)
           


def merge_vector(center_bone: GMTBone, vector_bone: GMTBone, vector_version: GMTVectorVersion, is_auth: bool):
    """Merges vector_c_n curves into center_c_n for easier modification.
    Does not affect NO_VECTOR animations.
    """

    if vector_version == GMTVectorVersion.NO_VECTOR:
        return

    if not (center_bone and vector_bone):
        print('GMTWarning: Cannot merge vector - \"center_c_n\" and/or \"vector_c_n\" bones are missing')

    if (vector_version == GMTVectorVersion.OLD_VECTOR and not is_auth) or vector_version == GMTVectorVersion.DRAGON_VECTOR:
        # Both curves' values should be applied, so add vector to center
        center_bone.location = add_curve(center_bone.location, vector_bone.location, GMTCurveType.LOCATION)
        center_bone.rotation = add_curve(center_bone.rotation, vector_bone.rotation, GMTCurveType.ROTATION)

    # Reset vector's curves to avoid confusion, since it won't be used anymore
    vector_bone.location = GMTCurve.new_location_curve()
    vector_bone.rotation = GMTCurve.new_rotation_curve()
    convert_gmt_curve_to_blender(vector_bone.location)
    convert_gmt_curve_to_blender(vector_bone.rotation)


def add_curve(curve: GMTCurve, other: GMTCurve, expected_curve_type: GMTCurveType) -> GMTCurve:
    """Adds the animation data of a curve to this curve. Both curves need to have the same GMTCurveType.
    If their type is LOCATION, vectors will be added.
    If their type is ROTATION, quaternions will be multiplied.
    expected_curve_type is only used if both curves are None
    """

    if (other or curve) is None:
        if expected_curve_type == GMTCurveType.LOCATION:
            curve = GMTCurve.new_location_curve()
        elif expected_curve_type == GMTCurveType.ROTATION:
            curve = GMTCurve.new_rotation_curve()
        else:
            curve = GMTCurve(expected_curve_type)

        convert_gmt_curve_to_blender(curve)
        return curve
    elif other is None:
        return curve
    elif curve is None:
        return deepcopy(other)

    if curve.type != other.type:
        raise GMTError('Curves with different types cannot be added')

    if curve.type == GMTCurveType.LOCATION:
        # Vector add and lerp
        def add(v1, v2): return v1 + v2
        def lerp(v1, v2, f): return v1.lerp(v2, f)

        if len(curve.keyframes) == 0:
            curve.keyframes.append(GMTKeyframe(0, Vector()))
    elif curve.type == GMTCurveType.ROTATION:
        # Quaternion multiply and slerp
        def add(v1, v2): return v1 @ v2
        def lerp(v1, v2, f): return v1.slerp(v2, f)

        if len(curve.keyframes) == 0:
            curve.keyframes.append(GMTKeyframe(0, Quaternion()))
    else:
        raise GMTError(f'Incompatible curve type for addition: {curve.type}')

    result = list()
    curve_dict = {kf.frame: kf.value for kf in curve.keyframes}
    curve_min = curve.keyframes[0].frame
    curve_max = curve.keyframes[-1].frame

    other_dict = {kf.frame: kf.value for kf in other.keyframes}
    other_min = other.keyframes[0].frame
    other_max = other.keyframes[-1].frame

    # Iterate over frames from 0 to the last frame in either curve
    for i in range(max(curve.get_end_frame(), other.get_end_frame()) + 1):
        # Check if the current frame has a keyframe
        v1 = curve_dict.get(i)
        v2 = other_dict.get(i)

        # Do not add/interpolate if no values are explicitly specified in this frame
        if not (v1 is None and v2 is None):
            if v1 is None:
                # Get the last keyframe that is less than the current frame, or the first keyframe
                less = next((k for k in reversed(curve_dict) if k < i), curve_min)

                # Get the first keyframe that is greater than the current frame, or the last keyframe
                more = next((k for k in curve_dict if k > i), curve_max)

                # Interpolate between the two values for the current frame, or use the only value if there is only 1 keyframe
                v1 = lerp(curve_dict[less], curve_dict[more], (i - less) /
                          (more - less)) if less != more else curve_dict[less]
            if v2 is None:
                less = next((k for k in reversed(other_dict) if k < i), other_min)
                more = next((k for k in other_dict if k > i), other_max)
                v2 = lerp(other_dict[less], other_dict[more], (i - less) /
                          (more - less)) if less != more else other_dict[less]

            result.append(GMTKeyframe(i, add(v1, v2)))

    curve.keyframes = result
    return curve


def import_curve(context: bpy.context, curve: GMTCurve, bone_name: str, channelbag_or_action, group_name: str, bone_props: Dict[str, GMTBlenderBoneProps]):
    """
    Import a GMT curve into Blender fcurves.
    Works with both Blender 5.0+ channelbags and legacy actions.
    
    Args:
        channelbag_or_action: either a channelbag (Blender 5.0+) or action (legacy)
    """
    try:
        data_path = get_data_path_from_curve_type(context, curve.type, curve.channel)

        if data_path == '' or len(curve.keyframes) == 0:
            print(f'[yakuza_gmt] Skipping type {curve.type} curve for {bone_name}...')
            return

        frames, values = zip(*map(lambda kf: (kf.frame, kf.value), curve.keyframes))

        need_const_interpolation = False
        if data_path == 'location':
            values = transform_location_to_blender(bone_props, bone_name, values)
        elif data_path == 'rotation_quaternion':
            values = transform_rotation_to_blender(bone_props, bone_name, values)
        elif 'pat1' in data_path:
            need_const_interpolation = True
            values = pattern1_to_blender(values)
        elif 'pat' in data_path:
            need_const_interpolation = True
            # pat2 and pat3 use the same format
            values = pattern2_to_blender(values)
        else:
            print(f'[yakuza_gmt] Unsupported data_path: {data_path}')
            return

        for i, values_channel in enumerate(zip(*values)):
            # Create fcurve using the helper that works with both channelbags and legacy actions
            full_data_path = f'pose.bones["{bone_name}"].{data_path}'
            fcurve = _ensure_fcurve(channelbag_or_action, full_data_path, i, group_name)
            
            if not fcurve:
                print(f'[yakuza_gmt] Failed to create fcurve for {full_data_path}[{i}]')
                continue
            
            fcurve.keyframe_points.add(len(frames))
            fcurve.keyframe_points.foreach_set('co', [x for co in zip(frames, values_channel) for x in co])

            # Not needed if the change_interpolation() handler is active
            if need_const_interpolation:
                for kf in fcurve.keyframe_points:
                    kf.interpolation = 'CONSTANT'

            fcurve.update()
            print(f'[yakuza_gmt] Successfully created fcurve: {full_data_path}[{i}] with {len(frames)} keyframes')
            
    except Exception as e:
        import traceback
        print(f'[yakuza_gmt] ERROR importing curve for {bone_name}: {e}')
        print(f'[yakuza_gmt] Traceback: {traceback.format_exc()}')
        raise GMTError(f'Failed to import curve for {bone_name}: {e}')


def get_data_path_from_curve_type(context: bpy.context, curve_type: GMTCurveType, curve_channel: GMTCurveChannel) -> str:
    if curve_type == GMTCurveType.LOCATION:
        return 'location'
    elif curve_type == GMTCurveType.ROTATION:
        return 'rotation_quaternion'
    elif curve_type == GMTCurveType.PATTERN_HAND:
        if curve_channel == GMTCurveChannel.LEFT_HAND:
            return 'pat1_left_hand'
        elif curve_channel == GMTCurveChannel.RIGHT_HAND:
            return 'pat1_right_hand'
        # GMTCurveChannel.UNK_HAND is not explicitly checked for since it's unknown if it's actually related to hands
        else:
            channel = curve_channel.value

            pat_tuple = (-32_768, 32_767, 0, f'pat1_unk_{channel}', f'Pat1 Unk {channel}', "Unknown pattern property")
            pat_string = '|'.join(map(lambda x: str(x), pat_tuple))

            if channel == GMTCurveChannel.FACE:
                return 'pat1_face_animation'

            # The type will be created, but it won't be added to the types dict (to be deleted) here
            # That will be taken care of in the unregister function of the addon
            return create_pose_bone_type(context, pat_string)
    elif curve_type in (GMTCurveType.PATTERN_UNK, GMTCurveType.PATTERN_FACE):
        channel = curve_channel.value

        pat_string = ""

        pat_num = 2 if GMTCurveType.PATTERN_UNK else 3

        pat_tuple = (-128, 127, 0, f'pat{pat_num}_unk_{channel}',
                        f'Face Target {get_flag_name(OEDEFaceTarget, channel)}', "Face target animation")
        pat_string = '|'.join(map(lambda x: str(x), pat_tuple))

        return create_pose_bone_type(context, pat_string)
    else:
        return ''


def create_pose_bone_type(context: bpy.context, pat_string: str):
    # Example pat: '-1|25|-1|pat1_left_hand|Left Hand|some description'
    splits = pat_string.split('|', 5)

    if len(splits) != 6:
        print('GMTWarning: Unexpected pattern string when creating a PoseBone attribute')
        return ''

    min_val, max_val, default_val, prop_name, pat_name, desc = splits

    # Only set the attribute (and add it to the collection) if it was not created before
    if not hasattr(bpy.types.PoseBone, prop_name):
        if hasattr(context.scene, 'pattern_types'):
            pat = context.scene.pattern_types.add()
            pat.string = pat_string
        else:
            print('GMTWarning: Addon did not register correctly - missing collection property in scene')

        setattr(bpy.types.PoseBone, prop_name, bpy.props.IntProperty(name=pat_name, min=int(
            min_val), max=int(max_val), description=desc, default=int(default_val)))

    return prop_name


def create_shape_key_from_first_frame(armature_obj, action):

    #Some face targets have face_c_n bones in them which messes up import
    remove_fcurves_for_bone(action, "face_c_n")

    action_name = action.name
    bpy.context.scene.frame_set(int(action.frame_range[0]))

    # Temporarily assign the action
    if not armature_obj.animation_data:
        armature_obj.animation_data_create()
    armature_obj.animation_data.action = action

    face_mesh = get_ideal_shape_key_mesh(armature_obj)

    depsgraph = bpy.context.evaluated_depsgraph_get()

    if(face_mesh != None):
        eval_obj = face_mesh.evaluated_get(depsgraph)
        mesh_data = eval_obj.to_mesh()

        # Add Basis if needed
        if not face_mesh.data.shape_keys:
            face_mesh.shape_key_add(name="Basis")

        # Add shape key with the action name
        shape_name = clean_face_target_name(action_name)
        shape_key = face_mesh.shape_key_add(name=shape_name, from_mix=False)
        shape_key.slider_min = -0.5
        shape_key.slider_max = 0.5

        for i, vert in enumerate(mesh_data.vertices):
            shape_key.data[i].co = vert.co

        eval_obj.to_mesh_clear()
        print(f"Added shape key '{action_name}' to '{face_mesh.name}'")      



def menu_func_import(self, context):
    self.layout.operator(ImportGMT.bl_idname, text='Yakuza Animation (.gmt/.cmt/.ifa)')
    self.layout.operator(ImportFaceTargetGMT.bl_idname, text='Yakuza Face Target Animation (f_res .gmt)')


def get_flag_name(enum_class, value):
    try:
        flag = enum_class(value)
        return flag.name or str(value)
    except ValueError:
        return str(value)
    
def clean_face_target_name(text):
    no_prefix = text.split('_', 1)[1] if '_' in text else text
    return no_prefix.split('[', 1)[0]   

def apply_face_target_anim_to_shape_keys(ao):
    is_any_shape_key_applied = False

    face_bone = ao.data.bones.get("face_c_n")

    if(not face_bone):
        return False
    
    fcurves_to_remove = []

    action = ao.animation_data.action if ao.animation_data else None
    # Fallback: try to resolve action by name from bpy.data.actions when missing
    if action and not hasattr(action, 'fcurves'):
        try:
            aname = getattr(action, 'name', None)
            if isinstance(aname, str):
                candidate = bpy.data.actions.get(aname)
                if candidate and hasattr(candidate, 'fcurves'):
                    action = candidate
                    try:
                        if ao and ao.animation_data:
                            ao.animation_data.action = action
                    except Exception:
                        pass
        except Exception:
            pass
    if action:
        for fcurve in action.fcurves:
            if 'pose.bones["face_c_n"]' in fcurve.data_path:
                prefix = 'pose.bones["face_c_n"].'
                clean_path = fcurve.data_path[len(prefix):]

                if(clean_path.startswith("pat2_unk")):
                    shape_key_target_id = clean_path.replace("pat2_unk_", "")
                    shape_key_target = OEDEFaceTarget(int(shape_key_target_id)).name

                    meshes = [
                        obj for obj in bpy.data.objects
                        if obj.type == 'MESH' and any(
                            mod.type == 'ARMATURE' and mod.object == ao for mod in obj.modifiers
                        )
                    ]

                    for mesh_obj in meshes:
                        print(f"🔎 Checking mesh: {mesh_obj.name}")

                        # 🎭 Skip if shape key does not exist
                        if not mesh_obj.data.shape_keys or shape_key_target not in mesh_obj.data.shape_keys.key_blocks:
                            print(f"⚠️ Shape key '{shape_key_target}' not found on '{mesh_obj.name}' — skipping.")
                            continue

                        # 🎯 Get the shape key data block
                        shape_keys = mesh_obj.data.shape_keys

                        # 🎭 Make sure it has animation data
                        if not shape_keys.animation_data:
                            shape_keys.animation_data_create()
                        if not shape_keys.animation_data.action or not hasattr(shape_keys.animation_data.action, 'fcurves'):
                            # ensure a usable action exists for the shape keys
                            shape_keys.animation_data.action = bpy.data.actions.new(name=f"{mesh_obj.name}_ShapeKeyAction")

                        # 🧼 Remove old FCurve(s)
                        shape_key_path = f'key_blocks["{shape_key_target}"].value'
                        existing = [fc for fc in shape_keys.animation_data.action.fcurves if fc.data_path == shape_key_path]
                        for fc in existing:
                            shape_keys.animation_data.action.fcurves.remove(fc)

                        # ➕ Add new FCurve for shape key value
                        # Use helper function for Blender 5.0 compatibility
                        if shape_keys.animation_data and shape_keys.animation_data.action:
                            # Get channelbag for shape keys if Blender 5.0+
                            shape_action = shape_keys.animation_data.action
                            if HAS_ANIM_UTILS and IS_BLENDER_5_0_PLUS:
                                # For shape keys, we need to create channelbag for the mesh object
                                shape_channelbag = _get_or_create_channelbag(shape_action, mesh_obj)
                                new_fcurve = _ensure_fcurve(shape_channelbag, shape_key_path, 0)
                            else:
                                # Legacy mode
                                new_fcurve = shape_action.fcurves.new(data_path=shape_key_path)
                        else:
                            print("⚠️ No animation data for shape keys")
                            continue

                        # 🧪 Insert remapped keyframes
                        for kp in fcurve.keyframe_points:
                            frame = kp.co.x
                            byte_val = kp.co.y
                            shape_val = adjust_range(byte_val, -127, 127, -0.5, 0.5) #byte_to_half_float(byte_val)

                            new_kp = new_fcurve.keyframe_points.insert(frame, shape_val, options={'FAST'})
                            new_kp.interpolation = kp.interpolation

                        fcurves_to_remove.append(fcurve)
                        print(f"✅ Applied shape key curve to '{mesh_obj.name}'")
                        is_any_shape_key_applied = True

                    print(f"🎯 Curve: {shape_key_target} {shape_key_target_id} [Index: {fcurve.array_index}]")
                    for keyframe in fcurve.keyframe_points:
                        print(f"  Frame {keyframe.co.x:.0f}: Value {keyframe.co.y:.6f}")                  
    else:
        print("⚠️ No action assigned to the armature.")

    return is_any_shape_key_applied

def byte_to_half_float(value):
    return (value / 127.0) * 0.5

def adjust_range(value, old_min, old_max, new_min, new_max):
    return new_min + (value - old_min) * (new_max - new_min) / (old_max - old_min)

def get_ideal_shape_key_mesh(ao):
     # Get all mesh objects influenced by this armature
    meshes = [
        obj for obj in bpy.data.objects
        if obj.type == 'MESH' and any(mod.type == 'ARMATURE' and mod.object == ao for mod in obj.modifiers)
    ]

    # Try to find one with "face" in the name
    face_mesh = next((obj for obj in meshes if "face" in obj.name.lower()), None)

    if(face_mesh == None):
        if(meshes):
            face_mesh = meshes[0] 

    return face_mesh  

def remove_fcurves_for_bone(action, bone_name):
    if not action:
        return

    # Resolve action object if it does not expose fcurves
    if not hasattr(action, 'fcurves'):
        try:
            aname = getattr(action, 'name', None)
            if isinstance(aname, str):
                candidate = bpy.data.actions.get(aname)
                if candidate:
                    action = candidate
        except Exception:
            pass

    # Collect F-Curves to remove
    to_remove = [fcu for fcu in action.fcurves
                 if fcu.data_path.startswith(f'pose.bones["{bone_name}"]')]

    for fcu in to_remove:
        action.fcurves.remove(fcu)
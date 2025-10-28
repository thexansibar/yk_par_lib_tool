import array
from typing import Union, List, Dict, cast, Tuple, Set

import bmesh
import bpy.types
from mathutils import Matrix, Vector
from ...common import AttribSetLayerNames, AttribSetLayers_bmesh
from ....gmdlib.abstract.gmd_mesh import GMDMesh, GMDSkinnedMesh
from ....gmdlib.abstract.gmd_shader import GMDSkinnedVertexBuffer, GMDVertexBuffer
from ....gmdlib.errors.error_reporter import ErrorReporter
from ....meshlib.vertex_fusion import vertex_fusion, make_bone_indices_consistent


def gmd_meshes_to_bmesh(
        name: str,
        gmd_meshes: Union[List[GMDMesh], List[GMDSkinnedMesh]],
        vertex_group_indices: Dict[str, int],
        attr_set_material_idx_mapping: Dict[int, int],
        gmd_to_blender_world: Matrix,
        fuse_vertices: bool,
        custom_split_normals: bool,
        error: ErrorReporter
) -> bpy.types.Mesh:
    if len(gmd_meshes) == 0:
        error.fatal("Called gmd_meshes_to_bmesh with 0 meshes!")
    if len([m for m in gmd_meshes if m.empty]) > 0:
        error.fatal("Called gmd_meshes_to_bmesh with meshes marked as empty! This can only happen if using "
                    "VertexImportMode.NO_VERTICES, which shouldn't be the case here.")

    is_skinned = isinstance(gmd_meshes[0], GMDSkinnedMesh) and gmd_meshes[0].vertices_data.layout.assume_skinned
    error.debug("MESH", f"make_merged_gmd_mesh called with {gmd_meshes} skinned={is_skinned} fusing={fuse_vertices}")
    error.debug("MESH", f"vertex layout: {str(gmd_meshes[0].vertices_data.layout)}")

    # If necessary, rewrite bone indices to be consistent
    vertices: Union[List[GMDVertexBuffer], List[GMDSkinnedVertexBuffer]]
    if is_skinned:
        if not all(isinstance(x, GMDSkinnedMesh) for x in gmd_meshes):
            error.fatal("Called gmd_meshes_to_bmesh with a mix of skinned and unskinned meshes")

        gmd_meshes = cast(List[GMDSkinnedMesh], gmd_meshes)
        # Rewrite vertices data to use consistent bone indices throughout
        relevant_bones, vertices = make_bone_indices_consistent(gmd_meshes)
    else:
        if any(isinstance(x, GMDSkinnedMesh) for x in gmd_meshes):
            error.fatal("Called gmd_meshes_to_bmesh with a mix of skinned and unskinned meshes")

        gmd_meshes = cast(List[GMDMesh], gmd_meshes)
        relevant_bones = None
        vertices = [
            m.vertices_data
            for m in gmd_meshes
        ]

    # Create the mesh and deform layer (if necessary)
    bm = bmesh.new()
    deform = bm.verts.layers.deform.new("Vertex Weights") if is_skinned else None
    if deform and (relevant_bones is None):
        error.fatal(f"Mismatch between deform/is_skinned, and the existence of relevant_bones")

    # Add a single vertex's (position, normal, bone weights) to the BMesh
    def add_vertex_to_bmesh(buf, i: int):
        yk_pos = buf.pos[i]
        vert = bm.verts.new((-yk_pos[0], yk_pos[2], yk_pos[1]))
        if buf.normal is not None:
            # apply the matrix to normal.xyz.resized(4) to set the w component to 0 - normals cannot be translated!
            # Just using .xyz would make blender apply a translation
            yk_norm = buf.normal[i]
            vert.normal = Vector((-yk_norm[0], yk_norm[2], yk_norm[1]))
        if is_skinned:
            bones = buf.bone_data[i]
            weights = buf.weight_data[i]
            for j in range(len(bones)):
                if weights[j] <= 0:
                    break
                vertex_group_index = vertex_group_indices[relevant_bones[bones[j]].name]
                vert[deform][vertex_group_index] = weights[j]

    # Optionally apply vertex fusion (merging "adjacent" vertices while keeping per-loop data)
    # before adding all vertices in order
    if fuse_vertices:
        _, mesh_vtx_idx_to_bmesh_idx, is_fused = vertex_fusion([m.triangles.triangle_list for m in gmd_meshes],
                                                               vertices)
        for i_buf, buf in enumerate(vertices):
            for i in range(len(buf)):
                if not is_fused[i_buf][i]:
                    assert len(bm.verts) == mesh_vtx_idx_to_bmesh_idx[i_buf][i]
                    add_vertex_to_bmesh(buf, i)
    else:
        mesh_vtx_idx_to_bmesh_idx = [
            [-1] * len(buf)
            for buf in vertices
        ]
        # Assign each vertex in each mesh to the bmesh
        i_vert = 0
        for i_buf, buf in enumerate(vertices):
            for i in range(len(buf)):
                add_vertex_to_bmesh(buf, i)
                mesh_vtx_idx_to_bmesh_idx[i_buf][i] = i_vert
                i_vert += 1

    # Now we've added the vertices, update bmesh internal structures
    bm.verts.ensure_lookup_table()
    bm.verts.index_update()

    # The GMD meshes may have different vertex layouts -> may need to send vertex data to different layers
    # => for each unique vertex layout in our meshes, find the layers they need and create/reuse them
    unique_vertex_layouts = set([buf.layout for buf in vertices])
    # Map the "packing flags" to the set of layers to use (packing flags are essentially the hash of the attribute set)
    attr_set_layers: Dict[int, AttribSetLayers_bmesh] = {
        layout.packing_flags: AttribSetLayerNames.build_from(layout, is_skinned).create_on(bm, error)
        for layout in unique_vertex_layouts
    }

    # Put the faces and extra data in the BMesh
    triangles: Set[Tuple[int, int, int]] = set()

    # OPTIMIZATION: Pre-allocate lists for batch layer assignment
    # This reduces per-loop overhead significantly
    for m_i, gmd_mesh in enumerate(gmd_meshes):
        layers = attr_set_layers[gmd_mesh.vertices_data.layout.packing_flags]
        # Check the layers
        if not layers.tangent_layer and gmd_mesh.vertices_data.layout.tangent_storage and layers.primary_uv_i is None:
            error.recoverable(f"Material/shader {gmd_mesh.attribute_set.shader} requires tangents to be calculated, "
                              f"but doesn't have a primary UV map."
                              f"This will fail if you try to export it, because Blender calculates tangents from UV maps."
                              f"If you're OK with this, disable Strict Import and try again")

        attr_idx = attr_set_material_idx_mapping[id(gmd_mesh.attribute_set)]

        # For face in mesh
        for ti in range(0, len(gmd_mesh.triangles.triangle_list), 3):
            tri_idxs = gmd_mesh.triangles.triangle_list[ti:ti + 3]
            if 0xFFFF in tri_idxs:
                error.recoverable(f"Found an 0xFFFF index inside a triangle_indices list! That shouldn't happen.")
                continue

            remapped_tri_idxs = tuple(mesh_vtx_idx_to_bmesh_idx[m_i][v_i] for v_i in tri_idxs)
            # If face doesn't already exist, and is valid
            if len(set(remapped_tri_idxs)) != 3:
                continue
            if tuple(sorted(remapped_tri_idxs)) in triangles:
                continue
            # Create face
            try:
                # This can throw ValueError if the triangle is "degenerate" - i.e. has two vertices that are the same
                # [1, 2, 3] is fine
                # [1, 2, 2] is degenerate
                # This should never be called with degenerate triangles, but if there is one we skip it and recover.
                face = bm.faces.new(
                    (bm.verts[remapped_tri_idxs[0]], bm.verts[remapped_tri_idxs[1]], bm.verts[remapped_tri_idxs[2]]))
            except ValueError as e:
                error.recoverable(
                    f"Adding face {remapped_tri_idxs} resulted in ValueError - This should have been a valid triangle. "
                    f"Vert count: {len(bm.verts)}.\n{e}")
                continue
            else:
                face.smooth = True
                face.material_index = attr_idx
                triangles.add(tuple(sorted(remapped_tri_idxs)))

            verts_with_loops = list(zip(tri_idxs, face.loops))

            # OPTIMIZATION: Batch layer assignments - loop once per attribute type instead of
            # iterating verts_with_loops multiple times
            # This reduces Python loop overhead and improves cache locality
            
            # Pre-fetch vertex data for all three loops (better cache locality)
            v_i_0, v_i_1, v_i_2 = tri_idxs
            loop_0, loop_1, loop_2 = face.loops[0], face.loops[1], face.loops[2]
            
            # Apply Col0
            if layers.col0_layer:
                col0_data = gmd_mesh.vertices_data.col0
                loop_0[layers.col0_layer] = col0_data[v_i_0]
                loop_1[layers.col0_layer] = col0_data[v_i_1]
                loop_2[layers.col0_layer] = col0_data[v_i_2]

            # Apply Col1
            if layers.col1_layer:
                col1_data = gmd_mesh.vertices_data.col1
                loop_0[layers.col1_layer] = col1_data[v_i_0]
                loop_1[layers.col1_layer] = col1_data[v_i_1]
                loop_2[layers.col1_layer] = col1_data[v_i_2]

            # Apply weight_data
            if layers.weight_data_layer:
                weight_data = gmd_mesh.vertices_data.weight_data
                loop_0[layers.weight_data_layer] = weight_data[v_i_0]
                loop_1[layers.weight_data_layer] = weight_data[v_i_1]
                loop_2[layers.weight_data_layer] = weight_data[v_i_2]

            # Apply bone_data
            if layers.bone_data_layer:
                bone_data = gmd_mesh.vertices_data.bone_data
                loop_0[layers.bone_data_layer] = bone_data[v_i_0] / 255
                loop_1[layers.bone_data_layer] = bone_data[v_i_1] / 255
                loop_2[layers.bone_data_layer] = bone_data[v_i_2] / 255

            # Apply normal_w
            if layers.normal_w_layer:
                normal_data = gmd_mesh.vertices_data.normal
                loop_0[layers.normal_w_layer] = ((normal_data[v_i_0][3] + 1) / 2, 0, 0, 0)
                loop_1[layers.normal_w_layer] = ((normal_data[v_i_1][3] + 1) / 2, 0, 0, 0)
                loop_2[layers.normal_w_layer] = ((normal_data[v_i_2][3] + 1) / 2, 0, 0, 0)

            # Apply tangent
            if layers.tangent_layer:
                tangent_data = gmd_mesh.vertices_data.tangent
                t0, t1, t2 = tangent_data[v_i_0], tangent_data[v_i_1], tangent_data[v_i_2]
                loop_0[layers.tangent_layer] = ((t0[0] + 1) / 2, (t0[1] + 1) / 2, (t0[2] + 1) / 2, (t0[3] + 1) / 2)
                loop_1[layers.tangent_layer] = ((t1[0] + 1) / 2, (t1[1] + 1) / 2, (t1[2] + 1) / 2, (t1[3] + 1) / 2)
                loop_2[layers.tangent_layer] = ((t2[0] + 1) / 2, (t2[1] + 1) / 2, (t2[2] + 1) / 2, (t2[3] + 1) / 2)

            # Apply tangent_w
            if layers.tangent_w_layer:
                tangent_data = gmd_mesh.vertices_data.tangent
                loop_0[layers.tangent_w_layer] = ((tangent_data[v_i_0][3] + 1) / 2, 0, 0, 0)
                loop_1[layers.tangent_w_layer] = ((tangent_data[v_i_1][3] + 1) / 2, 0, 0, 0)
                loop_2[layers.tangent_w_layer] = ((tangent_data[v_i_2][3] + 1) / 2, 0, 0, 0)

            # Apply UVs
            for uv_i, (uv_componentcount, uv_layer) in enumerate(layers.uv_layers):
                uv_data = gmd_mesh.vertices_data.uvs[uv_i]
                if uv_componentcount == 2:
                    uv0, uv1, uv2 = uv_data[v_i_0], uv_data[v_i_1], uv_data[v_i_2]
                    loop_0[uv_layer].uv = (uv0[0], 1.0 - uv0[1])
                    loop_1[uv_layer].uv = (uv1[0], 1.0 - uv1[1])
                    loop_2[uv_layer].uv = (uv2[0], 1.0 - uv2[1])
                else:
                    uv0, uv1, uv2 = uv_data[v_i_0], uv_data[v_i_1], uv_data[v_i_2]
                    loop_0[uv_layer] = Vector(uv0).resized(4)
                    loop_1[uv_layer] = Vector(uv1).resized(4)
                    loop_2[uv_layer] = Vector(uv2).resized(4)
                    # Check for out-of-range values
                    for uv_val in [uv0, uv1, uv2]:
                        if any(x < 0 or x > 1 for x in uv_val):
                            error.recoverable(f"Data in UV{uv_i} is outside the range of values Blender can store. "
                                              f"Expected values between 0 and 1, got {uv_val}")

    overall_mesh = bpy.data.meshes.new(name)
    error.debug("OBJ", f"\tOverall mesh vert count: {len(bm.verts)}")
    bm.to_mesh(overall_mesh)

    # Use custom split normals on the final mesh to ensure the exact normals from the source file are used here.
    # We make a few assumptions:
    # 1. the vertices from bm.verts are in the same order in overall_mesh.vertices
    # 2. the normals from bm.verts are not modified between setting them and now
    #
    # bpy.types.Mesh custom split normals are per-vertex-loop i.e. per face corner.
    # => for each face corner, look up the original vertex index and use that to determine the new normal.
    #
    # Code for setting split normals is based on the FBX importer:
    # https://github.com/sobotka/blender-addons/blob/master/io_scene_fbx/import_fbx.py#L1336
    if custom_split_normals:
        # pre-Blender-4.1 we had to explicitly ask for split normals. Now calling normals_split_custom_set is enough?
        if bpy.app.version < (4, 1):
            overall_mesh.create_normals_split()

        # clnors = [ vert0normalX, vert0normalY, vert0normalZ, vert1normalX, ...]
        clnors = array.array('f', [0.0] * (len(overall_mesh.loops) * 3))
        for i_loop, loop in enumerate(overall_mesh.loops):
            nor: Vector = bm.verts[loop.vertex_index].normal
            nor.normalize()
            clnors[i_loop * 3 + 0] = nor.x
            clnors[i_loop * 3 + 1] = nor.y
            clnors[i_loop * 3 + 2] = nor.z

        # This is a really weird setup.
        # tuple(
        #   # zip the same iterator three times to create an iter of (float, float, float)
        #   zip(clnors_iter, clnors_iter, clnors_iter)
        # ) # expand the iterator of (float, float, float) into a tuple
        # It creates three references to the same iterator, and zip() goes through them in order
        # clnors is [x, y, z, x, y, z, ...].
        # zip() pops values out of the same iterator three times, so pops an x then a y then a z.
        # zip() then packs those into a tuple and yields it.
        # The toplevel tuple() creates a tuple-of-(xyz-tuple) from all the (xyz-tuple)s emitted from zip()
        clnors_iter = iter(clnors)
        overall_mesh.normals_split_custom_set(tuple(zip(clnors_iter, clnors_iter, clnors_iter)))

        # pre-Blender-4.1 we had to enable things like auto-smooth as a magic incantation to make normals work better.
        if bpy.app.version < (4, 1):
            overall_mesh.use_auto_smooth = True
            overall_mesh.free_normals_split()  # Really not sure why we do this, but it doesn't have a visual impact.
            # Maybe it helps save memory, in which case we should do it.

    bm.free()

    return overall_mesh

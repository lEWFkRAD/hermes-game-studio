"""Analyze GLB surface topology using disposable exact-position-welded arrays.

blender --background --factory-startup --threads 4 --python geometry-topology.py -- \
    --input asset.glb --out-dir new-topology-directory

This imports but never saves/exports/repairs the source. Vertex welding exists
only in Python analysis arrays. Positions are exact numeric Blender world-space
coordinates, with no distance tolerance. Coincident touching objects can merge.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import time

import bpy


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bounds(points):
    low = [min(point[i] for point in points) for i in range(3)]
    high = [max(point[i] for point in points) for i in range(3)]
    return {'min': low, 'max': high, 'dimensions': [high[i] - low[i] for i in range(3)]}


def triangle_area(a, b, c):
    ab = tuple(b[i] - a[i] for i in range(3))
    ac = tuple(c[i] - a[i] for i in range(3))
    cross = (ab[1] * ac[2] - ab[2] * ac[1],
             ab[2] * ac[0] - ab[0] * ac[2],
             ab[0] * ac[1] - ab[1] * ac[0])
    return .5 * math.sqrt(sum(value * value for value in cross))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--largest-components', type=int, default=50)
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else [])
    if args.largest_components < 1:
        parser.error('largest-components must be positive')
    source = args.input.resolve(strict=True)
    output = args.out_dir.resolve()
    if output == source.parent:
        parser.error('Choose a separate analysis output directory')
    output.mkdir(parents=True, exist_ok=True)
    destination = output / 'topology.json'
    if destination.exists():
        parser.error('topology.json already exists; choose a new output directory')
    started = time.monotonic()
    before = sha256(source)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=str(source))
    bpy.context.scene.frame_set(1)
    bpy.context.view_layer.update()
    meshes = [obj for obj in bpy.context.scene.objects if obj.type == 'MESH']
    if not meshes:
        raise ValueError('No mesh objects found')

    # Exact numeric coordinate identity collapses UV/normal splits without
    # changing Blender mesh data, material indices, attributes, or source bytes.
    position_ids = {}
    positions = []
    triangles = []
    mesh_rows = []
    nonfinite_vertices = 0
    for obj in meshes:
        mesh = obj.data
        mesh.calc_loop_triangles()
        indices = []
        for vertex in mesh.vertices:
            position = tuple(float(v) for v in (obj.matrix_world @ vertex.co))
            if not all(math.isfinite(value) for value in position):
                nonfinite_vertices += 1
                raise ValueError('Nonfinite vertex position; topology analysis cannot continue')
            if position not in position_ids:
                position_ids[position] = len(positions)
                positions.append(position)
            indices.append(position_ids[position])
        triangles.extend(tuple(indices[v] for v in triangle.vertices)
                         for triangle in mesh.loop_triangles)
        mesh_rows.append({'object': obj.name, 'indexedVertices': len(mesh.vertices),
                          'triangles': len(mesh.loop_triangles),
                          'uniqueExactPositionsWithinObject': len(set(indices))})
    if not positions:
        raise ValueError('No mesh vertices found')
    global_bounds = bounds(positions)
    diagonal_squared = sum(d * d for d in global_bounds['dimensions'])
    near_zero_threshold = diagonal_squared * 1e-12
    parent = list(range(len(positions)))
    sizes = [1] * len(positions)

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(a, b):
        a, b = find(a), find(b)
        if a == b:
            return
        if sizes[a] < sizes[b]:
            a, b = b, a
        parent[b] = a
        sizes[a] += sizes[b]

    valid_triangles = []
    areas = []
    degenerate_repeated_indices = 0
    zero_area = 0
    near_zero_nonzero = 0
    repeated_triangles = 0
    triangle_keys = set()
    edge_uses = Counter()
    edge_directions = Counter()
    referenced = set()
    for triangle in triangles:
        a, b, c = triangle
        area = triangle_area(positions[a], positions[b], positions[c])
        areas.append(area)
        if len(set(triangle)) < 3:
            degenerate_repeated_indices += 1
        if area == 0:
            zero_area += 1
            continue
        if area < near_zero_threshold:
            near_zero_nonzero += 1
        key = tuple(sorted(triangle))
        if key in triangle_keys:
            repeated_triangles += 1
        triangle_keys.add(key)
        valid_triangles.append((triangle, area))
        referenced.update(triangle)
        union(a, b)
        union(b, c)
        for first, second in ((a, b), (b, c), (c, a)):
            edge = (first, second) if first < second else (second, first)
            edge_uses[edge] += 1
            edge_directions[edge] += 1 if first < second else -1

    component_vertices = {}
    for index in referenced:
        component_vertices.setdefault(find(index), []).append(index)
    component_faces = Counter()
    component_areas = Counter()
    for triangle, area in valid_triangles:
        root = find(triangle[0])
        component_faces[root] += 1
        component_areas[root] += area
    boundary_by_component = Counter()
    nonmanifold_by_component = Counter()
    winding_by_component = Counter()
    boundary_edge_count = 0
    nonmanifold_edge_count = 0
    winding_conflicts = 0
    edge_lengths = []
    for (a, b), count in edge_uses.items():
        root = find(a)
        edge_lengths.append(math.dist(positions[a], positions[b]))
        if count == 1:
            boundary_edge_count += 1
            boundary_by_component[root] += 1
        elif count > 2:
            nonmanifold_edge_count += 1
            nonmanifold_by_component[root] += 1
        elif abs(edge_directions[(a, b)]) == 2:
            winding_conflicts += 1
            winding_by_component[root] += 1

    components = []
    for root, indices in component_vertices.items():
        components.append({'vertices': len(indices), 'triangles': component_faces[root],
                           'surfaceArea': component_areas[root],
                           'boundaryEdges': boundary_by_component[root],
                           'nonmanifoldEdges': nonmanifold_by_component[root],
                           'twoFaceWindingConflictEdges': winding_by_component[root],
                           'boundsBlenderWorld': bounds([positions[index] for index in indices])})
    components.sort(key=lambda item: (-item['triangles'], -item['vertices']))
    after = sha256(source)
    if after != before:
        raise RuntimeError('Source hash changed during analysis')
    indexed_vertices = sum(row['indexedVertices'] for row in mesh_rows)
    report = {
        'schema': 'exact-position-surface-topology-v1',
        'createdUtc': datetime.now(timezone.utc).isoformat(),
        'status': 'analyzed', 'blenderVersion': bpy.app.version_string,
        'source': {'path': str(source), 'bytes': source.stat().st_size,
                   'sha256Before': before, 'sha256After': after, 'unchanged': after == before},
        'method': {'weld': 'Exact numeric equality of imported base-mesh world positions; no tolerance',
                   'sourceMutated': False, 'meshDataMutated': False,
                   'analysisData': 'Disposable Python coordinates, indices, union-find and edge incidence arrays',
                   'coordinateSystem': 'Blender world coordinates after glTF import (+Z up)',
                   'componentDefinition': 'Vertex-connected surfaces after exact-position welding; zero-area triangles excluded',
                   'boundaryDefinition': 'Undirected edge incident to exactly one nonzero-area triangle',
                   'nonmanifoldEdgeDefinition': 'Undirected edge incident to more than two nonzero-area triangles',
                   'windingConflictDefinition': 'Two-face edge with both directed uses in the same direction',
                   'limitations': ['Exact coincident touching surfaces can merge into one component.',
                                   'Near-coincident positions do not weld; exact seams may remain when coordinates differ.',
                                   'Uses base meshes at frame 1; armature/morph/modifier deformation is not evaluated.',
                                   'Does not test self-intersections, nonmanifold vertex fans, UV quality, or animation suitability.',
                                   'A closed edge-manifold surface is not a production-readiness certificate.']},
        'meshes': mesh_rows,
        'counts': {'meshObjects': len(meshes), 'indexedVertices': indexed_vertices,
                   'exactPositionVertices': len(positions),
                   'duplicatePositionVerticesCollapsed': indexed_vertices - len(positions),
                   'triangles': len(triangles), 'nonzeroAreaTriangles': len(valid_triangles),
                   'exactWeldedSurfaceComponents': len(components),
                   'verticesUnreferencedByNonzeroAreaTriangles': len(positions) - len(referenced),
                   'uniqueSurfaceEdges': len(edge_uses), 'boundaryEdges': boundary_edge_count,
                   'nonmanifoldEdges': nonmanifold_edge_count,
                   'twoFaceWindingConflictEdges': winding_conflicts,
                   'zeroAreaTriangles': zero_area,
                   'repeatedIndexTrianglesAfterWeld': degenerate_repeated_indices,
                   'duplicateNonzeroAreaTrianglesIgnoringWinding': repeated_triangles,
                   'nearZeroButNonzeroAreaTriangles': near_zero_nonzero,
                   'nonfiniteVertexPositions': nonfinite_vertices},
        'extrema': {'boundsBlenderWorld': global_bounds,
                    'triangleArea': {'min': min(areas) if areas else None,
                                     'max': max(areas) if areas else None, 'total': sum(areas)},
                    'nonzeroSurfaceEdgeLength': {'min': min(edge_lengths) if edge_lengths else None,
                                                 'max': max(edge_lengths) if edge_lengths else None},
                    'nearZeroAreaThreshold': near_zero_threshold},
        'componentSummary': {'largestVerticesFraction': max((item['vertices'] for item in components), default=0) / len(positions),
                             'largestTrianglesFraction': components[0]['triangles'] / len(valid_triangles) if valid_triangles else 0,
                             'componentsWithAtMost10Triangles': sum(item['triangles'] <= 10 for item in components),
                             'componentsWithAtMost100Triangles': sum(item['triangles'] <= 100 for item in components),
                             'allComponentTriangleCounts': [item['triangles'] for item in components]},
        'largestComponents': components[:args.largest_components],
        'elapsedSeconds': time.monotonic() - started,
    }
    destination.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print('TOPOLOGY_RESULT ' + str(destination), flush=True)
    print(json.dumps(report['counts']), flush=True)


if __name__ == '__main__':
    main()

from array import array
import itertools
from functools import reduce
import numpy as np

from OCP.gp import gp_Vec, gp_Pnt

from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepGProp import BRepGProp_Face
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools

from OCP.GCPnts import (
    GCPnts_QuasiUniformDeflection,
    GCPnts_UniformAbscissa,
    GCPnts_UniformDeflection,
)

from OCP.TopAbs import (
    TopAbs_ShapeEnum,
    TopAbs_Orientation,
    TopAbs_VERTEX,
    TopAbs_EDGE,
    TopAbs_FACE,
)
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS, TopoDS_Shape, TopoDS_Compound, TopoDS_Solid
from OCP.TopAbs import TopAbs_FACE

from OCP.TopExp import TopExp_Explorer

from OCP.TopLoc import TopLoc_Location

from OCP.StlAPI import StlAPI_Writer

from cadquery.occ_impl.shapes import downcast
from .utils import distance, Timer

from cadquery.occ_impl.shapes import Compound, Shape
from cadquery.occ_impl.geom import BoundBox

HASH_CODE_MAX = 2147483647


class BoundingBox(object):
    def __init__(self, objects, optimal=False, tol=1e-5):
        self.optimal = optimal
        self.tol = tol
        bbox = reduce(self._opt, [self.bbox(obj) for obj in objects])
        self.xmin, self.xmax, self.ymin, self.ymax, self.zmin, self.zmax = bbox
        self._calc()

    def _calc(self):
        self.xsize = self.xmax - self.xmin
        self.ysize = self.ymax - self.ymin
        self.zsize = self.zmax - self.zmin
        self.center = (
            self.xmin + self.xsize / 2.0,
            self.ymin + self.ysize / 2.0,
            self.zmin + self.zsize / 2.0,
        )
        self.max = max([abs(x) for x in (self.xmin, self.xmax, self.ymin, self.ymax, self.zmin, self.zmax)])

    def max_dist_from_center(self):
        return max(
            [
                distance(self.center, v)
                for v in itertools.product((self.xmin, self.xmax), (self.ymin, self.ymax), (self.zmin, self.zmax))
            ]
        )

    def max_dist_from_origin(self):
        return max(
            [
                np.linalg.norm(v)
                for v in itertools.product((self.xmin, self.xmax), (self.ymin, self.ymax), (self.zmin, self.zmax))
            ]
        )

    def _opt(self, b1, b2):
        return (
            min(b1[0], b2[0]),
            max(b1[1], b2[1]),
            min(b1[2], b2[2]),
            max(b1[3], b2[3]),
            min(b1[4], b2[4]),
            max(b1[5], b2[5]),
        )

    def update(self, bb):
        self.xmin = min(bb.xmin, self.xmin)
        self.xmax = max(bb.xmax, self.xmax)
        self.ymin = min(bb.ymin, self.ymin)
        self.ymax = max(bb.ymax, self.ymax)
        self.zmin = min(bb.zmin, self.zmin)
        self.zmax = max(bb.zmax, self.zmax)
        self._calc()

    def _bounding_box(self, obj, tol=1e-5):
        bbox = Bnd_Box()
        if self.optimal:
            BRepTools.Clean_s(obj)
            BRepBndLib.AddOptimal_s(obj, bbox)
        else:
            BRepBndLib.Add_s(obj, bbox)
        values = bbox.Get()
        return (values[0], values[3], values[1], values[4], values[2], values[5])

    def bbox(self, objects):
        bb = reduce(self._opt, [self._bounding_box(obj) for obj in objects])
        return bb

    def is_empty(self, eps=0.01):
        return (
            (abs(self.xmax - self.xmin) < 0.01)
            and (abs(self.ymax - self.ymin) < 0.01)
            and (abs(self.zmax - self.zmin) < 0.01)
        )

    def __repr__(self):
        return "[x(%f .. %f), y(%f .. %f), z(%f .. %f)]" % (
            self.xmin,
            self.xmax,
            self.ymin,
            self.ymax,
            self.zmin,
            self.zmax,
        )


# Tessellate and discretize functions


def tessellate(shape, quality: float, angular_tolerance: float = 0.1, debug=False):
    mesh = Timer(debug, "| | | | Incremental mesh")
    # Remove previous mesh data
    BRepTools.Clean_s(shape)
    BRepMesh_IncrementalMesh(shape, quality, False, angular_tolerance, True)
    mesh.stop()

    vertices = array("f")
    triangles = array("f")
    normals = array("f")

    # global buffers
    p_buf = gp_Pnt()
    n_buf = gp_Vec()
    loc_buf = TopLoc_Location()

    offset = -1

    # every line below is selected for performance. Do not introduce functions to "beautify" the code

    values = Timer(debug, "| | | | nodes, normals")
    for face in get_faces(shape):
        if face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED:
            i1, i2 = 2, 1
        else:
            i1, i2 = 1, 2

        internal = face.Orientation() == TopAbs_Orientation.TopAbs_INTERNAL

        poly = BRep_Tool.Triangulation_s(face, loc_buf)
        if poly is not None:
            Trsf = loc_buf.Transformation()

            # add vertices
            # [node.Transformed(Trsf).Coord() for node in poly.Nodes()] is 5-8 times slower!
            items = poly.Nodes()
            coords = [items.Value(i).Transformed(Trsf).Coord() for i in range(items.Lower(), items.Upper() + 1)]
            flat = []
            for coord in coords:
                flat += coord
            vertices.extend(flat)

            # add triangles
            items = poly.Triangles()
            coords = [items.Value(i).Get() for i in range(items.Lower(), items.Upper() + 1)]
            flat = []
            for coord in coords:
                flat += (coord[0] + offset, coord[i1] + offset, coord[i2] + offset)
            triangles.extend(flat)

            # add normals
            if poly.HasUVNodes():
                prop = BRepGProp_Face(face)
                items = poly.UVNodes()

                def extract(uv0, uv1):
                    prop.Normal(uv0, uv1, p_buf, n_buf)
                    return n_buf.Reverse().Coord() if internal else n_buf.Coord()

                uvs = [items.Value(i).Coord() for i in range(items.Lower(), items.Upper() + 1)]
                flat = []
                for uv1, uv2 in uvs:
                    flat += extract(uv1, uv2)
                normals.extend(flat)

            offset += poly.NbNodes()

    values.stop()

    # Remove mesh data again
    BRepTools.Clean_s(shape)

    return (
        np.asarray(vertices, dtype=np.float32).reshape(-1, 3),
        np.asarray(triangles, dtype=np.uint32),
        np.asarray(normals, dtype=np.float32).reshape(-1, 3),
    )


def discretize_edge(edge, deflection=0.1):
    curve_adaptator = BRepAdaptor_Curve(edge)

    discretizer = GCPnts_QuasiUniformDeflection()
    discretizer.Initialize(
        curve_adaptator, deflection, curve_adaptator.FirstParameter(), curve_adaptator.LastParameter()
    )

    if not discretizer.IsDone():
        raise AssertionError("Discretizer not done.")

    points = [curve_adaptator.Value(discretizer.Parameter(i)).Coord() for i in range(1, discretizer.NbPoints() + 1)]

    # return tuples representing the single lines of the eged
    edges = []
    for i in range(len(points) - 1):
        edges.append((points[i], points[i + 1]))
    return edges


# Export STL


def write_stl_file(compound, filename, tolerance=None, angular_tolerance=None):

    # Remove previous mesh data
    BRepTools.Clean_s(compound)

    mesh = BRepMesh_IncrementalMesh(compound, tolerance, True, angular_tolerance)
    mesh.Perform()

    writer = StlAPI_Writer()

    result = writer.Write(compound, filename)

    # Remove the mesh data again
    BRepTools.Clean_s(compound)
    return result


# OCP types and accessors

# Source pythonocc-core: Extend/TopologyUtils.py
def is_vertex(topods_shape):
    if not hasattr(topods_shape, "ShapeType"):
        return False
    return topods_shape.ShapeType() == TopAbs_VERTEX


# Source pythonocc-core: Extend/TopologyUtils.py
def is_edge(topods_shape):
    if not hasattr(topods_shape, "ShapeType"):
        return False
    return topods_shape.ShapeType() == TopAbs_EDGE


def is_compound(topods_shape):
    return isinstance(topods_shape, TopoDS_Compound)


def is_solid(topods_shape):
    return isinstance(topods_shape, TopoDS_Solid)


def is_shape(topods_shape):
    return isinstance(topods_shape, TopoDS_Shape)


def _get_topo(shape, topo):
    explorer = TopExp_Explorer(shape, topo)
    hashes = {}
    while explorer.More():
        item = explorer.Current()
        hash = item.HashCode(HASH_CODE_MAX)
        if hashes.get(hash) is None:
            hashes[hash] = True
            yield downcast(item)
        explorer.Next()


def get_faces(shape):
    return _get_topo(shape, TopAbs_FACE)


def get_edges(shape):
    return _get_topo(shape, TopAbs_EDGE)


def get_point(vertex):
    p = BRep_Tool.Pnt_s(vertex)
    return (p.X(), p.Y(), p.Z())


def tq(loc):
    T = loc.wrapped.Transformation()
    t = T.Transforms()
    q = T.GetRotation()
    return (t, (q.X(), q.Y(), q.Z(), q.W()))


def get_rgb(color):
    if color is None:
        return (176, 176, 176)
    rgb = color.wrapped.GetRGB()
    return (int(255 * rgb.Red()), int(255 * rgb.Green()), int(255 * rgb.Blue()))

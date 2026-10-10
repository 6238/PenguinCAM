"""The plate: the six orientations a part can be printed in. Flask-free, no app imports.

Each orientation is named by the part axis that ends up pointing up. The matrices are
row-major rotations applied to the part's vertices (v' = M v).
"""

ORIENTATIONS = ('+z', '-z', '+x', '-x', '+y', '-y')

ORIENTATION_MATRICES = {
    '+z': ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
    '-z': ((1, 0, 0), (0, -1, 0), (0, 0, -1)),
    '+x': ((0, 0, -1), (0, 1, 0), (1, 0, 0)),
    '-x': ((0, 0, 1), (0, 1, 0), (-1, 0, 0)),
    '+y': ((1, 0, 0), (0, 0, -1), (0, 1, 0)),
    '-y': ((1, 0, 0), (0, 0, 1), (0, -1, 0)),
}

"""Bounded planar frame geometry, in CSS viewport coordinates."""

from __future__ import annotations

import math

IDENTITY = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)


def project(matrix, x, y):
    z = matrix[6] * x + matrix[7] * y + matrix[8]
    if not math.isfinite(z) or z <= 1e-9:
        raise ValueError("Frame point crosses the perspective horizon")
    result = (
        (matrix[0] * x + matrix[1] * y + matrix[2]) / z,
        (matrix[3] * x + matrix[4] * y + matrix[5]) / z,
    )
    if not all(math.isfinite(v) and abs(v) <= 1e7 for v in result):
        raise ValueError("Frame projection exceeds the geometry budget")
    return result


def compose(left, right):
    return tuple(
        sum(left[3 * r + k] * right[3 * k + c] for k in range(3))
        for r in range(3)
        for c in range(3)
    )


def inverse(m):
    a, b, c, d, e, f, g, h, i = m
    adj = (
        e * i - f * h,
        c * h - b * i,
        b * f - c * e,
        f * g - d * i,
        a * i - c * g,
        c * d - a * f,
        d * h - e * g,
        b * g - a * h,
        a * e - b * d,
    )
    determinant = a * adj[0] + b * adj[3] + c * adj[6]
    if not math.isfinite(determinant) or abs(determinant) < 1e-12:
        raise ValueError("Frame projection is singular")
    return tuple(v / determinant for v in adj)


def frame_matrix(width, height, quad):
    if (
        len(quad) != 8
        or not all(math.isfinite(v) for v in (*quad, width, height))
        or width <= 0
        or height <= 0
    ):
        raise ValueError("Frame has no usable geometry")
    points = list(zip(quad[::2], quad[1::2]))
    crosses = []
    for n in range(4):
        p, q, r = (points[(n + k) % 4] for k in range(3))
        crosses.append((q[0] - p[0]) * (r[1] - q[1]) - (q[1] - p[1]) * (r[0] - q[0]))
    if not (all(v > 1e-6 for v in crosses) or all(v < -1e-6 for v in crosses)):
        raise ValueError("Frame quad is collapsed or crosses itself")
    x0, y0, x1, y1, x2, y2, x3, y3 = quad
    dx, dy = x0 - x1 + x2 - x3, y0 - y1 + y2 - y3
    g = h = 0.0
    if abs(dx) + abs(dy) > 1e-9:
        ax, ay, bx, by = x1 - x2, y1 - y2, x3 - x2, y3 - y2
        determinant = ax * by - bx * ay
        if abs(determinant) < 1e-12:
            raise ValueError("Frame projection is singular")
        g, h = (dx * by - bx * dy) / determinant, (ax * dy - dx * ay) / determinant
    matrix = (
        (x1 - x0 + g * x1) / width,
        (x3 - x0 + h * x3) / height,
        x0,
        (y1 - y0 + g * y1) / width,
        (y3 - y0 + h * y3) / height,
        y0,
        g / width,
        h / height,
        1.0,
    )
    for x, y in ((0, 0), (width, 0), (width, height), (0, height)):
        project(matrix, x, y)
    inverse(matrix)
    return matrix


def region(matrix, rect):
    x, y, w, h = (rect[k] for k in ("x", "y", "width", "height"))
    if w <= 0 or h <= 0:
        raise ValueError("Frame region has no area")
    points = [
        project(matrix, *p) for p in ((x, y), (x + w, y), (x + w, y + h), (x, y + h))
    ]
    left, top = min(p[0] for p in points), min(p[1] for p in points)
    return {
        "x": left,
        "y": top,
        "width": max(p[0] for p in points) - left,
        "height": max(p[1] for p in points) - top,
    }

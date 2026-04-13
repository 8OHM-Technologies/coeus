import math
from typing import List, Tuple


def get_intersection_over_cell_area(
    box: List[float], cell: Tuple[float, float, float, float]
) -> float:
    """
    Calculates what percentage of a grid cell is covered by the bounding box.
    Returns a float between 0.0 and 1.0.
    """
    b_xmin, b_ymin, b_xmax, b_ymax = box
    c_xmin, c_ymin, c_xmax, c_ymax = cell

    # Determine the coordinates of the intersection rectangle
    x_left = max(b_xmin, c_xmin)
    y_top = max(b_ymin, c_ymin)
    x_right = min(b_xmax, c_xmax)
    y_bottom = min(b_ymax, c_ymax)

    # If the bounding boxes do not intersect, the area is 0
    if x_right < x_left or y_bottom < y_top:
        return 0.0

    intersection_area = (x_right - x_left) * (y_bottom - y_top)
    cell_area = (c_xmax - c_xmin) * (c_ymax - c_ymin)

    return intersection_area / cell_area


def get_grid_indexes(
    bounding_boxes: List[List[float]],
    image_width: float = 380.0,
    image_height: float = 380.0,
    grid_count: int = 9,
    overlap_threshold: float = 0.35,
) -> List[int]:
    """
    Converts a list of bounding boxes [xmin, ymin, xmax, ymax] into a list
    of integer indexes corresponding to the grid cells.
    """
    if not bounding_boxes:
        return []

    # hCaptcha usually uses 3x3 (9) or 4x4 (16) grids.
    columns = int(math.sqrt(grid_count))
    cell_width = image_width / columns
    cell_height = image_height / columns

    selected_indexes = set()

    # Pre-calculate the bounding boxes of all the grid cells
    cells = []
    for index in range(grid_count):
        row = index // columns
        col = index % columns
        c_xmin = col * cell_width
        c_ymin = row * cell_height
        cells.append((c_xmin, c_ymin, c_xmin + cell_width, c_ymin + cell_height))

    for box in bounding_boxes:
        xmin, ymin, xmax, ymax = box

        # --- Strategy 1: Center Point Selection ---
        # Guarantees we click the core of the detected object
        center_x = (xmin + xmax) / 2
        center_y = (ymin + ymax) / 2

        col = int(center_x // cell_width)
        row = int(center_y // cell_height)
        center_index = (row * columns) + col

        if 0 <= center_index < grid_count:
            selected_indexes.add(center_index)

        # --- Strategy 2: Overlap Selection ---
        # For large objects (like a bus or train) spanning multiple cells.
        # If the bounding box covers more than X% of a cell, we click it.
        for index, cell_box in enumerate(cells):
            overlap_ratio = get_intersection_over_cell_area(box, cell_box)
            if overlap_ratio >= overlap_threshold:
                selected_indexes.add(index)

    return sorted(list(selected_indexes))

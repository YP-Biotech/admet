"""
Droplet Detection Module - Simplified
Handles contour detection and filtering
"""

import cv2
import numpy as np
from typing import List, Tuple, Dict
from .logger import get_logger

logger = get_logger("Detector")


class DropletDetector:
    """Detects droplets using contour analysis"""

    def __init__(self, config: dict = None):
        default_config = {
            "closing_t": 2,
            "closing_e": 1,
            "min_area": 50,
            "max_area": 10000,
            "min_circularity": 0.3,
            "max_aspect_ratio": 4.0,
            "no_edge": False,
        }

        self.config = default_config
        if config:
            self.config.update(config)

        logger.debug("DropletDetector initialized")

    def detect(self, binary: np.ndarray) -> Tuple[List, List[Dict]]:
        """
        Detect droplets in binary image

        Args:
            binary: Binary image

        Returns:
            Tuple of (contours, properties)
        """
        contours = self.find_raw_contours(binary)

        logger.debug(f"Found {len(contours)} raw contours")

        # Filter
        filtered = self._filter_contours(contours, binary.shape)

        logger.debug(f"Filtered to {len(filtered)} droplets")

        # Calculate properties
        properties = [self._get_properties(c) for c in filtered]

        return filtered, properties

    def find_raw_contours(self, binary: np.ndarray) -> List:
        processed = self._apply_morphology(binary)
        contours, _ = cv2.findContours(
            processed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        return contours

    def _apply_morphology(self, binary: np.ndarray) -> np.ndarray:
        """Apply morphological operations"""
        result = binary.copy()

        # Closing
        if self.config["closing_t"] > 0:
            k_size = 2 * self.config["closing_t"] + 1
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_size, k_size))
            result = cv2.morphologyEx(result, cv2.MORPH_CLOSE, kernel)

        # Erosion
        if self.config["closing_e"] > 0:
            k_size = 2 * self.config["closing_e"] + 1
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_size, k_size))
            result = cv2.erode(result, kernel)

        return result

    def _filter_contours(self, contours: List, img_shape: Tuple) -> List:
        """Filter contours by size and shape"""
        h, w = img_shape[:2]
        filtered = []

        for contour in contours:
            area = cv2.contourArea(contour)

            # Area filter
            if area < self.config["min_area"] or area > self.config["max_area"]:
                continue

            # Circularity filter
            perimeter = cv2.arcLength(contour, True)
            if perimeter > 0:
                circularity = 4 * np.pi * area / (perimeter**2)
                if circularity < self.config["min_circularity"]:
                    continue

            x, y, bw, bh = cv2.boundingRect(contour)
            aspect_ratio = max(bw / max(bh, 1), bh / max(bw, 1))
            if aspect_ratio > self.config["max_aspect_ratio"]:
                continue

            # Edge filter
            if self.config["no_edge"]:
                if x <= 0 or y <= 0 or x + bw >= w or y + bh >= h:
                    continue

            filtered.append(contour)

        return filtered

    def _get_properties(self, contour) -> Dict:
        """Calculate basic properties for a contour"""
        area = cv2.contourArea(contour)
        perimeter = cv2.arcLength(contour, True)

        # Centroid
        M = cv2.moments(contour)
        if M["m00"] != 0:
            cx = M["m10"] / M["m00"]
            cy = M["m01"] / M["m00"]
        else:
            cx, cy = 0, 0

        # Bounding box
        x, y, w, h = cv2.boundingRect(contour)

        # Circularity
        circularity = 4 * np.pi * area / (perimeter**2) if perimeter > 0 else 0

        # Equivalent diameter
        diameter = np.sqrt(4 * area / np.pi)

        return {
            "area": area,
            "perimeter": perimeter,
            "centroid_x": cx,
            "centroid_y": cy,
            "bbox_x": x,
            "bbox_y": y,
            "bbox_width": w,
            "bbox_height": h,
            "circularity": circularity,
            "equivalent_diameter": diameter,
        }


def separate_touching(binary: np.ndarray, relief: np.ndarray, *, max_area: float, gap: int,
                      min_seed: int = 20) -> np.ndarray:
    """Packed droplets as separate shapes, each grown from the middle of its outline.

    Against an empty-channel background a droplet shows as a closed outline whose middle
    looks like the channel. Each middle the outline encloses seeds a watershed over the
    relief, so the line between two touching droplets falls on the outlines they share.
    The lines are drawn gap wide, so the closing that follows cannot join them again.
    """
    ellipse = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    closed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, ellipse)
    # Padded with background, so everything reachable from the edge is outside.
    padded = cv2.copyMakeBorder(closed, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
    mask = np.zeros((padded.shape[0] + 2, padded.shape[1] + 2), np.uint8)
    cv2.floodFill(padded, mask, (0, 0), 255)
    middles = cv2.bitwise_not(padded)[1:-1, 1:-1]

    count, labels, stats, _ = cv2.connectedComponentsWithStats(middles)
    markers = np.zeros(binary.shape, np.int32)
    seeds = 0
    for label in range(1, count):
        if min_seed <= stats[label, cv2.CC_STAT_AREA] <= max_area:
            seeds += 1
            markers[labels == label] = seeds + 1
    if not seeds:
        return cv2.bitwise_or(closed, middles)
    markers[(closed == 0) & (middles == 0)] = 1
    cv2.watershed(cv2.cvtColor(cv2.GaussianBlur(relief, (5, 5), 1.5), cv2.COLOR_GRAY2BGR), markers)

    shapes = np.where(markers > 1, 255, 0).astype(np.uint8)
    lines = np.where(markers == -1, 255, 0).astype(np.uint8)
    lines[0, :] = lines[-1, :] = lines[:, 0] = lines[:, -1] = 0       # the watershed also marks the frame's edge
    lines = cv2.dilate(lines, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (gap, gap)))
    shapes[lines > 0] = 0
    return shapes

"""
Chipload-based feeds and speeds calculation for CNC machining.

This module provides physics-based calculation of feeds and speeds based on
ground-truth cutting parameters (chipload, RPM, tool geometry) rather than
arbitrary feed rates. This ensures settings scale correctly across different
tool sizes and flute counts.

Key concepts:
- Chipload: Material removed per tooth per revolution (in/tooth)
- Feed rate = RPM × flutes × diameter × chipload
- Settings scale with tool diameter using power law
"""

from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass
import math


@dataclass
class ChiploadParams:
    """Ground-truth cutting parameters for a material."""
    preferred_rpm: int
    chipload_ref: float  # in/tooth at reference tool
    chipload_min: float
    chipload_max: float
    slotting_multiplier: float = 1.0


@dataclass
class MachineLimits:
    """Physical limits of the CNC machine."""
    rpm_min: int
    rpm_max: int
    feed_xy_max: float  # IPM
    feed_z_max: float   # IPM


@dataclass
class OperationResult:
    """Result of feed/speed calculation for one operation."""
    operation_type: str
    rpm: int
    feed_xy: float
    feed_z: float  # For pecking/plunging
    actual_chipload: float
    target_chipload: float
    status: str  # 'ok', 'low', 'high', 'clamped_feed', 'clamped_rpm'
    warnings: List[str]

    def is_healthy(self) -> bool:
        """Check if operation is within safe parameters."""
        return self.status == 'ok'


class ChiploadCalculator:
    """
    Pure calculation engine for chipload-based feeds and speeds.

    All methods are stateless - pass in parameters, get results back.
    This makes the calculations easy to test and reuse.
    """

    @staticmethod
    def calculate_chipload(rpm: int, feed: float, diameter: float, flutes: int) -> float:
        """
        Calculate actual chipload from operational parameters.

        Formula: chipload = feed / (rpm × flutes)
        Note: This is the simplified version. Full version accounts for diameter
        in the feed calculation, but for calculating what you're actually getting,
        this is the relationship.

        Args:
            rpm: Spindle speed (RPM)
            feed: Feed rate (IPM)
            diameter: Tool diameter (inches)
            flutes: Number of flutes

        Returns:
            Chipload in inches per tooth
        """
        if rpm == 0 or flutes == 0:
            return 0.0
        # Actual formula when not considering engagement:
        # chipload = feed / (rpm × flutes)
        # But we're being more precise here for display purposes
        return feed / (rpm * flutes)

    @staticmethod
    def calculate_feed(rpm: int, chipload: float, diameter: float, flutes: int) -> float:
        """
        Calculate feed rate from chipload and tool parameters.

        Formula: feed = rpm × flutes × chipload

        Args:
            rpm: Spindle speed (RPM)
            chipload: Target chipload (in/tooth)
            diameter: Tool diameter (inches)
            flutes: Number of flutes

        Returns:
            Feed rate in IPM
        """
        return rpm * flutes * chipload

    @staticmethod
    def scale_chipload_by_diameter(
        chipload_ref: float,
        diameter_ref: float,
        diameter: float,
        exponent: float = 0.70
    ) -> float:
        """
        Scale chipload for different tool diameter using power law.

        Larger tools can handle proportionally larger chiploads, but not
        linearly. The relationship follows a power law:

        chipload(D) = chipload_ref × (D / D_ref)^exponent

        Typical exponent is 0.70 - this means a 2x larger tool can handle
        about 1.6x the chipload (not 2x).

        Args:
            chipload_ref: Reference chipload (in/tooth)
            diameter_ref: Reference tool diameter (inches)
            diameter: Actual tool diameter (inches)
            exponent: Scaling exponent (default 0.70)

        Returns:
            Scaled chipload for the actual tool diameter
        """
        if diameter_ref == 0:
            return chipload_ref

        ratio = diameter / diameter_ref
        return chipload_ref * (ratio ** exponent)

    @staticmethod
    def clamp_value(value: float, min_val: float, max_val: float) -> Tuple[float, bool]:
        """
        Clamp value to range, returning both clamped value and whether it was clamped.

        Args:
            value: Value to clamp
            min_val: Minimum allowed value
            max_val: Maximum allowed value

        Returns:
            Tuple of (clamped_value, was_clamped)
        """
        if value < min_val:
            return min_val, True
        elif value > max_val:
            return max_val, True
        else:
            return value, False

    @classmethod
    def calculate_operation(
        cls,
        operation_type: str,
        tool_diameter: float,
        tool_flutes: int,
        diameter_ref: float,
        chipload_params: ChiploadParams,
        machine_limits: MachineLimits,
        chipload_exponent: float = 0.70,
        allow_rpm_reduction: bool = True,
        ramp_feed_multiplier: float = 0.65,
        peck_feed_multiplier: float = 0.15
    ) -> OperationResult:
        """
        Calculate all feeds and speeds for one operation.

        This is the main calculation engine that implements the chipload-based
        approach. It:
        1. Scales reference chipload for tool diameter
        2. Applies operation-specific multipliers (slotting, etc)
        3. Calculates ideal feed rate
        4. Clamps to machine limits
        5. Optionally reduces RPM to maintain minimum chipload
        6. Generates warnings and recommendations

        Args:
            operation_type: 'slotting', 'clearing', 'ramp', or 'peck_drill'
            tool_diameter: Tool diameter (inches)
            tool_flutes: Number of flutes
            diameter_ref: Reference tool diameter (inches)
            chipload_params: Material cutting parameters
            machine_limits: Machine physical limits
            chipload_exponent: Diameter scaling exponent
            allow_rpm_reduction: Whether to reduce RPM if feed is clamped
            ramp_feed_multiplier: Feed multiplier for ramping (0-1)
            peck_feed_multiplier: Feed multiplier for pecking (0-1)

        Returns:
            OperationResult with all calculated parameters and status
        """
        warnings = []
        status = 'ok'

        # Step 1: Scale chipload for tool diameter
        target_chipload = cls.scale_chipload_by_diameter(
            chipload_params.chipload_ref,
            diameter_ref,
            tool_diameter,
            chipload_exponent
        )

        # Step 2: Apply operation-specific multipliers
        if operation_type == 'slotting':
            target_chipload *= chipload_params.slotting_multiplier

        # Step 3: Clamp chipload to material limits
        target_chipload, chipload_was_clamped = cls.clamp_value(
            target_chipload,
            chipload_params.chipload_min,
            chipload_params.chipload_max
        )

        if chipload_was_clamped:
            warnings.append(f"Target chipload adjusted to material limits")

        # Step 4: Calculate ideal feed at preferred RPM
        rpm = chipload_params.preferred_rpm
        ideal_feed = cls.calculate_feed(rpm, target_chipload, tool_diameter, tool_flutes)

        # Step 5: Clamp feed to machine limits
        feed_xy, feed_was_clamped = cls.clamp_value(
            ideal_feed,
            0,
            machine_limits.feed_xy_max
        )

        # Step 6: Calculate actual chipload after feed clamping
        actual_chipload = cls.calculate_chipload(rpm, feed_xy, tool_diameter, tool_flutes)

        # Step 7: Check if we need to reduce RPM to maintain minimum chipload
        rpm_was_reduced = False
        if feed_was_clamped and actual_chipload < chipload_params.chipload_min and allow_rpm_reduction:
            # Calculate RPM needed to maintain minimum chipload
            required_rpm = feed_xy / (tool_flutes * chipload_params.chipload_min)
            new_rpm = max(int(required_rpm), machine_limits.rpm_min)

            if new_rpm < rpm:
                rpm = new_rpm
                actual_chipload = cls.calculate_chipload(rpm, feed_xy, tool_diameter, tool_flutes)
                rpm_was_reduced = True
                warnings.append(f"RPM reduced to {rpm} to maintain minimum chipload")
                status = 'clamped_rpm'

        # Step 8: Determine overall status
        if not rpm_was_reduced:
            if actual_chipload < chipload_params.chipload_min:
                status = 'low'
                warnings.append(
                    f"Chipload {actual_chipload:.4f} in/tooth below minimum "
                    f"{chipload_params.chipload_min:.4f} - rubbing risk"
                )
            elif actual_chipload > chipload_params.chipload_max:
                status = 'high'
                warnings.append(
                    f"Chipload {actual_chipload:.4f} in/tooth exceeds maximum "
                    f"{chipload_params.chipload_max:.4f} - breakage risk"
                )
            elif feed_was_clamped:
                status = 'clamped_feed'
                warnings.append(
                    f"Feed limited by machine ({machine_limits.feed_xy_max:.1f} IPM max)"
                )

        # Step 9: Calculate operation-specific feed rates
        if operation_type == 'ramp':
            feed_z = feed_xy * ramp_feed_multiplier
        elif operation_type == 'peck_drill':
            feed_z = min(feed_xy * peck_feed_multiplier, machine_limits.feed_z_max)
        else:
            feed_z = min(feed_xy, machine_limits.feed_z_max)  # Default Z feed

        return OperationResult(
            operation_type=operation_type,
            rpm=rpm,
            feed_xy=feed_xy,
            feed_z=feed_z,
            actual_chipload=actual_chipload,
            target_chipload=target_chipload,
            status=status,
            warnings=warnings
        )

    @staticmethod
    def generate_recommendations(
        result: OperationResult,
        tool_flutes: int
    ) -> List[str]:
        """
        Generate actionable recommendations for non-optimal cutting conditions.

        Recommendations are always safe and technically valid, but may not
        always be practical (e.g., "use larger tool" when features don't allow it).

        Args:
            result: Operation result to analyze
            tool_flutes: Number of flutes on current tool

        Returns:
            List of recommendation strings
        """
        recommendations = []

        if result.status == 'low':
            # Chipload too low (rubbing risk)
            suggestions = []
            if tool_flutes > 1:
                suggestions.append("fewer flutes")
            suggestions.append("larger tool diameter (if part features allow)")
            suggestions.append("lower RPM")

            recommendations.append(
                f"💡 To increase chipload: Consider {' or '.join(suggestions)}"
            )

        elif result.status == 'high':
            # Chipload too high (breakage risk)
            suggestions = ["smaller tool diameter", "more flutes", "higher RPM"]
            recommendations.append(
                f"💡 To reduce chipload: Consider {' or '.join(suggestions)}"
            )

        elif result.status == 'clamped_feed':
            # Feed limited by machine
            recommendations.append(
                "💡 Machine feed limit reached. Cutting at safe chipload but slower than ideal."
            )

        elif result.status == 'clamped_rpm':
            # RPM was automatically reduced
            recommendations.append(
                f"✅ RPM automatically reduced to maintain safe chipload"
            )

        return recommendations

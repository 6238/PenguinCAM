"""
Diagnostics and reporting for chipload-based machining operations.

Tracks operation results and generates user-friendly health reports that
help students understand cutting conditions without needing to learn machining theory.
"""

from typing import List, Dict, Any
from chipload_calculator import OperationResult, ChiploadCalculator


class ChiploadDiagnostics:
    """
    Tracks machining operation results and generates health reports.

    This class collects results from multiple operations (perimeters, pockets,
    holes, etc.) and rolls them up into an overall health assessment with
    actionable recommendations.
    """

    def __init__(self):
        """Initialize empty diagnostics collector."""
        self.operations: List[OperationResult] = []
        self.tool_flutes: int = 1  # Track for recommendations

    def record_operation(
        self,
        result: OperationResult,
        tool_flutes: int
    ) -> None:
        """
        Record the result of one machining operation.

        Args:
            result: Calculated operation result
            tool_flutes: Number of flutes (for generating recommendations)
        """
        self.operations.append(result)
        self.tool_flutes = tool_flutes

    def get_overall_status(self) -> str:
        """
        Determine overall health status across all operations.

        Returns:
            'ok' - All operations within safe parameters
            'warning' - Some operations have chipload concerns
            'info' - Operations adjusted but safe
        """
        if not self.operations:
            return 'ok'

        # Check for safety concerns (low/high chipload)
        if any(op.status in ['low', 'high'] for op in self.operations):
            return 'warning'

        # Check for informational status (clamped but safe)
        if any(op.status in ['clamped_feed', 'clamped_rpm'] for op in self.operations):
            return 'info'

        return 'ok'

    def get_summary_message(self) -> str:
        """
        Generate a concise summary message for the overall status.

        Returns:
            User-friendly summary string
        """
        status = self.get_overall_status()

        if status == 'ok':
            return "✅ All operations within safe parameters"
        elif status == 'warning':
            return "⚠️ Chipload concerns detected"
        else:  # info
            return "ℹ️ Operations adjusted for machine limits"

    def get_operation_details(self) -> List[Dict[str, Any]]:
        """
        Get detailed information about each operation for UI display.

        Returns:
            List of operation detail dicts with formatted strings
        """
        details = []

        for op in self.operations:
            # Format operation type name nicely
            type_names = {
                'slotting': 'Perimeters',
                'clearing': 'Pockets',
                'peck_drill': 'Holes',
                'ramp': 'Ramp Entry'
            }
            display_name = type_names.get(op.operation_type, op.operation_type.title())

            # Generate status icon
            icons = {
                'ok': '✅',
                'low': '⚠️',
                'high': '⚠️',
                'clamped_feed': 'ℹ️',
                'clamped_rpm': '✅'
            }
            icon = icons.get(op.status, '❓')

            # Generate main status line
            status_line = f"{display_name}: {op.actual_chipload:.4f} in/tooth @ {op.rpm} RPM {icon}"

            # Get recommendations
            recommendations = ChiploadCalculator.generate_recommendations(op, self.tool_flutes)

            details.append({
                'operation_type': op.operation_type,
                'display_name': display_name,
                'status': op.status,
                'icon': icon,
                'status_line': status_line,
                'chipload': op.actual_chipload,
                'rpm': op.rpm,
                'feed_xy': op.feed_xy,
                'warnings': op.warnings,
                'recommendations': recommendations
            })

        return details

    def to_dict(self) -> Dict[str, Any]:
        """
        Serialize diagnostics to dictionary for JSON API responses.

        Returns:
            Dictionary with overall status, summary, and operation details
        """
        return {
            'overall_status': self.get_overall_status(),
            'summary_message': self.get_summary_message(),
            'operations': self.get_operation_details(),
            'total_operations': len(self.operations)
        }

    def log_summary(self, log_func=print) -> None:
        """
        Log a human-readable summary to console.

        Args:
            log_func: Function to call for logging (default: print)
        """
        log_func("\n" + "="*70)
        log_func("CHIPLOAD DIAGNOSTICS")
        log_func("="*70)
        log_func(self.get_summary_message())
        log_func("")

        for detail in self.get_operation_details():
            log_func(f"  {detail['status_line']}")

            # Show warnings if any
            for warning in detail['warnings']:
                log_func(f"    ⚠️  {warning}")

            # Show recommendations if any
            for rec in detail['recommendations']:
                log_func(f"    {rec}")

            log_func("")

        log_func("="*70 + "\n")

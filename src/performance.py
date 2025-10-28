"""
Performance profiling utilities for YK PAR Library Tool.

Usage:
    from .performance import profile, Timer
    
    @profile
    def my_function():
        # Your code here
        pass
    
    with Timer("Operation name"):
        # Code to time
        pass
"""
import time
from functools import wraps


# Global flag to enable/disable profiling output
PROFILING_ENABLED = False


def enable_profiling(enabled: bool = True) -> None:
    """Enable or disable profiling output at runtime."""
    global PROFILING_ENABLED
    PROFILING_ENABLED = enabled


def profile(func):
    """Decorator to profile function execution time.
    
    Usage:
        @profile
        def my_slow_function():
            time.sleep(1)
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not PROFILING_ENABLED:
            return func(*args, **kwargs)
        
        start = time.perf_counter()
        result = func(*args, **kwargs)
        elapsed = time.perf_counter() - start
        
        func_name = func.__name__
        print(f"[PROFILE] {func_name}: {elapsed*1000:.2f}ms")
        
        return result
    return wrapper


class Timer:
    """Context manager for timing code blocks.
    
    Usage:
        with Timer("Loading PAR file"):
            par = read_par(path)
    """
    def __init__(self, name: str = "Operation", enabled: bool = None):
        self.name = name
        self.enabled = enabled if enabled is not None else PROFILING_ENABLED
        self.start = None
        self.elapsed = None
    
    def __enter__(self):
        if self.enabled:
            self.start = time.perf_counter()
        return self
    
    def __exit__(self, *args):
        if self.enabled and self.start is not None:
            self.elapsed = time.perf_counter() - self.start
            print(f"[TIMER] {self.name}: {self.elapsed*1000:.2f}ms")
    
    def elapsed_ms(self) -> float:
        """Get elapsed time in milliseconds."""
        return self.elapsed * 1000 if self.elapsed else 0.0


class PerformanceMonitor:
    """Track cumulative performance metrics across multiple operations.
    
    Usage:
        monitor = PerformanceMonitor()
        
        for i in range(100):
            with monitor.track("parse_file"):
                parse_file(i)
        
        monitor.report()  # Print summary
    """
    def __init__(self):
        self.metrics = {}
        self.enabled = PROFILING_ENABLED
    
    def track(self, operation: str):
        """Context manager to track an operation."""
        return _OperationTracker(self, operation, self.enabled)
    
    def record(self, operation: str, elapsed: float):
        """Record a timing measurement."""
        if operation not in self.metrics:
            self.metrics[operation] = {
                'count': 0,
                'total_ms': 0.0,
                'min_ms': float('inf'),
                'max_ms': 0.0
            }
        
        m = self.metrics[operation]
        m['count'] += 1
        m['total_ms'] += elapsed * 1000
        m['min_ms'] = min(m['min_ms'], elapsed * 1000)
        m['max_ms'] = max(m['max_ms'], elapsed * 1000)
    
    def report(self):
        """Print performance summary."""
        if not self.metrics:
            print("[PERF] No metrics recorded")
            return
        
        print("\n" + "="*60)
        print("PERFORMANCE REPORT")
        print("="*60)
        
        for operation, m in sorted(self.metrics.items()):
            avg_ms = m['total_ms'] / m['count']
            print(f"\n{operation}:")
            print(f"  Count:   {m['count']}")
            print(f"  Total:   {m['total_ms']:.2f}ms")
            print(f"  Average: {avg_ms:.2f}ms")
            print(f"  Min:     {m['min_ms']:.2f}ms")
            print(f"  Max:     {m['max_ms']:.2f}ms")
        
        print("\n" + "="*60 + "\n")
    
    def clear(self):
        """Clear all metrics."""
        self.metrics.clear()


class _OperationTracker:
    """Internal context manager for PerformanceMonitor."""
    def __init__(self, monitor, operation, enabled):
        self.monitor = monitor
        self.operation = operation
        self.enabled = enabled
        self.start = None
    
    def __enter__(self):
        if self.enabled:
            self.start = time.perf_counter()
        return self
    
    def __exit__(self, *args):
        if self.enabled and self.start is not None:
            elapsed = time.perf_counter() - self.start
            self.monitor.record(self.operation, elapsed)


# Global performance monitor instance
_global_monitor = PerformanceMonitor()


def get_monitor() -> PerformanceMonitor:
    """Get the global performance monitor instance."""
    return _global_monitor


# Example usage and testing
if __name__ == '__main__':
    # Enable profiling
    enable_profiling(True)
    
    # Test decorator
    @profile
    def slow_function():
        time.sleep(0.1)
        return "done"
    
    # Test context manager
    with Timer("Test operation"):
        time.sleep(0.05)
    
    # Test performance monitor
    monitor = get_monitor()
    
    for i in range(10):
        with monitor.track("loop_operation"):
            time.sleep(0.01)
    
    monitor.report()
    
    print("\n✓ All profiling utilities working correctly")

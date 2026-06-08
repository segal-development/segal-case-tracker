"""
Structured JSON Logging for PJUD Scraper.

Provides:
- Structured JSON output compatible with cloud logging platforms
- Context propagation (user_rut, competency, operation, duration)
- @log_operation decorator for automatic entry/exit logging
- Configurable log levels via environment variable
"""

import functools
import logging
import os
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, TypeVar

# Try to import structlog, fall back to stdlib if not available
try:
    import structlog
    HAS_STRUCTLOG = True
except ImportError:
    HAS_STRUCTLOG = False


# Context variable for request-scoped logging context
_log_context: ContextVar[Dict[str, Any]] = ContextVar("log_context", default={})


@dataclass
class LogContext:
    """Context data for structured logging.
    
    Attributes:
        user_rut: The user's RUT being processed
        competency: The competency type (civil, laboral, penal)
        operation: The current operation name
        extra: Additional context fields
    """
    user_rut: Optional[str] = None
    competency: Optional[str] = None
    operation: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary, excluding None values."""
        result = {}
        if self.user_rut:
            result["user_rut"] = self.user_rut
        if self.competency:
            result["competency"] = self.competency
        if self.operation:
            result["operation"] = self.operation
        result.update(self.extra)
        return result


def get_log_level() -> int:
    """Get log level from environment variable."""
    level_name = os.environ.get("PJUD_LOG_LEVEL", "INFO").upper()
    return getattr(logging, level_name, logging.INFO)


def configure_logging(
    level: Optional[int] = None,
    json_output: bool = True,
) -> None:
    """Configure structured logging for the application.
    
    Args:
        level: Log level (default: from PJUD_LOG_LEVEL env var)
        json_output: Whether to output JSON format (default: True)
    """
    if level is None:
        level = get_log_level()
    
    if HAS_STRUCTLOG and json_output:
        # Configure structlog for JSON output
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.StackInfoRenderer(),
                structlog.dev.set_exc_info,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(level),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(),
            cache_logger_on_first_use=True,
        )
    else:
        # Fallback to stdlib logging with JSON-like format
        logging.basicConfig(
            level=level,
            format='{"timestamp": "%(asctime)s", "level": "%(levelname)s", "logger": "%(name)s", "message": "%(message)s"}',
            datefmt="%Y-%m-%dT%H:%M:%S",
            stream=sys.stdout,
        )


def get_logger(name: str = "pjud") -> Any:
    """Get a logger instance with structured logging support.
    
    Args:
        name: Logger name (default: "pjud")
    
    Returns:
        Logger instance (structlog or stdlib)
    """
    if HAS_STRUCTLOG:
        return structlog.get_logger(name)
    return logging.getLogger(name)


@contextmanager
def log_context(**kwargs):
    """Context manager for adding temporary logging context.
    
    Example:
        with log_context(user_rut="12345678-9", competency="civil"):
            logger.info("Processing user")
    """
    token = _log_context.set({**_log_context.get(), **kwargs})
    try:
        if HAS_STRUCTLOG:
            structlog.contextvars.bind_contextvars(**kwargs)
        yield
    finally:
        _log_context.reset(token)
        if HAS_STRUCTLOG:
            structlog.contextvars.unbind_contextvars(*kwargs.keys())


F = TypeVar("F", bound=Callable[..., Any])


def log_operation(
    operation_name: Optional[str] = None,
    log_args: bool = False,
    log_result: bool = False,
) -> Callable[[F], F]:
    """Decorator for automatic operation logging.
    
    Logs entry, exit, duration, and errors for decorated functions.
    Works with both sync and async functions.
    
    Args:
        operation_name: Override operation name (default: function name)
        log_args: Whether to log function arguments (default: False)
        log_result: Whether to log return value (default: False)
    
    Example:
        @log_operation()
        async def get_cases(session):
            ...
        
        @log_operation(operation_name="fetch_case_detail", log_args=True)
        async def get_detail(session, case_token):
            ...
    """
    def decorator(func: F) -> F:
        op_name = operation_name or func.__name__
        logger = get_logger(f"pjud.{op_name}")
        
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            start_time = time.time()
            
            log_data = {
                "operation": op_name,
                "status": "started",
            }
            if log_args:
                # Safely stringify args (avoid sensitive data in full)
                log_data["args_count"] = len(args)
                log_data["kwargs_keys"] = list(kwargs.keys())
            
            # Add context from context var
            log_data.update(_log_context.get())
            
            if HAS_STRUCTLOG:
                logger.info("operation_started", **log_data)
            else:
                logger.info(f"Operation started: {op_name}")
            
            try:
                result = await func(*args, **kwargs)
                
                duration_ms = int((time.time() - start_time) * 1000)
                
                success_data = {
                    "operation": op_name,
                    "status": "completed",
                    "duration_ms": duration_ms,
                }
                success_data.update(_log_context.get())
                
                if log_result and result is not None:
                    if isinstance(result, (list, tuple)):
                        success_data["result_count"] = len(result)
                    elif isinstance(result, dict):
                        success_data["result_keys"] = list(result.keys())
                
                if HAS_STRUCTLOG:
                    logger.info("operation_completed", **success_data)
                else:
                    logger.info(f"Operation completed: {op_name} ({duration_ms}ms)")
                
                return result
                
            except Exception as e:
                duration_ms = int((time.time() - start_time) * 1000)
                
                error_data = {
                    "operation": op_name,
                    "status": "failed",
                    "duration_ms": duration_ms,
                    "error_type": type(e).__name__,
                    "error_message": str(e),
                }
                error_data.update(_log_context.get())
                
                if HAS_STRUCTLOG:
                    logger.error("operation_failed", **error_data)
                else:
                    logger.error(f"Operation failed: {op_name} ({duration_ms}ms) - {e}")
                
                raise
        
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            start_time = time.time()
            
            log_data = {
                "operation": op_name,
                "status": "started",
            }
            log_data.update(_log_context.get())
            
            if HAS_STRUCTLOG:
                logger.info("operation_started", **log_data)
            else:
                logger.info(f"Operation started: {op_name}")
            
            try:
                result = func(*args, **kwargs)
                
                duration_ms = int((time.time() - start_time) * 1000)
                
                success_data = {
                    "operation": op_name,
                    "status": "completed",
                    "duration_ms": duration_ms,
                }
                success_data.update(_log_context.get())
                
                if HAS_STRUCTLOG:
                    logger.info("operation_completed", **success_data)
                else:
                    logger.info(f"Operation completed: {op_name} ({duration_ms}ms)")
                
                return result
                
            except Exception as e:
                duration_ms = int((time.time() - start_time) * 1000)
                
                error_data = {
                    "operation": op_name,
                    "status": "failed",
                    "duration_ms": duration_ms,
                    "error_type": type(e).__name__,
                    "error_message": str(e),
                }
                error_data.update(_log_context.get())
                
                if HAS_STRUCTLOG:
                    logger.error("operation_failed", **error_data)
                else:
                    logger.error(f"Operation failed: {op_name} ({duration_ms}ms) - {e}")
                
                raise
        
        # Return appropriate wrapper based on function type
        import asyncio
        if asyncio.iscoroutinefunction(func):
            return async_wrapper  # type: ignore
        return sync_wrapper  # type: ignore
    
    return decorator

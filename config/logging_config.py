import logging
import logging.handlers
import os
from datetime import datetime

# Create logs directory if it doesn't exist
LOGS_DIR = os.getenv("LOGS_DIR", "./logs")
os.makedirs(LOGS_DIR, exist_ok=True)

# Log format
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Configure root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

# File handler - rotates daily
log_file = os.path.join(LOGS_DIR, f"mcp_server_{datetime.now().strftime('%Y%m%d')}.log")
file_handler = logging.handlers.RotatingFileHandler(
    log_file,
    maxBytes=10 * 1024 * 1024,  # 10MB
    backupCount=10,  # Keep 10 backups
)
file_handler.setLevel(logging.DEBUG)
file_formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
file_handler.setFormatter(file_formatter)
root_logger.addHandler(file_handler)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
console_handler.setFormatter(console_formatter)
root_logger.addHandler(console_handler)

# Get logger
def get_logger(name: str) -> logging.Logger:
    """Get a logger instance."""
    return logging.getLogger(name)

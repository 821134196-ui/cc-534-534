"""medbox —— 养老机构分药盒管理系统（零第三方依赖）。"""

from .db import connect, init_db
from .core import MedBoxService, DomainError

__all__ = ["connect", "init_db", "MedBoxService", "DomainError"]
__version__ = "1.0.0"

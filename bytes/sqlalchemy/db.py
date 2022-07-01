import logging
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base

from bytes.config import settings

logger = logging.getLogger(__name__)

SQL_BASE = declarative_base()


@lru_cache(maxsize=None)
def get_engine() -> Engine:
    logger.info("Connecting to database..")

    engine = create_engine(settings.bytes_db_uri, pool_pre_ping=True, pool_size=25)

    logger.info("Connected to database")

    return engine

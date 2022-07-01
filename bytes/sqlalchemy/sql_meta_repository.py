import logging
import uuid
from typing import Type, List, Iterator, Optional

from fastapi import Depends
from sqlalchemy.orm import sessionmaker, Session

from bytes.config import Settings, settings
from bytes.models import (
    Boefje,
    BoefjeMeta,
    Normalizer,
    NormalizerMeta,
    RawData,
    MimeType,
    RawDataMeta,
)
from bytes.repositories.hash_repository import HashRepository
from bytes.timestamping.provider import create_hash_repository
from bytes.timestamping.hashing import hash_data
from bytes.sqlalchemy.db import SQL_BASE, get_engine
from bytes.sqlalchemy.db_models import BoefjeMetaInDB, NormalizerMetaInDB, RawFileInDB
from bytes.repositories.meta_repository import MetaDataRepository, BoefjeMetaFilter, RawDataFilter
from bytes.repositories.raw_repository import RawRepository
from bytes.raw.file_raw_repository import create_raw_repository

logger = logging.getLogger(__name__)


class SQLMetaDataRepository(MetaDataRepository):
    def __init__(
        self, session: Session, raw_repository: RawRepository, hash_repository: HashRepository, app_settings: Settings
    ):
        self.session = session
        self.raw_repository = raw_repository
        self.hash_repository = hash_repository
        self.app_settings = app_settings

    def __enter__(self) -> None:
        pass

    def __exit__(self, exc_type: Type[Exception], exc_value: str, exc_traceback: str) -> None:
        logger.info("Committing session")
        self.session.commit()
        logger.info("Committed session")

    def save_boefje_meta(self, boefje_meta: BoefjeMeta) -> None:
        logger.info("Inserting meta: %s", boefje_meta.json())

        boefje_meta_in_db = to_boefje_meta_in_db(boefje_meta)
        self.session.add(boefje_meta_in_db)

    def get_boefje_meta_by_id(self, boefje_meta_id: str) -> BoefjeMeta:
        boefje_meta_in_db = self.session.get(BoefjeMetaInDB, boefje_meta_id)

        if boefje_meta_in_db is None:
            raise ObjectNotFoundException(BoefjeMetaInDB, id=boefje_meta_id)

        return to_boefje_meta(boefje_meta_in_db)

    def get_boefje_meta(
        self,
        query_filter: BoefjeMetaFilter,
    ) -> List[BoefjeMeta]:
        logger.info("Querying boefje meta")

        query = self.session.query(BoefjeMetaInDB).filter(BoefjeMetaInDB.organization == query_filter.organization)

        if query_filter.boefje_id is not None:
            query = query.filter(BoefjeMetaInDB.boefje_id == query_filter.boefje_id)

        if query_filter.input_ooi is not None:
            query = query.filter(BoefjeMetaInDB.input_ooi == query_filter.input_ooi)

        ordering_fn = BoefjeMetaInDB.started_at.desc if query_filter.descending else BoefjeMetaInDB.started_at.asc
        query = query.order_by(ordering_fn()).limit(query_filter.limit)

        return [to_boefje_meta(boefje_meta) for boefje_meta in query]

    def save_normalizer_meta(self, normalizer_meta: NormalizerMeta) -> None:
        logger.info("Saving normalizer meta")

        normalizer_meta_in_db = to_normalizer_meta_in_db(normalizer_meta)
        self.session.add(normalizer_meta_in_db)

    def get_normalizer_meta(self, normalizer_meta_id: str) -> NormalizerMeta:
        normalizer_meta_in_db = self.session.get(NormalizerMetaInDB, normalizer_meta_id)

        if normalizer_meta_in_db is None:
            raise ObjectNotFoundException(NormalizerMetaInDB, id=normalizer_meta_id)

        return to_normalizer_meta(normalizer_meta_in_db)

    def save_raw(self, raw: RawData) -> str:
        logger.info("Saving raw")

        # Hash the data
        secure_hash = hash_data(raw, raw.boefje_meta.ended_at, self.app_settings.hashing_algorithm)

        # Send hash to a third party service.
        link = self.hash_repository.store(secure_hash=secure_hash)

        raw.secure_hash = secure_hash
        raw.hash_retrieval_link = link

        logger.info("Added hash %s and link %s to data", secure_hash, link)

        raw_file_in_db = to_raw_file_in_db(raw)

        self.session.add(raw_file_in_db)
        self.raw_repository.save_raw(raw_file_in_db.id, raw)

        return str(raw_file_in_db.id)

    def get_raws(self, query_filter: RawDataFilter) -> List[RawDataMeta]:
        if query_filter.boefje_meta_id:
            query = self.session.query(RawFileInDB).filter(RawFileInDB.boefje_meta_id == query_filter.boefje_meta_id)
        else:
            query = (
                self.session.query(RawFileInDB)
                .join(BoefjeMetaInDB)
                .filter(BoefjeMetaInDB.organization == query_filter.organization)
            )

        if query_filter.normalized:
            query = query.join(NormalizerMetaInDB, isouter=False)

        if query_filter.normalized is False:  # it can also be None, in which case we do not want a filter
            query = query.join(NormalizerMetaInDB, isouter=True).filter(NormalizerMetaInDB.id == None)

        if query_filter.mime_types:
            query = query.filter(RawFileInDB.mime_types.contains([m.value for m in query_filter.mime_types]))

        query = query.limit(query_filter.limit)

        return [self._to_raw_meta(raw_file_in_db) for raw_file_in_db in query]

    def get_raw(self, raw_id: str) -> RawData:
        raw_in_db: Optional[RawFileInDB] = self.session.get(RawFileInDB, raw_id)

        if raw_in_db is None:
            raise ObjectNotFoundException(RawFileInDB, id=raw_id)

        boefje_meta = to_boefje_meta(raw_in_db.boefje_meta)
        return self.raw_repository.get_raw(raw_in_db.id, boefje_meta)

    def has_raw(self, boefje_meta: BoefjeMeta, mime_types: List[MimeType]) -> bool:
        query = self.session.query(RawFileInDB).filter(RawFileInDB.boefje_meta_id == boefje_meta.id)

        if len(mime_types) > 0:
            query = query.filter(RawFileInDB.mime_types.contains([mime_type.value for mime_type in mime_types]))

        count: int = query.count()

        return count > 0

    def _to_raw(self, raw_file_in_db: RawFileInDB) -> RawData:
        boefje_meta = to_boefje_meta(raw_file_in_db.boefje_meta)
        data = self.raw_repository.get_raw(raw_file_in_db.id, boefje_meta)

        return to_raw_data(raw_file_in_db, data.value)

    @staticmethod
    def _to_raw_meta(raw_file_in_db: RawFileInDB) -> RawDataMeta:
        boefje_meta = to_boefje_meta(raw_file_in_db.boefje_meta)

        return RawDataMeta(
            id=raw_file_in_db.id,
            boefje_meta=boefje_meta,
            secure_hash=raw_file_in_db.secure_hash,
            hash_retrieval_link=raw_file_in_db.hash_retrieval_link,
            mime_types=[to_mime_type(mime_type) for mime_type in raw_file_in_db.mime_types],
        )


def create_meta_data_repository(
    raw_repository: RawRepository = Depends(create_raw_repository),
) -> Iterator[MetaDataRepository]:
    session = sessionmaker(bind=get_engine())()
    repository = SQLMetaDataRepository(session, raw_repository, create_hash_repository(), settings)

    try:
        yield repository
    except Exception as error:
        logger.error("An error occured: %s. Rolling back session", error, exc_info=True)
        session.rollback()
        raise error
    finally:
        logger.info("Closing session")
        session.close()


class ObjectNotFoundException(Exception):
    def __init__(self, cls: Type[SQL_BASE], **kwargs):  # type: ignore
        super().__init__(f"The object of type {cls} was not found for query parameters {kwargs}")


def to_boefje_meta_in_db(boefje_meta: BoefjeMeta) -> BoefjeMetaInDB:
    return BoefjeMetaInDB(
        id=boefje_meta.id,
        boefje_id=boefje_meta.boefje.id,
        boefje_version=boefje_meta.boefje.version,
        arguments=boefje_meta.arguments,
        input_ooi=boefje_meta.input_ooi,
        organization=boefje_meta.organization,
        started_at=boefje_meta.started_at,
        ended_at=boefje_meta.ended_at,
    )


def to_boefje_meta(boefje_meta_in_db: BoefjeMetaInDB) -> BoefjeMeta:
    return BoefjeMeta(
        id=boefje_meta_in_db.id,
        boefje=Boefje(id=boefje_meta_in_db.boefje_id, version=boefje_meta_in_db.boefje_version),
        arguments=boefje_meta_in_db.arguments,
        input_ooi=boefje_meta_in_db.input_ooi,
        organization=boefje_meta_in_db.organization,
        started_at=boefje_meta_in_db.started_at,
        ended_at=boefje_meta_in_db.ended_at,
    )


def to_normalizer_meta_in_db(normalizer_meta: NormalizerMeta) -> NormalizerMetaInDB:
    return NormalizerMetaInDB(
        id=normalizer_meta.id,
        normalizer_name=normalizer_meta.normalizer.name,
        normalizer_version=normalizer_meta.normalizer.version,
        started_at=normalizer_meta.started_at,
        ended_at=normalizer_meta.ended_at,
        boefje_meta_id=normalizer_meta.boefje_meta.id,
        raw_file_id=normalizer_meta.raw_file_id,
    )


def to_normalizer_meta(normalizer_meta_in_db: NormalizerMetaInDB) -> NormalizerMeta:
    boefje_meta = to_boefje_meta(normalizer_meta_in_db.boefje_meta)

    return NormalizerMeta(
        id=normalizer_meta_in_db.id,
        normalizer=Normalizer(
            name=normalizer_meta_in_db.normalizer_name,
            version=normalizer_meta_in_db.normalizer_version,
        ),
        started_at=normalizer_meta_in_db.started_at,
        ended_at=normalizer_meta_in_db.ended_at,
        boefje_meta=boefje_meta,
        raw_file_id=normalizer_meta_in_db.raw_file_id,
    )


def to_raw_file_in_db(raw_data: RawData) -> RawFileInDB:
    return RawFileInDB(
        id=str(uuid.uuid4()),
        boefje_meta_id=raw_data.boefje_meta.id,
        secure_hash=raw_data.secure_hash,
        hash_retrieval_link=raw_data.hash_retrieval_link,
        mime_types=[mime_type.value for mime_type in raw_data.mime_types],
    )


def to_raw_data(raw_file_in_db: RawFileInDB, raw: bytes) -> RawData:
    return RawData(
        value=raw,
        boefje_meta=to_boefje_meta(raw_file_in_db.boefje_meta),
        secure_hash=raw_file_in_db.secure_hash,
        hash_retrieval_link=raw_file_in_db.hash_retrieval_link,
        mime_types=[to_mime_type(mime_type) for mime_type in raw_file_in_db.mime_types],
    )


def to_mime_type(mime_type: str) -> MimeType:
    return MimeType(value=mime_type)

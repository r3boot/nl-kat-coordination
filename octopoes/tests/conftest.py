import uuid
from datetime import datetime, timezone
from ipaddress import IPv4Address, ip_address
from typing import Dict, Iterator, List, Optional, Set
from unittest.mock import Mock

import pytest
from bits.runner import BitRunner
from requests.adapters import HTTPAdapter, Retry

from octopoes.api.api import app
from octopoes.api.models import Declaration, Observation
from octopoes.api.router import settings
from octopoes.config.settings import Settings, XTDBType
from octopoes.connector.octopoes import OctopoesAPIConnector
from octopoes.core.app import get_xtdb_client
from octopoes.core.service import OctopoesService
from octopoes.events.manager import EventManager
from octopoes.models import OOI, DeclaredScanProfile, EmptyScanProfile, Reference, ScanProfileBase
from octopoes.models.ooi.network import IPAddressV6
from octopoes.models.ooi.software import Software, SoftwareInstance
from octopoes.models.path import Direction, Path
from octopoes.models.types import (
    DNSZone,
    Hostname,
    HostnameHTTPURL,
    HTTPResource,
    IPAddressV4,
    IPPort,
    IPService,
    Network,
    ResolvedHostname,
    Service,
    Website,
)
from octopoes.repositories.ooi_repository import OOIRepository, XTDBOOIRepository
from octopoes.repositories.origin_repository import XTDBOriginRepository
from octopoes.repositories.scan_profile_repository import ScanProfileRepository
from octopoes.xtdb.client import XTDBHTTPClient, XTDBSession


@pytest.fixture
def valid_time():
    return datetime.now(timezone.utc)


class MockScanProfileRepository(ScanProfileRepository):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.profiles = {}

    def get(self, ooi_reference: Reference, valid_time: datetime) -> ScanProfileBase:
        return self.profiles[ooi_reference]

    def save(
        self, old_scan_profile: Optional[ScanProfileBase], new_scan_profile: ScanProfileBase, valid_time: datetime
    ) -> None:
        self.profiles[new_scan_profile.reference] = new_scan_profile

    def list(self, scan_profile_type: Optional[str], valid_time: datetime) -> List[ScanProfileBase]:
        if scan_profile_type:
            return [profile for profile in self.profiles.values() if profile.scan_profile_type == scan_profile_type]
        else:
            return self.profiles.values()

    def delete(self, scan_profile: ScanProfileBase, valid_time: datetime) -> None:
        del self.profiles[scan_profile.reference]


@pytest.fixture
def scan_profile_repository():
    return MockScanProfileRepository(Mock())


class MockOOIRepository(OOIRepository):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.oois = {}

    def save(self, ooi: OOI, valid_time: datetime, end_valid_time: Optional[datetime] = None) -> None:
        self.oois[ooi.reference] = ooi

    def load_bulk(self, references: Set[Reference], valid_time: datetime) -> Dict[str, OOI]:
        return {ooi.primary_key: ooi for ooi in self.oois.values() if ooi.reference in references}

    def list_neighbours(self, references: Set[Reference], paths: Set[Path], valid_time: datetime) -> Set[OOI]:
        neighbours = set()

        for path in paths:
            # Neighbours should have paths of length 1
            assert len(path.segments) == 1
            segment = path.segments[0]
            if segment.direction == Direction.OUTGOING:
                for ref in references:
                    neighbour_ref = getattr(self.oois[ref], segment.property_name)
                    if neighbour_ref is not None:
                        neighbours.add(self.oois[neighbour_ref])
            else:
                for ooi in self.oois.values():
                    if not isinstance(ooi, segment.target_type):
                        continue

                    for ref in references:
                        if getattr(ooi, segment.property_name) == ref:
                            neighbours.add(ooi)

        return neighbours

    def list_oois_without_scan_profile(self, valid_time: datetime) -> Set[Reference]:
        return set()


@pytest.fixture
def ooi_repository():
    return MockOOIRepository(Mock())


def add_ooi(ooi, ooi_repository, scan_profile_repository, valid_time):
    ooi_repository.save(ooi, valid_time)
    scan_profile_repository.save(None, EmptyScanProfile(reference=ooi.reference), valid_time)
    return ooi


@pytest.fixture
def network(ooi_repository, scan_profile_repository, valid_time):
    return add_ooi(Network(name="internet"), ooi_repository, scan_profile_repository, valid_time)


@pytest.fixture
def dns_zone(network, ooi_repository, hostname, scan_profile_repository, valid_time):
    ooi = add_ooi(
        DNSZone(name="example.com", hostname=hostname.reference, network=network.reference),
        ooi_repository,
        scan_profile_repository,
        valid_time,
    )
    hostname.dns_zone = ooi.reference
    return ooi


@pytest.fixture
def hostname(network, ooi_repository, scan_profile_repository, valid_time):
    return add_ooi(
        Hostname(name="example.com", network=network.reference),
        ooi_repository,
        scan_profile_repository,
        valid_time,
    )


@pytest.fixture
def ipaddressv4(network, ooi_repository, scan_profile_repository, valid_time):
    return add_ooi(
        IPAddressV4(network=network.reference, address=IPv4Address("192.0.2.1")),
        ooi_repository,
        scan_profile_repository,
        valid_time,
    )


@pytest.fixture
def resolved_hostname(hostname, ipaddressv4, ooi_repository, scan_profile_repository, valid_time):
    return add_ooi(
        ResolvedHostname(hostname=hostname.reference, address=ipaddressv4.reference),
        ooi_repository,
        scan_profile_repository,
        valid_time,
    )


@pytest.fixture
def http_resource_http(hostname, ipaddressv4, network):
    ip_port = IPPort(address=ipaddressv4.reference, protocol="tcp", port=80)
    ip_service = IPService(ip_port=ip_port.reference, service=Service(name="http").reference)
    website = Website(ip_service=ip_service.reference, hostname=hostname.reference)
    web_url = HostnameHTTPURL(netloc=hostname.reference, path="/", scheme="http", network=network.reference, port=80)
    return HTTPResource(website=website.reference, web_url=web_url.reference)


@pytest.fixture
def http_resource_https(hostname, ipaddressv4, network):
    ip_port = IPPort(address=ipaddressv4.reference, protocol="tcp", port=443)
    ip_service = IPService(ip_port=ip_port.reference, service=Service(name="https").reference)
    website = Website(ip_service=ip_service.reference, hostname=hostname.reference)
    web_url = HostnameHTTPURL(netloc=hostname.reference, path="/", scheme="https", network=network.reference, port=443)
    return HTTPResource(website=website.reference, web_url=web_url.reference)


@pytest.fixture
def empty_scan_profile():
    return EmptyScanProfile(reference="test_reference")


@pytest.fixture
def declared_scan_profile():
    return DeclaredScanProfile(reference="test_reference", level=2)


@pytest.fixture
def xtdbtype_crux():
    def get_settings_override():
        return Settings(xtdb_type=XTDBType.CRUX)

    app.dependency_overrides[settings] = get_settings_override
    yield
    app.dependency_overrides = {}


@pytest.fixture
def app_settings():
    return Settings()


@pytest.fixture
def octopoes_service() -> OctopoesService:
    return OctopoesService(Mock(), Mock(), Mock(), Mock())


@pytest.fixture
def bit_runner(mocker) -> BitRunner:
    return mocker.patch("octopoes.core.service.BitRunner")


@pytest.fixture
def xtdb_http_client(app_settings: Settings) -> XTDBHTTPClient:
    testnode = f"test-{str(uuid.uuid4())}"
    client = get_xtdb_client(app_settings.xtdb_uri, testnode, app_settings.xtdb_type)
    client._session.mount("http://", HTTPAdapter(max_retries=Retry(total=3, backoff_factor=1)))

    return client


@pytest.fixture
def xtdb_session(xtdb_http_client: XTDBHTTPClient) -> Iterator[XTDBSession]:
    xtdb_http_client.create_node()

    yield XTDBSession(xtdb_http_client)

    xtdb_http_client.delete_node()


@pytest.fixture
def octopoes_api_connector(xtdb_session: XTDBSession) -> OctopoesAPIConnector:
    connector = OctopoesAPIConnector("http://ci_octopoes:80", xtdb_session.client._client)
    connector.session.mount("http://", HTTPAdapter(max_retries=Retry(total=3, backoff_factor=1)))

    return connector


@pytest.fixture
def xtdb_ooi_repository(xtdb_session: XTDBSession) -> Iterator[XTDBOOIRepository]:
    yield XTDBOOIRepository(Mock(spec=EventManager), xtdb_session, XTDBType.XTDB_MULTINODE)


@pytest.fixture
def mock_xtdb_session():
    return XTDBSession(Mock())


@pytest.fixture
def origin_repository(mock_xtdb_session):
    yield XTDBOriginRepository(Mock(spec=EventManager), mock_xtdb_session, XTDBType.XTDB_MULTINODE)


def seed_system(octopoes_api_connector: OctopoesAPIConnector, valid_time):
    network = Network(name="test")
    octopoes_api_connector.save_declaration(Declaration(ooi=network, valid_time=valid_time))

    hostnames = [
        Hostname(network=network.reference, name="example.com"),
        Hostname(network=network.reference, name="a.example.com"),
        Hostname(network=network.reference, name="b.example.com"),
        Hostname(network=network.reference, name="c.example.com"),
        Hostname(network=network.reference, name="d.example.com"),
        Hostname(network=network.reference, name="e.example.com"),
        Hostname(network=network.reference, name="f.example.com"),
    ]

    addresses = [
        IPAddressV4(network=network.reference, address=ip_address("192.0.2.3")),
        IPAddressV6(network=network.reference, address=ip_address("3e4d:64a2:cb49:bd48:a1ba:def3:d15d:9230")),
    ]
    ports = [
        IPPort(address=addresses[0].reference, protocol="tcp", port=25),
        IPPort(address=addresses[0].reference, protocol="tcp", port=443),
        IPPort(address=addresses[0].reference, protocol="tcp", port=22),
        IPPort(address=addresses[1].reference, protocol="tcp", port=80),
    ]
    services = [Service(name="smtp"), Service(name="https"), Service(name="http"), Service(name="ssh")]
    ip_services = [
        IPService(ip_port=ports[0].reference, service=services[0].reference),
        IPService(ip_port=ports[1].reference, service=services[1].reference),
        IPService(ip_port=ports[2].reference, service=services[3].reference),
        IPService(ip_port=ports[3].reference, service=services[2].reference),
    ]

    resolved_hostnames = [
        ResolvedHostname(hostname=hostnames[0].reference, address=addresses[0].reference),  # ipv4
        ResolvedHostname(hostname=hostnames[0].reference, address=addresses[1].reference),  # ipv6
        ResolvedHostname(hostname=hostnames[1].reference, address=addresses[0].reference),
        ResolvedHostname(hostname=hostnames[2].reference, address=addresses[0].reference),
        ResolvedHostname(hostname=hostnames[3].reference, address=addresses[0].reference),
        ResolvedHostname(hostname=hostnames[4].reference, address=addresses[0].reference),
        ResolvedHostname(hostname=hostnames[5].reference, address=addresses[0].reference),
        ResolvedHostname(hostname=hostnames[3].reference, address=addresses[1].reference),
        ResolvedHostname(hostname=hostnames[4].reference, address=addresses[1].reference),
        ResolvedHostname(hostname=hostnames[6].reference, address=addresses[1].reference),
    ]

    websites = [
        Website(ip_service=ip_services[0].reference, hostname=hostnames[0].reference),
        Website(ip_service=ip_services[0].reference, hostname=hostnames[1].reference),
    ]
    software = [Software(name="DICOM")]
    instance = [SoftwareInstance(ooi=ports[0].reference, software=software[0].reference)]

    oois = hostnames + addresses + ports + services + ip_services + resolved_hostnames + websites + software + instance
    octopoes_api_connector.save_observation(
        Observation(method="", source=network.reference, task_id=uuid.uuid4(), valid_time=valid_time, result=oois)
    )
    octopoes_api_connector.recalculate_bits()

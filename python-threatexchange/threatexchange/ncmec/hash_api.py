# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

"""
Simple implementation for the NCMEC hash sharing XML API

You can find the complete documentation at 
https://report.cybertip.org/hashsharing/v2/documentation.pdf
"""

import xml.etree.ElementTree as ET
from datetime import datetime
from dataclasses import dataclass
from enum import Enum, unique
import logging
import time
import typing as t
from io import BytesIO

import requests
from requests.packages.urllib3.util.retry import Retry

# Maybe move to a common library someday
from threatexchange.fb_threatexchange.api import TimeoutHTTPAdapter


_DATE_FORMAT_STR = "%Y-%m-%dT%H:%M:%SZ"
_DEFAULT_ELE = ET.Element("")

T = t.TypeVar("T")


def nullthrows(v: t.Optional[T]) -> T:
    """Typing helper"""
    assert v is not None
    return v


class _XMLWrapper:
    """
    Simpler wrapper around XML element for typing and null checking.

    To use the raw methods, just use .element
    """

    def __init__(self, e: ET.Element) -> None:
        self.element = e

    def __getitem__(self, key: str) -> "_XMLWrapper":
        """Asserts child with name exists"""
        child = self.element.findall(key)
        if not child:
            raise IndexError
        if len(child) != 1:
            raise ValueError(f"More than one child named {key}")
        return _XMLWrapper(child[0])

    def maybe(self, key: str, *rest: str) -> "_XMLWrapper":
        """Gets a child node or a placeholder for chaining"""
        curr = self.element
        for k in (key,) + rest:
            child = curr.find(k)
            if child is None:
                return _XMLWrapper(_DEFAULT_ELE)
            curr = child
        return _XMLWrapper(curr)

    @property
    def text(self) -> str:
        """Assert non-null text"""
        return nullthrows(self.element.text)

    @property
    def tag(self) -> str:
        """Suger for getting the tag name"""
        return self.element.tag

    def __len__(self) -> int:
        return len(self.element)

    def __bool__(self) -> bool:
        """True if not a non-existant element from .maybe()"""
        return self.element is not _DEFAULT_ELE

    def __iter__(self) -> t.Iterator["_XMLWrapper"]:
        """Iterate over children"""
        for child in self.element:
            yield _XMLWrapper(child)

    def __str__(self) -> str:
        return f"<{self.tag}>{self.element.text or '...'}</{self.tag}>"

    # Because these methods are named after types it messes
    # with typechecking
    def int(self, key: str) -> int:
        """Assert property exists and return as int"""
        return int(nullthrows(self.element.get(key)))

    def str(self, key: str) -> str:
        """Assert property exists and return as str"""
        return nullthrows(self.element.get(key))


@dataclass
class StatusResult:
    esp_id: int
    esp_name: str


@unique
class NCMECEntryType(Enum):
    image = "image"
    video = "video"


@dataclass
class NCMECEntryUpdate:
    id: str
    member_id: int
    entry_type: NCMECEntryType
    deleted: bool
    classification: t.Optional[str]
    fingerprints: t.Dict[str, str]

    @classmethod
    def from_xml(cls, xml: _XMLWrapper) -> "NCMECEntryUpdate":
        type_str = xml.tag.lower()
        deleted = type_str.startswith("deleted")
        if deleted:
            type_str = type_str[len("deleted") :]

        return cls(
            id=xml["id"].text,
            member_id=xml["member"].int("id"),
            entry_type=NCMECEntryType(type_str),
            deleted=deleted,
            classification=xml.maybe("classification").element.text,
            fingerprints={x.tag: x.text for x in xml.maybe("fingerprints")},
        )


@dataclass
class GetEntriesResponse:
    updates: t.List[NCMECEntryUpdate]
    max_timestamp: int
    next: str

    @classmethod
    def from_xml(cls, xml: _XMLWrapper, fallback_max_time: int) -> "GetEntriesResponse":
        updates: t.List[NCMECEntryUpdate] = []
        max_ts = 0

        for content_xml in (xml.maybe("images"), xml.maybe("videos")):
            if not content_xml or not len(content_xml):
                continue
            max_ts = max(
                max_ts,
                int(
                    datetime.strptime(
                        content_xml.str("maxTimestamp"), _DATE_FORMAT_STR
                    ).timestamp()
                ),
            )
            updates.extend(NCMECEntryUpdate.from_xml(c) for c in content_xml)

        next_ = xml.maybe("paging", "next").element.text or ""
        return cls(updates, max_ts or fallback_max_time, next_)


@unique
class NCMECEndpoint(Enum):
    status = "status"
    entries = "entries"


class NCMECEnvironment(Enum):
    """
    Which 'Environment' to connect to.

    Environments differ by who is allowed to read or write, as well as what
    content exists on them.
    """

    Industry = "https://exttest.cybertip.org/hashsharing"
    NGO = "https://hashsharing-test.ncmec.org/npo"
    Exploitative = "https://hashsharing-test.ncmec.org/exploitative"

    test_Industry = "https://exttest.cybertip.org/hashsharing"
    test_NGO = "https://hashsharing-test.ncmec.org/npo"
    test_Exploitative = "https://hashsharing-test.ncmec.org/exploitative"


class NCMECHashAPI:
    """
    A wrapper around the NCMEC hash exchange API.

    Not to be confused with the cybertips API, which is for submitting illegal
    content, the hash exchange API is for submitting signals (or 'fingerprints'
    in the NCMEC parlance).

    Depending on which API you are connecting to, what is inside the database
    may differ greatly in exact topic. See NCMECEnvironment as well as NCMEC's
    documentation.
    """

    VERSION = "v2"

    def __init__(
        self,
        username: str,
        password: str,
        environment: NCMECEnvironment = NCMECEnvironment.test_Industry,
    ) -> None:
        self.username = username
        self.password = password
        self._base_url = environment.value

    def _get_session(self, endpoint: NCMECEndpoint) -> requests.Session:
        """
        Custom requests sesson

        Ideally, should be used within a context manager:
        ```
        with self._get_session() as session:
            session.get()...
        ```
        """

        session = requests.Session()
        session.auth = (self.username, self.password)
        session.mount(
            self._base_url,
            adapter=TimeoutHTTPAdapter(
                timeout=60,
                max_retries=Retry(
                    total=4,
                    status_forcelist=[429, 500, 502, 503, 504],
                    # No retry for post. Could probably add timeout...
                    allowed_methods=["HEAD", "GET", "OPTIONS"],
                    backoff_factor=0.2,  # ~1.5 seconds of retries
                ),
            ),
        )
        return session

    def _get(self, endpoint: NCMECEndpoint, *, next_: str = "", **params) -> ET.Element:
        """
        Perform an HTTP GET request, and return the XML response payload.

        Same timeouts and retry strategy as `_get_session` above.
        """

        url = "/".join((self._base_url, self.VERSION, endpoint.value))
        if next_:
            url = self._base_url + next_
            params = {}

        with self._get_session(endpoint) as session:
            response = session.get(url, params=params)
        # Gate this log just in case decode() blows up
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            logging.debug("GET %s returned: %s", endpoint, response.content.decode())
        response.raise_for_status()
        # Namespaces don't really add anything here, so let's remove them
        it = ET.iterparse(BytesIO(response.content))
        for _, el in it:
            _ns, has_namespace, postfix = el.tag.partition("}")
            if has_namespace:
                el.tag = postfix
        return it.root  # type: ignore

    def _post(self, endpoint: NCMECEndpoint, *, data=None) -> t.Any:
        """
        Perform an HTTP POST request, and return the XML response payload.

        No timeout or retry strategy.
        """

        url = "/".join((self._base_url, endpoint.value))
        with self._get_session(endpoint) as session:
            response = session.post(url, data=data)
            response.raise_for_status()
            return response

    def status(self) -> StatusResult:
        """Query the status endpoint, which tells you who you are."""
        response = self._get(NCMECEndpoint.status)
        member = _XMLWrapper(response)["member"]
        return StatusResult(member.int("id"), member.text)

    def get_entries(
        self,
        *,
        start_timestamp: int = 0,
        next_: str = "",
    ) -> GetEntriesResponse:
        """
        Fetch a series of update records from the hash API.
        """
        # Need a dict here for python keyword 'from'
        params: t.Dict[str, t.Any] = {
            "from": _date_format(start_timestamp),
        }
        response = self._get(
            NCMECEndpoint.entries,
            next_=next_,
            to=_date_format(int(time.time())),
            **params,
        )
        return GetEntriesResponse.from_xml(_XMLWrapper(response), int(time.time()))

    def get_entries_iter(
        self, start_timestamp: int = 0
    ) -> t.Iterator[GetEntriesResponse]:
        """
        A simple wrapper around get_entries to keep fetching until complete.
        """
        has_more = True
        next_ = ""
        while has_more:
            result = self.get_entries(start_timestamp=start_timestamp, next_=next_)
            next_ = result.next
            has_more = bool(next_)
            yield result


def _date_format(timestamp: int) -> str:
    """ISO 8601 format yyyy-MM-dd'T'HH:mm:ss.SSSZ"""
    return datetime.fromtimestamp(timestamp).strftime(_DATE_FORMAT_STR)

from __future__ import annotations

import json
import re
import socket
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from http.cookiejar import Cookie, LWPCookieJar
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import HTTPCookieProcessor, Request, build_opener


BASE_URL = "https://www.steamgifts.com"
WISHLIST_PATH = "/giveaways/search?type=wishlist"


class SteamGiftsError(RuntimeError):
    pass


class SteamGiftsHTTPError(SteamGiftsError):
    def __init__(self, status: int, reason: str):
        self.status = status
        super().__init__(f"SteamGifts returned HTTP {status}: {reason}")


class LoginExpiredError(SteamGiftsError):
    pass


class ParseError(SteamGiftsError):
    pass


class EntryError(SteamGiftsError):
    def __init__(self, message: str, insufficient_points: bool = False, already_entered: bool = False):
        self.insufficient_points = insufficient_points
        self.already_entered = already_entered
        super().__init__(message)


@dataclass(frozen=True)
class Giveaway:
    code: str
    title: str
    url: str
    points: int | None
    entered: bool


@dataclass(frozen=True)
class WishlistPage:
    giveaways: tuple[Giveaway, ...]
    csrf_token: str
    has_next: bool


@dataclass(frozen=True)
class EntryResult:
    code: str
    points: int | None
    entry_count: int | None


class WishlistParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.authenticated = False
        self.sign_in = False
        self.csrf_token = ""
        self.has_next = False
        self.rows: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self.depth = 0
        self.pinned_depths: list[int] = []
        self.capture_title = False
        self.capture_thin = False
        self.capture_next = False

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {key: value or "" for key, value in attrs_list}
        classes = set(attrs.get("class", "").split())
        if "pinned-giveaways__outer-wrap" in classes:
            self.pinned_depths.append(self.depth)
        if "nav__avatar-outer-wrap" in classes or "nav__points" in classes:
            self.authenticated = True
        if tag == "input" and attrs.get("name") == "xsrf_token":
            self.csrf_token = attrs.get("value", "")
        if self.current is None and "giveaway__row-inner-wrap" in classes:
            self.current = {
                "depth": self.depth,
                "entered": "is-faded" in classes,
                "pinned": bool(self.pinned_depths),
                "href": "",
                "title": [],
                "thin": [],
            }
        if self.current is not None:
            if "giveaway__heading__name" in classes:
                self.current["href"] = attrs.get("href", "")
                self.capture_title = True
            if "giveaway__heading__thin" in classes:
                self.capture_thin = True
        if tag == "a":
            href = attrs.get("href", "")
            if "login" in href or "openid" in href:
                self.capture_next = False
            if attrs.get("rel") == "next":
                self.has_next = True
        if tag not in {"meta", "link", "img", "input", "br", "hr"}:
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag not in {"meta", "link", "img", "input", "br", "hr"}:
            self.depth -= 1
        if self.current is not None and tag in {"a", "h2"}:
            self.capture_title = False
        if self.current is not None and tag == "span":
            self.capture_thin = False
        if self.current is not None and self.depth == self.current["depth"]:
            self.rows.append(self.current)
            self.current = None
        if self.pinned_depths and self.depth == self.pinned_depths[-1]:
            self.pinned_depths.pop()

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        if "Sign in through Steam" in text:
            self.sign_in = True
        if self.capture_title and self.current is not None:
            self.current["title"].append(text)
        if self.capture_thin and self.current is not None:
            self.current["thin"].append(text)
        if text.casefold() == "next":
            self.has_next = True


def parse_wishlist(html: str) -> WishlistPage:
    parser = WishlistParser()
    parser.feed(html)
    if parser.sign_in and not parser.authenticated:
        raise LoginExpiredError("SteamGifts login has expired")
    if not parser.authenticated:
        raise ParseError("Authenticated SteamGifts navigation was not found")
    if not parser.csrf_token:
        raise ParseError("SteamGifts CSRF token was not found")
    giveaways: list[Giveaway] = []
    for row in parser.rows:
        if row["pinned"]:
            continue
        href = row["href"]
        match = re.match(r"^/giveaway/([A-Za-z0-9]+)/", href)
        if not match:
            continue
        thin = " ".join(row["thin"])
        point_matches = re.findall(r"\((\d+)P\)", thin)
        title = " ".join(row["title"]).strip()
        if not title:
            raise ParseError(f"Giveaway {match.group(1)} has no title")
        giveaways.append(
            Giveaway(
                code=match.group(1),
                title=title,
                url=urljoin(BASE_URL, href),
                points=int(point_matches[-1]) if point_matches else None,
                entered=bool(row["entered"]),
            )
        )
    return WishlistPage(tuple(giveaways), parser.csrf_token, parser.has_next)


def parse_entry_response(code: str, body: str) -> EntryResult:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ParseError("SteamGifts entry response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ParseError("SteamGifts entry response had an unexpected shape")
    if payload.get("type") != "success":
        message = str(payload.get("msg") or payload.get("message") or "Entry was rejected")
        normalized = message.casefold()
        insufficient = "point" in normalized and any(word in normalized for word in ("enough", "insufficient", "require"))
        already_entered = "already" in normalized and "enter" in normalized
        raise EntryError(message, insufficient_points=insufficient, already_entered=already_entered)
    points = payload.get("points")
    entry_count = payload.get("entry_count")

    def optional_integer(value: Any, name: str) -> int | None:
        if value is None:
            return None
        normalized = str(value).replace(",", "").replace(" ", "")
        try:
            return int(normalized)
        except ValueError as exc:
            raise ParseError(f"SteamGifts entry response had an invalid {name}") from exc

    return EntryResult(
        code=code,
        points=optional_integer(points, "points"),
        entry_count=optional_integer(entry_count, "entry count"),
    )


class SteamGiftsClient:
    def __init__(self, cookie_header: str, cookie_path: Path, timeout: float, user_agent: str):
        self.cookie_path = cookie_path
        self.timeout = timeout
        self.user_agent = user_agent
        self.jar = LWPCookieJar(str(cookie_path))
        if cookie_path.exists():
            try:
                self.jar.load(ignore_discard=True, ignore_expires=True)
            except OSError as exc:
                raise SteamGiftsError(f"Could not load cookie jar: {exc}") from exc
        self._seed_cookies(cookie_header)
        self.opener = build_opener(HTTPCookieProcessor(self.jar))
        self.csrf_token = ""

    def _seed_cookies(self, header: str) -> None:
        parsed = SimpleCookie()
        try:
            parsed.load(header)
        except Exception as exc:
            raise SteamGiftsError("SG_COOKIE is not a valid Cookie header") from exc
        if not parsed:
            raise SteamGiftsError("SG_COOKIE did not contain any cookies")
        for name, morsel in parsed.items():
            self.jar.set_cookie(
                Cookie(
                    version=0,
                    name=name,
                    value=morsel.value,
                    port=None,
                    port_specified=False,
                    domain=".steamgifts.com",
                    domain_specified=True,
                    domain_initial_dot=True,
                    path="/",
                    path_specified=True,
                    secure=True,
                    expires=None,
                    discard=True,
                    comment=None,
                    comment_url=None,
                    rest={"HttpOnly": None},
                    rfc2109=False,
                )
            )

    def _save_cookies(self) -> None:
        self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
        self.jar.save(ignore_discard=True, ignore_expires=True)
        self.cookie_path.chmod(0o600)

    def _request(self, path: str, data: dict[str, str] | None = None) -> tuple[str, Message]:
        url = urljoin(BASE_URL, path)
        encoded = urlencode(data).encode() if data is not None else None
        headers = {
            "Accept": "application/json" if data is not None else "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.8",
            "User-Agent": self.user_agent,
        }
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
            headers["X-Requested-With"] = "XMLHttpRequest"
            headers["Referer"] = urljoin(BASE_URL, WISHLIST_PATH)
        request = Request(url, data=encoded, headers=headers, method="POST" if data is not None else "GET")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
                final_url = response.geturl()
                if "steamcommunity.com/openid" in final_url or "/login" in final_url:
                    raise LoginExpiredError("SteamGifts redirected to login")
                return body, response.headers
        except HTTPError as exc:
            raise SteamGiftsHTTPError(exc.code, str(exc.reason)) from exc
        except (URLError, TimeoutError, socket.timeout, ConnectionError) as exc:
            reason = getattr(exc, "reason", exc)
            raise SteamGiftsError(f"Could not connect to SteamGifts: {reason}") from exc
        finally:
            self._save_cookies()

    def wishlist(self, max_pages: int) -> tuple[Giveaway, ...]:
        found: dict[str, Giveaway] = {}
        for page_number in range(1, max_pages + 1):
            separator = "&" if "?" in WISHLIST_PATH else "?"
            body, _ = self._request(f"{WISHLIST_PATH}{separator}page={page_number}")
            page = parse_wishlist(body)
            self.csrf_token = page.csrf_token
            for giveaway in page.giveaways:
                found[giveaway.code] = giveaway
            if not page.has_next:
                break
        return tuple(found.values())

    def enter(self, code: str) -> EntryResult:
        if not re.fullmatch(r"[A-Za-z0-9]+", code):
            raise EntryError("Invalid giveaway code")
        if not self.csrf_token:
            body, _ = self._request(WISHLIST_PATH)
            self.csrf_token = parse_wishlist(body).csrf_token
        body, _ = self._request(
            "/ajax.php",
            {"xsrf_token": self.csrf_token, "do": "entry_insert", "code": code},
        )
        return parse_entry_response(code, body)

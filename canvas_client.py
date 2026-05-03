"""Thin Canvas REST wrapper using session cookies."""
from __future__ import annotations

from typing import Iterator
from urllib.parse import urljoin

import requests

CANVAS_BASE = "https://canvas.nd.edu"


class CanvasClient:
    def __init__(self, cookies: dict[str, str]):
        self.s = requests.Session()
        self.s.cookies.update(cookies)
        self.s.headers.update({
            "Accept": "application/json+canvas-string-ids, application/json",
            "User-Agent": "nd-canvas-dl/0.1",
        })

    def _paginate(self, url: str, params: dict | None = None) -> Iterator[dict]:
        params = {**(params or {}), "per_page": 100}
        while url:
            r = self.s.get(url, params=params if "?" not in url else None)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                yield from data
            else:
                yield data
                return
            url = r.links.get("next", {}).get("url")
            params = None

    def get(self, path: str, **params):
        r = self.s.get(urljoin(CANVAS_BASE, path), params=params)
        r.raise_for_status()
        return r.json()

    def list_courses(self) -> list[dict]:
        return list(self._paginate(
            urljoin(CANVAS_BASE, "/api/v1/courses"),
            params={"enrollment_state": "active", "include[]": "term"},
        ))

    def list_files(self, course_id) -> list[dict]:
        return list(self._paginate(
            urljoin(CANVAS_BASE, f"/api/v1/courses/{course_id}/files")
        ))

    def get_file(self, file_id) -> dict:
        return self.get(f"/api/v1/files/{file_id}")

    def list_modules(self, course_id) -> list[dict]:
        return list(self._paginate(
            urljoin(CANVAS_BASE, f"/api/v1/courses/{course_id}/modules"),
            params={"include[]": "items"},
        ))

    def list_module_items(self, course_id, module_id) -> list[dict]:
        return list(self._paginate(
            urljoin(CANVAS_BASE, f"/api/v1/courses/{course_id}/modules/{module_id}/items"),
            params={"include[]": "content_details"},
        ))

    def list_pages(self, course_id) -> list[dict]:
        return list(self._paginate(
            urljoin(CANVAS_BASE, f"/api/v1/courses/{course_id}/pages")
        ))

    def get_page(self, course_id, url_slug: str) -> dict:
        return self.get(f"/api/v1/courses/{course_id}/pages/{url_slug}")

    def list_assignments(self, course_id) -> list[dict]:
        return list(self._paginate(
            urljoin(CANVAS_BASE, f"/api/v1/courses/{course_id}/assignments")
        ))

    def list_announcements(self, course_id) -> list[dict]:
        return list(self._paginate(
            urljoin(CANVAS_BASE, "/api/v1/announcements"),
            params={"context_codes[]": f"course_{course_id}"},
        ))

    def list_discussions(self, course_id) -> list[dict]:
        return list(self._paginate(
            urljoin(CANVAS_BASE, f"/api/v1/courses/{course_id}/discussion_topics")
        ))

    def list_quizzes(self, course_id) -> list[dict]:
        return list(self._paginate(
            urljoin(CANVAS_BASE, f"/api/v1/courses/{course_id}/quizzes")
        ))

    def list_quiz_questions(self, course_id, quiz_id) -> list[dict]:
        return list(self._paginate(
            urljoin(CANVAS_BASE, f"/api/v1/courses/{course_id}/quizzes/{quiz_id}/questions")
        ))

    def download(self, url: str, dest) -> None:
        with self.s.get(url, stream=True) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1024 * 64):
                    f.write(chunk)

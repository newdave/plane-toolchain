"""Entry point for the GitHub PR gate and delivery evidence actions.

Thin layer over ``plane_toolchain.github_evidence``: a GitHub REST client, the
Plane preflight, error classification, annotations and the job summary. Every
failure prints a fixed, secret-free cause; no URL, header or body is echoed.
"""

import re

import httpx2


class Failure(Exception):
    """A failure whose message is fixed, secret-free text."""


class GitHub:
    """Minimal GitHub REST reads and the one status write the gate needs."""

    def __init__(
        self,
        token: str,
        repository: str,
        api_url: str = "https://api.github.com",
        client: httpx2.Client | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid repository")
        self._token = token
        self._repository = repository
        self._api_url = api_url.rstrip("/")
        self._client = client or httpx2.Client(timeout=30)

    def _request(self, operation: str, method: str, path: str, **options):
        headers = {
            "Authorization": "Bearer " + self._token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"{self._api_url}/repos/{self._repository}{path}"
        try:
            response = self._client.request(method, url, headers=headers, **options)
        except httpx2.HTTPError:
            raise Failure(f"GitHub {operation} failed (network error)") from None
        if not 200 <= response.status_code < 300:
            raise Failure(f"GitHub {operation} failed (HTTP {response.status_code})")
        try:
            return response.json()
        except ValueError:
            raise Failure(f"GitHub {operation} failed (invalid response)") from None

    def pull(self, number: int) -> dict:
        return self._request("pull", "GET", f"/pulls/{int(number)}")

    def pulls_for_commit(self, sha: str) -> list[dict]:
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise Failure("Invalid commit SHA")
        return self._request(
            "commit pulls", "GET", f"/commits/{sha}/pulls", params={"per_page": 100}
        )

    def files(self, number: int) -> list[str]:
        names: list[str] = []
        for page in range(1, 31):
            rows = self._request(
                "files",
                "GET",
                f"/pulls/{int(number)}/files",
                params={"per_page": 100, "page": page},
            )
            names.extend(row["filename"] for row in rows)
            if len(rows) < 100:
                return names
        raise Failure("PR file listing incomplete")

    def post_status(self, sha: str, state: str, description: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise Failure("Invalid PR SHA")
        self._request(
            "status",
            "POST",
            f"/statuses/{sha}",
            json={"state": state, "context": "Plane task references", "description": description},
        )


def classify(error: BaseException) -> str:
    """Fixed, secret-free cause text for an error."""
    if type(error) is Failure:
        return str(error)
    return "Unexpected " + type(error).__name__

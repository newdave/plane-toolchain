"""Project Pages compatibility for Plane CE v1.4.3.

Installed in a derived backend image, never imported by the Plane API.
Existing documents are edited only as a whole-body replacement with an updated_at
precondition, after force-closing open editors through Live's Redis admin channel.
"""

import hashlib
import json
from datetime import datetime, timedelta, timezone

# Dependencies below exist in the Plane backend image, not this toolchain's environment.
# pyright: reportMissingImports=false

# List rows omit document bodies (description_*); retrieve keeps full detail.
LIST_FIELDS = (
    "id",
    "name",
    "access",
    "owned_by",
    "created_at",
    "updated_at",
    "archived_at",
    "is_locked",
    "parent",
)


# Client-facing text comes only from this table, never from an exception object.
INPUT_ERRORS = {
    "object": "Request body must be a JSON object",
    "fields": "Only name, description_html and access are supported",
    "name": "Expected a non-empty page name of at most 255 characters",
    "body": "Invalid page body or access",
    "document": "Live service did not return a complete document",
    "update_fields": "Only description_html, expected_updated_at and boolean force are supported",
    "expected_updated_at": "expected_updated_at must be the page's ISO 8601 updated_at with offset",
    "force": "Editing a page disconnects open editors; retry with force=true to proceed",
    "locked": "Page is locked; unlock it before editing",
    "archived": "Page is archived; restore it before editing",
    "conflict": "Page changed since expected_updated_at; retrieve it and retry",
    "close": "Could not disconnect open editors through Live; nothing written",
}

# Update error keys that are not plain 400 validation failures.
UPDATE_STATUS = {"conflict": 409, "close": 502}

# Live (apps/live) subscribes to this channel; a document's name is its page id.
ADMIN_CHANNEL = "hocuspocus:admin"
# Live closes connections about 50 ms after the command, then stores pending edits.
FORCE_CLOSE_WAIT_SECONDS = 1.0
# Live stores an open document after a 10 s debounce. The response tells the client to
# compare the page again once that debounce plus margin has passed; the API never waits.
LATE_STORE_WINDOW_SECONDS = 12
# Plane's track_page_version keeps at most this many versions per page.
VERSION_LIMIT = 20


class PageInputError(ValueError):
    """Client-safe validation failure identified by an INPUT_ERRORS key."""

    def __init__(self, key):
        super().__init__(INPUT_ERRORS[key])
        self.key = key


def creation_payload(data, sanitise, convert):
    """Validate and convert a complete new document before any database write."""
    if not isinstance(data, dict):
        raise PageInputError("object")
    if set(data) - {"name", "description_html", "access"}:
        raise PageInputError("fields")
    name = data.get("name")
    body = data.get("description_html")
    access = data.get("access", 1)
    if not isinstance(name, str) or not name.strip() or len(name) > 255:
        raise PageInputError("name")
    if not isinstance(body, str) or len(body.encode()) > 1_000_000 or access not in (0, 1):
        raise PageInputError("body")
    return dict(name=name, access=access, **converted_body(body, sanitise, convert))


def converted_body(body, sanitise, convert):
    """Sanitise HTML and convert it into matching JSON and binary document formats."""
    clean = sanitise(body)
    converted = convert(clean)
    if (
        not isinstance(converted, dict)
        or not isinstance(converted.get("description_json"), dict)
        or not isinstance(converted.get("description_binary"), str)
        or not converted["description_binary"]
    ):
        raise PageInputError("document")
    return dict(
        description_html=clean,
        description_json=converted["description_json"],
        description_binary=converted["description_binary"],
    )


def update_request(data):
    """Validate an update request; return (description_html, expected updated_at)."""
    if not isinstance(data, dict):
        raise PageInputError("object")
    if set(data) - {"description_html", "expected_updated_at", "force"}:
        raise PageInputError("update_fields")
    body = data.get("description_html")
    if not isinstance(body, str) or len(body.encode()) > 1_000_000:
        raise PageInputError("body")
    stamp = data.get("expected_updated_at")
    try:
        expected = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise PageInputError("expected_updated_at") from None
    if expected.tzinfo is None:
        raise PageInputError("expected_updated_at")
    force = data.get("force", False)
    if not isinstance(force, bool):
        raise PageInputError("update_fields")
    if not force:
        raise PageInputError("force")
    return body, expected


def editable(page):
    """Refuse what Plane's description update refuses."""
    if page.is_locked:
        raise PageInputError("locked")
    if page.archived_at:
        raise PageInputError("archived")


def stored_bytes(value):
    """BinaryField reads back as memoryview, bytes or None."""
    return bytes(value) if value is not None else b""


def content_digest(page):
    """Digest of the stored body; Live re-storing an unchanged document keeps it."""
    digest = hashlib.sha256()
    for part in ((page.description_html or "").encode(), stored_bytes(page.description_binary)):
        digest.update(hashlib.sha256(part).digest())
    return digest.hexdigest()


def page_update(data, load, prepare, close_editors, wait, locked, version):
    """Replace a page body; each step runs only after the previous one succeeded.

    load returns the visible page. prepare returns validated description_* values.
    locked is a context manager yielding the row under SELECT FOR UPDATE inside a
    transaction. version stores that row's current body as a PageVersion.
    """
    body, expected = update_request(data)
    page = load()
    editable(page)
    if page.updated_at != expected:
        raise PageInputError("conflict")
    seen = content_digest(page)
    # Convert before disconnecting anyone: a Live failure leaves editors untouched.
    new = prepare(body)
    close_editors(str(page.id))
    # Let Live store edits it held in memory. Its unload store bumps updated_at even
    # when nothing changed, so the recheck compares content instead.
    wait(FORCE_CLOSE_WAIT_SECONDS)
    with locked() as page:
        editable(page)
        if content_digest(page) != seen:
            raise PageInputError("conflict")
        old_html = page.description_html
        version(page)
        page.description_html = new["description_html"]
        page.description_json = new["description_json"]
        page.description_binary = new["description_binary"]
        # One save(): Page.save() derives description_stripped and bumps updated_at.
        page.save()
    return {"page": page, "old_html": old_html}


def late_store_check(now):
    """Readback hint: when and how the client detects a late Live store.

    Live may still store an editor's older state after the commit. The API returns at
    once instead of polling; a client that retrieves the page after recheck_after and
    finds a different description_html knows Live replaced the body.
    """
    after = now + timedelta(seconds=LATE_STORE_WINDOW_SECONDS)
    return {
        "recheck_after": after.isoformat(),
        "seconds": LATE_STORE_WINDOW_SECONDS,
        "compare": "description_html",
        "on_difference": "Live overwrote the update; retrieve the page and retry",
    }


def force_close_message(doc_id):
    """Live's force_close admin command (apps/live/src/types/admin-commands.ts)."""
    return ADMIN_CHANNEL, json.dumps(
        {
            "command": "force_close",
            "docId": doc_id,
            "reason": "admin_request",
            "code": 4000,
            "originServer": "plane-api-ce-pages",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


def publish_force_close(client, doc_id):
    """Publish force_close; no subscriber means no Live server would disconnect editors."""
    try:
        receivers = client.publish(*force_close_message(doc_id))
    except Exception:
        raise PageInputError("close") from None
    if not receivers:
        raise PageInputError("close")


def routes():
    """Use Plane's authentication, throttling, serializers and project permissions."""
    import logging
    import os
    import time
    from contextlib import contextmanager

    import requests
    from django.db import transaction
    from django.core.exceptions import ValidationError as DjangoValidationError
    from django.http import Http404
    from django.shortcuts import get_object_or_404
    from django.urls import path
    from plane.api.views.base import BaseAPIView
    from plane.app.permissions import ProjectPagePermission
    from plane.app.serializers import (
        PageBinaryUpdateSerializer,
        PageDetailSerializer,
        PageSerializer,
    )
    from plane.app.views.page.base import PageViewSet
    from plane.bgtasks.page_transaction_task import page_transaction
    from plane.db.models import Page, PageVersion, Project, ProjectMember
    from plane.settings.redis import redis_instance
    from rest_framework.exceptions import APIException, ValidationError
    from rest_framework.permissions import IsAuthenticated
    from rest_framework.response import Response

    def found(source, **lookup):
        """404 for absent rows and malformed ids; never a 500."""
        try:
            return get_object_or_404(source, **lookup)
        except (DjangoValidationError, ValueError):
            raise Http404 from None

    def sanitise(value):
        validator = PageBinaryUpdateSerializer(data={"description_html": value})
        validator.is_valid(raise_exception=True)
        return validator.validated_data["description_html"]

    def convert(value):
        try:
            response = requests.post(
                os.environ["CE_PLANE_LIVE_URL"].rstrip("/") + "/live/convert-document/",
                json={"description_html": value, "variant": "document"},
                timeout=20,
            )
            response.raise_for_status()
            converted = response.json()
            if not isinstance(converted, dict):
                raise ValueError("Live reply is not an object")
            return converted
        except (requests.RequestException, ValueError, KeyError):
            error = APIException("Live document conversion unavailable; nothing written")
            error.status_code = 502
            raise error from None

    def prepare(body):
        """Validate all three formats together; description_binary decodes to bytes."""
        binary = PageBinaryUpdateSerializer(data=converted_body(body, sanitise, convert))
        binary.is_valid(raise_exception=True)
        return binary.validated_data

    def close_editors(doc_id):
        try:
            client = redis_instance()
        except Exception:
            raise PageInputError("close") from None
        publish_force_close(client, doc_id)

    class Pages(BaseAPIView):
        permission_classes = [IsAuthenticated, ProjectPagePermission]

        def visible(self):
            slug, project_id = self.kwargs["slug"], self.kwargs["project_id"]
            project = found(Project, id=project_id, workspace__slug=slug, archived_at__isnull=True)
            guest = ProjectMember.objects.filter(
                project=project, member=self.request.user, is_active=True, role=5
            ).exists()
            from django.db.models import Q

            rows = Page.objects.filter(
                workspace__slug=slug,
                project_pages__project_id=project_id,
                project_pages__deleted_at__isnull=True,
            )
            rows = rows.filter(Q(owned_by=self.request.user) | Q(access=0))
            if guest and not project.guest_view_all_features:
                rows = rows.filter(owned_by=self.request.user)
            return rows.distinct().order_by("id")

        def get(self, request, slug, project_id, page_id=None):
            rows = self.visible()
            if page_id:
                return Response(PageDetailSerializer(found(rows, id=page_id)).data)
            # Bound the explicit CE contract; no silent truncation or inferred cursor.
            rows = list(rows.values(*LIST_FIELDS)[:1001])
            if len(rows) > 1000:
                raise ValidationError(
                    "More than 1000 project pages; add pagination before continuing"
                )
            return Response(rows)

        def post(self, request, slug, project_id):
            try:
                data = creation_payload(request.data, sanitise, convert)
            except PageInputError as error:
                message = INPUT_ERRORS.get(error.key, "Invalid page input")
                raise ValidationError(message) from None
            except ValueError:
                raise ValidationError("Invalid page input") from None
            binary = PageBinaryUpdateSerializer(
                data={
                    key: data[key]
                    for key in ("description_html", "description_json", "description_binary")
                }
            )
            binary.is_valid(raise_exception=True)
            serializer = PageSerializer(
                data={"name": data["name"], "access": data["access"]},
                context=dict(
                    project_id=project_id, owned_by_id=request.user.id, **binary.validated_data
                ),
            )
            serializer.is_valid(raise_exception=True)
            # Check the active project before creating both page and project membership.
            self.visible()
            with transaction.atomic():
                page = serializer.save(created_by=request.user, updated_by=request.user)
            return Response(PageDetailSerializer(page).data, status=201)

        def patch(self, request, slug, project_id, page_id):
            @contextmanager
            def locked():
                # FOR UPDATE cannot share a query with DISTINCT; recheck visibility by subquery.
                rows = Page.objects.select_for_update().filter(pk__in=self.visible().values("id"))
                with transaction.atomic():
                    yield found(rows, id=page_id)

            def version(row):
                # Explicit snapshot of the body being replaced, owned by the editor.
                PageVersion.objects.create(
                    page_id=row.id,
                    workspace_id=row.workspace_id,
                    owned_by_id=request.user.id,
                    description_html=row.description_html,
                    description_json=row.description_json,
                    description_binary=row.description_binary,
                )
                # Same cap as Plane's track_page_version, oldest first.
                stale = list(
                    PageVersion.objects.filter(page_id=row.id)
                    .order_by("-last_saved_at")
                    .values_list("id", flat=True)[VERSION_LIMIT:]
                )
                if stale:
                    PageVersion.objects.filter(id__in=stale).delete()

            def client_error(error):
                message = INPUT_ERRORS.get(error.key, "Invalid page input")
                if error.key in UPDATE_STATUS:
                    failure = APIException(message)
                    failure.status_code = UPDATE_STATUS[error.key]
                    return failure
                return ValidationError(message)

            try:
                result = page_update(
                    request.data,
                    load=lambda: found(self.visible(), id=page_id),
                    prepare=prepare,
                    close_editors=close_editors,
                    wait=time.sleep,
                    locked=locked,
                    version=version,
                )
            except PageInputError as error:
                raise client_error(error) from None
            except ValueError:
                raise ValidationError("Invalid page input") from None
            page = result["page"]
            # Mention/embed log, as Plane's description update does. track_page_version is
            # not queued: in CE v1.4.3 it reads a missing Page.description attribute, and
            # if fixed it would overwrite the snapshot above in place (same owner, <600 s).
            try:
                page_transaction.delay(
                    new_description_html=page.description_html,
                    old_description_html=result["old_html"],
                    page_id=str(page.id),
                )
            except Exception:
                # The body is committed; a retry would only hit the precondition.
                logging.getLogger(__name__).warning("Page transaction not queued: %s", page.id)
            # No wait here: a sync worker must not be held for a Live debounce.
            detail = dict(PageDetailSerializer(page).data)
            detail["late_store_check"] = late_store_check(datetime.now(timezone.utc))
            return Response(detail)

    # Import confirms this image uses the reviewed CE layout; fail startup on drift.
    if PageViewSet.permission_classes != [ProjectPagePermission]:
        raise RuntimeError("Plane Pages permission contract changed")
    base = "workspaces/<str:slug>/projects/<uuid:project_id>/ce-pages/"
    return [
        path(base, Pages.as_view(http_method_names=["get", "post"])),
        path(base + "<uuid:page_id>/", Pages.as_view(http_method_names=["get", "patch"])),
    ]

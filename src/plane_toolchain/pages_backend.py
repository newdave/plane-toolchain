"""Project Pages compatibility for Plane CE v1.4.3.

Installed in a derived backend image, never imported by TerrainLink's API.
The limited contract deliberately excludes edits to existing collaborative documents.
"""

# Dependencies below exist in the Plane backend image, not TerrainLink's environment.
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
    "fields": "Only name, description_html and access are supported",
    "name": "Expected a non-empty page name of at most 255 characters",
    "body": "Invalid page body or access",
    "document": "Live service did not return a complete document",
}


class PageInputError(ValueError):
    """Client-safe validation failure identified by an INPUT_ERRORS key."""

    def __init__(self, key):
        super().__init__(INPUT_ERRORS[key])
        self.key = key


def creation_payload(data, sanitise, convert):
    """Validate and convert a complete new document before any database write."""
    if set(data) - {"name", "description_html", "access"}:
        raise PageInputError("fields")
    name = data.get("name")
    body = data.get("description_html")
    access = data.get("access", 1)
    if not isinstance(name, str) or not name.strip() or len(name) > 255:
        raise PageInputError("name")
    if not isinstance(body, str) or len(body.encode()) > 1_000_000 or access not in (0, 1):
        raise PageInputError("body")
    clean = sanitise(body)
    converted = convert(clean)
    if (
        not isinstance(converted.get("description_json"), dict)
        or not isinstance(converted.get("description_binary"), str)
        or not converted["description_binary"]
    ):
        raise PageInputError("document")
    return dict(
        name=name,
        access=access,
        description_html=clean,
        description_json=converted["description_json"],
        description_binary=converted["description_binary"],
    )


def routes():
    """Use Plane's authentication, throttling, serializers and project permissions."""
    import os

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
    from plane.db.models import Page, Project, ProjectMember
    from rest_framework.exceptions import APIException, ValidationError
    from rest_framework.permissions import IsAuthenticated
    from rest_framework.response import Response

    def found(source, **lookup):
        """404 for absent rows and malformed ids; never a 500."""
        try:
            return get_object_or_404(source, **lookup)
        except (DjangoValidationError, ValueError):
            raise Http404 from None

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
                    return response.json()
                except (requests.RequestException, ValueError, KeyError):
                    error = APIException("Live document conversion unavailable; nothing written")
                    error.status_code = 502
                    raise error from None

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

    # Import confirms this image uses the reviewed CE layout; fail startup on drift.
    if PageViewSet.permission_classes != [ProjectPagePermission]:
        raise RuntimeError("Plane Pages permission contract changed")
    base = "workspaces/<str:slug>/projects/<uuid:project_id>/ce-pages/"
    return [
        path(base, Pages.as_view(http_method_names=["get", "post"])),
        path(base + "<uuid:page_id>/", Pages.as_view(http_method_names=["get"])),
    ]
